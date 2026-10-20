"""攻击工具：让她去打指定的那个活物。"""

from bridge import ask, ready
from naming import names, narrate
from wording import MOB_NAMES, attack_style, attack_target, name_of

from tools.common import ACTION_TIMEOUT_MS

ATTACK_TOOL = "maid_attack"


def _attack(ctx):
    """让她去打一个活物。点谁由玩家那句话决定，插件只把说法翻成 id 或类别。"""
    maid, error = ready(ctx)
    if not maid:
        return {"ok": False, "error": error}

    maid_name, player = names()
    raw = (ctx.get("args") or {}).get("target")
    raw = raw.strip() if isinstance(raw, str) else ""
    target = attack_target(raw)
    if not target:
        return {
            "ok": False,
            "error": f"没听懂要打谁：{raw or '（空）'}。可以说活物的名字（僵尸、羊），"
            "另一个玩家的名字（照「看一眼周围」里的名字说），"
            "或者 hostile（最近的敌对生物）、animal（最近的动物）、other_player（另一个玩家）、"
            f"owner（主人自己，也就是{player}）。",
        }
    style = attack_style((ctx.get("args") or {}).get("style"))

    response, error = ask(
        {"type": ATTACK_TOOL, "data": {"maid_id": maid, "target": target, "style": style}}, ACTION_TIMEOUT_MS
    )
    if error:
        return {"ok": False, "error": error}
    frame = response.get("data") or {}
    if response.get("type") == "error":
        return {"ok": False, "error": narrate(frame.get("message")) or "游戏那边没能下达攻击"}
    if not frame.get("accepted"):
        return {"ok": False, "error": narrate(frame.get("reason")) or "游戏拒绝了这次攻击"}
    return {"ok": True, "message": _attack_text(frame, maid_name, player)}


def _attack_text(frame, maid, player):
    """攻击回执 → 一句话。目标只说名字，不给格数。

    说清这个工具**只负责攻击**：它不保证目标已经倒下。模型很容易把「盯上了」当成
    「已经解决」，而实际上她可能才砍到第一下。

    轻轻一下是另一回事：只出一手、伤害固定，说清「打完就收手」就够，不必再提倒下。
    """
    target = frame.get("target") or {}
    if target.get("owner"):
        who = player
    else:
        fallback = (target.get("name") or "").strip() or "那个活物"
        who = name_of(MOB_NAMES, target.get("type"), fallback)
    if frame.get("style") == "light":
        return f"{maid}抬手轻轻拍了{who}一下，随即收手，没有追。"
    parts = [f"{maid}提着家伙朝{who}冲了过去，第一下已经挥出。"]
    if not frame.get("weapon", True):
        parts.append(f"{maid}手上没拿武器，只能空手打，落下去的力道很轻。")
    parts.append(f"{who}还站着，这一架要打上一会儿——{maid}会追着它打，直到它倒下或者再也追不上。")
    return "".join(parts)


TOOLS = {
    ATTACK_TOOL: _attack,
}
