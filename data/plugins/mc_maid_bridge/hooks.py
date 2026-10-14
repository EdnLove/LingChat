"""车万女仆桥接：把 MC 玩家的话送进对话，把角色的回话送回游戏内女仆气泡。

第一期只做「说话」——不涉及工具调用与游戏内动作。三个入口共用本文件：
`on_start`（启动/启用）、`on_ws`（WS 连接与收帧）、`on_reply`（每条助手回复）。

插件脚本每次调用都新建解释器、跑完即弃，所以这里不保存任何跨调用状态：
用得到的信息一律当场问（见 `_maid_id`）。
"""

from plugin_host import (
    edit_context,
    read_context,
    send_user_message,
    ws_open,
    ws_request,
    ws_send,
    ws_status,
)

CONN_ID = "tlm"
DECLARED_WS_URL = "ws://127.0.0.1:48920"

# 提示行的标记前缀，用来在台词表里认出自己写过的那条——幂等就靠它。
ENTRY_TAG = "［Minecraft 桥接］"

ENTRY_HINT = (
    "［Minecraft 桥接］你已和玩家一起进入 Minecraft 世界，游戏里的女仆由你操控。\n"
    "- 你在 LingChat 回的话会同步显示成游戏内女仆的聊天气泡，照常说话就行。\n"
    "- 以「［Minecraft］」开头的消息来自游戏世界，是玩家在 MC 里说的，按玩家发言对待。"
)

# 幂等检查先扫这么多条尾巴；不够长才整段再扫一次（见 `_entry_hint_written`）。
TAIL_SCAN = 50

# 台词行里这些字段是「哪一句、怎么念」的，不该跟着提示行一起复制过去。
_EPHEMERAL_LINE_FIELDS = (
    "original_emotion",
    "predicted_emotion",
    "tts_content",
    "action_content",
    "audio_file",
    "thinking",
    "tool_call",
)


# ─── 入口 ───


def on_start(ctx):
    """启动与启用时各跑一次：把配置里的地址应用到声明的那条连接上。"""
    url = _config(ctx).get("ws_url") or ""
    url = url.strip() if isinstance(url, str) else ""
    # 声明里的 url 不能写占位符：玩家没保存过配置时 ctx.config 不含该键，
    # 占位符不会被替换。所以默认值写在 manifest，非默认值在这里覆盖。
    if url and url != DECLARED_WS_URL:
        ws_open(CONN_ID, url=url)


def on_ws(ctx):
    """WS 连接的建立与收帧。"""
    payload = ctx.get("payload") or {}
    if payload.get("connId") != CONN_ID:
        return
    event = payload.get("event")
    if event == "connect":
        _bind_maid(ctx)
        _append_entry_hint()
    elif event == "message":
        _handle_frame(payload.get("data"))
    # disconnect / error 交给宿主的 auto_reconnect 退避重连，这里不做额外处理。


def on_reply(ctx):
    """每条助手回复都触发（逐句，含非终句）：把这一句同步成游戏内气泡。"""
    payload = ctx.get("payload") or {}
    # 剧本编辑器的预览流不该外发到游戏里。
    if payload.get("previewGen") or not payload.get("roleId"):
        return
    message = (payload.get("message") or "").strip()
    if not message:
        return
    if not _truthy(_config(ctx).get("echo_to_game"), True):
        return
    if not _connected():
        return
    maid_id, _ = _maid_id(_config(ctx))
    if not maid_id:
        return
    ws_send(
        CONN_ID,
        {"type": "send_chat", "data": {"maid_id": maid_id, "message": message}},
    )


# ─── 下行：MC 里玩家说的话 ───


def _bind_maid(ctx):
    """告诉模组盯哪只女仆。

    这一步是玩家说话能被推过来的前提：模组得先知道盯的是哪只女仆、且它的主人正是
    说话的那个玩家，才会把聊天转过来。不发这一条，MC 里的人说话永远没反应，而且
    是静默的——现象跟模组没装一模一样。

    每次重连都要重发：模组换存档或重启服务端时会把这个绑定清掉。
    """
    maid_id, reason = _maid_id(_config(ctx))
    if not maid_id:
        print(f"[mc_maid_bridge] 没能绑定女仆：{reason}")
        return
    ws_send(CONN_ID, {"type": "set_monitored_maid", "data": {"maid_id": maid_id}})


