"""七个工具的入口（manifest 的 `[[tools]]` 指向本文件），外加「她身边那一圈」的措辞。

`run(ctx)` 返回的文本就是模型看到的结果，所以说实话最要紧：没连上、没绑定女仆、模组说
办不到，一律如实回报，绝不假装已经执行——模型会照着这个结果跟玩家交代。

工具分三类：
- 动作（走出去、坐下、站起、躺下、看过来）：把意图翻成一条 `maid_action` 帧，受理与否
  当场给答复；走过去这类耗时的，结果稍后由 `action_done` 事件旁白交代。
- 查询（看一眼周围）：现取一份快照翻成几句事实，只回给模型看，不写台词表。
- 看图（看一眼玩家屏幕）：抓一张画面交给宿主，宿主把它当图片附给模型——她这才看得到
  你搭的爱心是什么形状。

那份快照有第二个用处：玩家在 MC 里说话时，随那句话带一句「你此刻的处境」（见
`carry_text`）；她挨打那类「出事」的旁白后面也缀一句眼前的活物（见 `creature_text`）。
身边的变化不再逐条写成旁白，她知道周围有什么就靠这两处。

给模型的话一律不带数字：格数、血量、光照写进上下文，它就会照本宣科地复述
「大约 10.5 格」这种句子，而玩家并不需要知道几位小数。
"""

from plugin_host import capture_screen

from bridge import ask, config, ready
from wording import (
    BLOCK_NAMES,
    DIMENSION_NAMES,
    MOB_NAMES,
    name_of,
    nearness_place,
    sighting,
)

# 动作的回执要等模组受理，超时明显小于工具自身的 15s，否则模型在工具超时里干等。
ACTION_TIMEOUT_MS = 8000
# 看一眼周围等快照的超时。单人游戏按 Esc 暂停时它必然超时，这时要如实说。
LOOK_TIMEOUT_MS = 8000
# 玩家说话时顺手取快照的超时：给得很短，玩家那句话不能被一次查询拖住（游戏暂停时
# 模组答不上来，这一轮就不带，照常把话送进对话）。
CARRY_TIMEOUT_MS = 1500

# 玩家离她这么近才算「在身边」。只判距离，不问「她看不看得见他」——视线那条会让模型
# 去编「我看见你了」，而真正要紧的只是主人有没有走远。
OWNER_NEAR = 30.0

LOOK_AROUND_TOOL = "maid_look_around"
SEE_SCREEN_TOOL = "maid_see_screen"

# 抓屏时按这个关键词找游戏窗口（标题或进程名命中即可），找不到就抓整个屏幕。
DEFAULT_SCREEN_WINDOW = "Minecraft"

# 图交给宿主要附的那句话：图从哪来、看到的是什么，模型才知道该怎么用。
SCREEN_HINT = (
    "（这是玩家屏幕上此刻的样子，也就是你眼前看到的画面。"
    "图里如果有聊天窗口，那是你自己在的那扇窗，不用管它。）"
)

# 带目标的动作与各自的默认目标。执行器只校验 JSON Schema 的一个子集、不认 enum，
# 所以合法值在这里兜底（模组那头还会再校验一次）。
TARGET_DEFAULTS = {"go_to": "player", "lie_down": "bed"}

# 受理了但还没做完时给模型的话：必须让它知道自己还没到位，否则它会当场宣布已经办到。
PENDING_TEXT = {
    "go_to": "正在走过去，到了会告诉你。",
    "lie_down": "正在走过去，躺下了会告诉你。",
}

# 受理即完成的动作给模型的话。
DONE_TEXT = {
    "sit": "已经坐下了。",
    "stand": "已经站起来了。",
    "look_at": "已经看向玩家了。",
}


def run(ctx):
    """工具入口。认不出的工具如实拒绝，不猜。"""
    tool = (ctx.get("tool_name") or "").strip()
    handler = TOOLS.get(tool)
    if handler is None:
        return {"ok": False, "error": f"这个工具不存在：{tool}"}
    return handler(ctx)


def action_tool(action):
    """把一个动作包成工具入口：工具名带插件前缀，动作名不带。"""

    def handler(ctx):
        return _do_action(ctx, action)

    return handler


def _do_action(ctx, action):
    maid, error = ready(ctx)
    if not maid:
        return {"ok": False, "error": error}

    data = {"maid_id": maid, "action": action}
    target = _action_target(action, ctx.get("args") or {})
    if target:
        data["target"] = target

    response, error = ask({"type": "maid_action", "data": data}, ACTION_TIMEOUT_MS)
    if error:
        return {"ok": False, "error": error}
    frame = response.get("data") or {}
    if not frame.get("accepted"):
        return {"ok": False, "error": frame.get("reason") or "游戏拒绝了这次动作"}
    if frame.get("async"):
        return {"ok": True, "message": PENDING_TEXT.get(action, "正在做，好了会告诉你。")}
    return {"ok": True, "message": DONE_TEXT.get(action, "已经做完了。")}


def _action_target(action, args):
    """这次动作要带的目标；不带目标的动作返回空串。

    认不出的取值一律退回默认：执行器的 schema 校验不认 enum，模型可能塞别的字符串。
    """
    default = TARGET_DEFAULTS.get(action)
    if not default:
        return ""
    raw = args.get("target")
    raw = raw.strip().lower() if isinstance(raw, str) else ""
    if raw not in ("player", "bed"):
        return default
    # 躺下只认床：player 这种取值对它没有意义
    if action == "lie_down" and raw != "bed":
        return default
    return raw


def _look_around(ctx):
    """看一眼她周围：取一份快照翻成几句事实给模型。

    只回去给模型看，不写台词表、也不请谁开口——玩家正在跟她说话，这一轮本来就要回话，
    再写一条旁白就是重复。
    """
    maid, error = ready(ctx)
    if not maid:
        return {"ok": False, "error": error}

    data, error = snapshot(maid, LOOK_TIMEOUT_MS)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "message": _look_around_text(data)}


def _see_screen(ctx):
    """看一眼玩家此刻屏幕上的画面，把图交给模型。

    图不经过这段文字：宿主会把工具结果里的 image 摘出来，单起一条消息附给模型，
    只在这一轮生效、不进长期记忆。抓不到就如实说，别让模型照着想象编。
    """
    window = (config(ctx).get("screen_window") or "").strip()
    shot = capture_screen(window=window or DEFAULT_SCREEN_WINDOW)
    if not shot.get("ok"):
        return {"ok": False, "error": shot.get("error") or "抓不到画面"}
    image = (shot.get("image") or "").strip()
    if not image:
        return {"ok": False, "error": "抓回来的画面是空的"}
    return {
        "ok": True,
        "message": "你看了一眼玩家此刻屏幕上的画面，图已经附上了。",
        "image": {"data_url": image, "hint": SCREEN_HINT},
    }


def snapshot(maid_id, timeout_ms):
    """现取一份她身边的快照。返回 (数据, 错误说明)；出错时数据是 None。

    工具「看一眼周围」与「玩家开口时顺手带一份」共用这条路：帧、超时口径、老模组
    认不出这个帧时的说法都只有一处。
    """
    response, error = ask({"type": LOOK_AROUND_TOOL, "data": {"maid_id": maid_id}}, timeout_ms)
    if error:
        return None, error
    if response.get("type") == "error":
        # 老模组认不出这个帧，回的是统一错误帧（里面只有 message，没有业务字段）
        message = ((response.get("data") or {}).get("message") or "").strip()
        return None, f"游戏那边没能回答：{message or '这个模组版本还不支持查看周围'}"
    return response.get("data") or {}, ""


def carry_text(data):
    """一份快照 → 一句「你此刻的处境」，附在玩家的发言后面。

    只挑此刻要紧的说：脚下与头顶、玩家在不在身边、附近有什么活物。正前方那一眼留给
    工具那句——玩家问起来才需要那么细，每句话都带上就成了噪音。
    帧里没有 owner 字段说明这压根不是快照（多半是别的东西回了信），那就什么都不带。
    """
    if "owner" not in data:
        return ""
    parts = []
    foot = _footing_phrase(data)
    if foot:
        parts.append(foot)
    parts.append(_owner_phrase(data.get("owner") or {}))
    creatures = creature_phrase(data)
    if creatures:
        parts.append(creatures)
    return "（你此刻：" + "；".join(parts) + "）"