def _handle_frame(data):
    if not isinstance(data, dict):
        return
    if data.get("type") != "chat_message":
        return
    chat = data.get("data") or {}
    message = (chat.get("message") or "").strip()
    if not message:
        return
    sender = (chat.get("sender") or "").strip() or "玩家"
    # 与玩家在输入框里发一句走同一条路：写进台词历史、按当前配置生成回复，
    # 所以聊天窗里看得到，角色的回话也会顺着 on_reply 变成游戏内气泡。
    send_user_message(f"［Minecraft］{sender}说：{message}")


# ─── 进入 MC 的提示行 ───


def _append_entry_hint():
    """往台词表末尾追加一条「进入 MC」的系统提示行，幂等。

    `edit_context` 只能替换、不能追加，所以「追加」是把最后一条展开成两条。
    写成 attribute=User + display_name=系统：记忆构建对「系统」不加「名字:」前缀，
    模型看到的就是一条干净的系统提示，而不是「谁说了什么」。
    """
    head = read_context(start=1, end=1)
    total = head.get("total") or 0
    if not head.get("ok") or not total:
        return
    since = max(1, total - TAIL_SCAN)
    tail = read_context(start=since, end=total)
    if _entry_hint_written(tail, since):
        return
    lines = tail.get("lines") or []
    if not lines:
        return
    hint = dict(_pick_template(lines))
    hint.update({"id": None, "attribute": "User", "display_name": "系统", "content": ENTRY_HINT})
    for field in _EPHEMERAL_LINE_FIELDS:
        hint[field] = None
    result = edit_context([lines[-1], hint], start=total, end=total)
    if not result.get("ok"):
        # 最常见的原因是正在生成回复（这时编辑会被拒），下次重连再试即可。
        print(f"[mc_maid_bridge] 进入 MC 的提示行没能写入：{result.get('error')}")


def _entry_hint_written(tail, since):
    """台词表里是否已经有过提示行。

    先看尾巴（重连的常见情形，便宜）；历史比尾巴窗口更长、尾巴里又没找到时，
    才整段扫一遍确认——避免历史很长时每次连接都做一次全量导出。
    """
    if _has_entry_tag(tail):
        return True
    if since <= 1:
        return False
    return _has_entry_tag(read_context())


def _has_entry_tag(history):
    lines = history.get("lines") or []
    return any(ENTRY_TAG in (line.get("content") or "") for line in lines)


def _pick_template(lines):
    """挑一条字段形态最贴合当模板：优先最后一条 User 行。

    直接拿最后一条也行，但若那是助手行，它的 perceived_role_ids 记的是「谁听到了
    它」，跟我们想要的「谁该看到这条提示」不是一回事。
    """
    for line in reversed(lines):
        if line.get("attribute") == "User":
            return line
    return lines[-1]


# ─── 小工具 ───


def _config(ctx):
    return ctx.get("config") or {}


def _maid_id(cfg):
    """绑定的女仆 UUID；没配现问一次。返回 (id, 错误说明)。

    插件无状态存不下发现到的 id，所以未配置时每次用到都要重问一遍；
    本机回环，代价可忽略。填了配置就完全免掉这次往返。
    """
    configured = cfg.get("maid_id")
    configured = configured.strip() if isinstance(configured, str) else ""
    if configured:
        return configured, ""
    result = ws_request(CONN_ID, {"type": "get_maid_status", "data": {}}, timeout_ms=5000)
    if not result.get("ok"):
        return "", result.get("error") or "无法获取女仆状态"
    frame = result.get("response") or {}
    maids = ((frame.get("data") or {}).get("maids")) or []
    if not maids:
        return "", "游戏里没有找到女仆"
    return maids[0].get("id") or "", ""


def _connected():
    status = ws_status(conn_id=CONN_ID)
    return bool(status.get("ok")) and status.get("state") == "connected"


def _truthy(value, default):
    """配置项可能是布尔、也可能是字符串（前端表单传过字符串）。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no")
    return bool(value)