def creature_phrase(data):
    """附近有哪些活物，一句话的中间那半截（「附近有左前方近处的僵尸」）。

    出事那一刻（她挨打、血量告急）跟着事件旁白一起给她——怪物另记一条有 2 分钟防抖
    挡着，但「她刚挨了一下，旁边站着谁」是当下最要紧的处境，不该等到玩家下次说话。
    """
    nearby = data.get("nearby") or []
    if not nearby:
        return ""
    described = "、".join(sighting(one) for one in nearby)
    return f"附近有{described}"


def creature_text(data):
    """同上，但成句：没有活物就什么都说不出来（空串）。"""
    phrase = creature_phrase(data)
    return f"{phrase}。" if phrase else ""


def _look_around_text(data):
    """她身边这一圈，说成四句事实：站位 → 正前方 → 玩家 → 附近的活物。

    一律不给数字：格数、血量、光照写进上下文，模型就会照本宣科地念出来。
    """
    dimension = name_of(DIMENSION_NAMES, data.get("dimension"), "某个世界")
    foot = name_of(BLOCK_NAMES, (data.get("standing_on") or {}).get("id"), "说不清的东西")
    roof = "头顶有遮挡，看不到天" if data.get("sheltered") else "头顶是露天的"
    lines = [f"你正站在{dimension}，脚下是{foot}，{roof}。"]

    look = data.get("looking_at") or {}
    look_kind = (look.get("kind") or "").strip()
    if look_kind == "block":
        block = name_of(BLOCK_NAMES, look.get("id"), "一个方块")
        lines.append(f"正前方{nearness_place(look.get('distance'))}是{block}。")
    elif look_kind == "entity":
        entity = name_of(MOB_NAMES, look.get("id"), "一个活物")
        lines.append(f"正前方{nearness_place(look.get('distance'))}是{entity}。")
    else:
        lines.append("正前方没有挡着的东西，视线是通的。")

    lines.append(_owner_phrase(data.get("owner") or {}) + "。")

    creatures = creature_text(data)
    lines.append(creatures if creatures else "附近没有别的活物。")
    if data.get("truncated"):
        lines.append("更远的没列出来。")
    return "".join(lines)


def _footing_phrase(data):
    """脚下踩着哪。「近/中/远」的档位在这句里没意义，所以单独拎出来。"""
    foot = name_of(BLOCK_NAMES, (data.get("standing_on") or {}).get("id"), "")
    if not foot:
        return ""
    # 下界与末地没有天空，「头顶有没有遮挡」在那里无从谈起
    roof = ""
    if data.get("has_sky"):
        roof = "，头顶有遮挡" if data.get("sheltered") else "，头顶是露天的"
    return f"脚下是{foot}{roof}"


def _nearness(distance):
    """「近处」/「不远处」/「远处」，塞进「正前方 X 是…」这种句子用。"""
    return {"近": "近处", "中": "不远处", "远": "远处"}.get(nearness(distance), "不远处")


def _owner_phrase(owner):
    """主人在不在她身边（`OWNER_NEAR` 之内），不带句号。

    只说在不在，不给格数，也不问视线：两条路（工具与开口时携带）共用这一句，
    说法只有一处。
    """
    if not owner.get("same_dimension"):
        return "玩家不在你所在的这个世界里"
    try:
        distance = float(owner.get("distance"))
    except (TypeError, ValueError):
        return "你身边没有玩家"
    if 0 <= distance <= OWNER_NEAR:
        return "玩家就在你身边"
    return "玩家不在你身边"


TOOLS = {
    "maid_go_to": action_tool("go_to"),
    "maid_sit_down": action_tool("sit"),
    "maid_stand_up": action_tool("stand"),
    "maid_lie_down": action_tool("lie_down"),
    "maid_look_at": action_tool("look_at"),
    LOOK_AROUND_TOOL: _look_around,
    SEE_SCREEN_TOOL: _see_screen,
}
