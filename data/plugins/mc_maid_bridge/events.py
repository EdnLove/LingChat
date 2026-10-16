"""模组推来的一条游戏事件 → 一句只陈述事实的中文。

只写发生了什么，不写「该有什么情绪」——情绪由模型自己决定，留在台词表里的始终是
客观记录。哪一条算紧急（要当场请角色开口）由 `_URGENT` 定，事件落地在 hooks.py。

**身边那一圈刻意不在这里登记**：附近有什么活物、玩家进出视线、头顶遮没遮住、脚下踩到
什么，这四类（模组的 `player_proximity_change` / `player_sight_change` / `living_near` /
`shelter_change` / `ground_change`）不再翻成旁白。她此刻的处境由玩家开口那一刻随消息
带过去的一份实时快照交代（见 `tools.carry_text`），逐条记下来只会让台词表积满
「附近有村民」——2026-10-16 真机上一个晚上 34 条事件旁白里 31 条是这类。模组那边照推
不误，这里不认就等于丢掉，是本次改动有意为之的「不认识就不出声」（见下）。

加一类事件：写一个函数，在 `FORMATTERS` 里登记一行；紧急的话再进 `_URGENT`。
每个函数收 `(data, player_name)`，只有受伤那条用得上名字（打她的可能是玩家本人）。
认不出的 event_type 一律不出声——模组先加了新事件、插件还没跟上时，这是刻意的向后兼容。
"""

from wording import (
    BIOME_NAMES,
    DAMAGE_HINTS,
    DIMENSION_NAMES,
    MOB_NAMES,
    as_int,
    direction_text,
    name_of,
    number,
)

# 受理即完成的动作收尾时记进台词表的旁白。go_to 另有说法（看走到哪了）。
ACTION_DONE_PHRASES = {
    "sit": "你坐下了。",
    "stand": "你站起来了。",
    "lie_down": "你躺到床上睡着了。",
    "look_at": "你转头看向了玩家。",
}


def format_event(data, player_name=""):
    """一条事件 → (中文, 是否紧急)。认不出的类型返回空文本，调用方据此什么都不做。"""
    kind = (data.get("event_type") or "").strip()
    formatter = FORMATTERS.get(kind)
    if formatter is None:
        return "", False
    return formatter(data, player_name), kind in URGENT


def snapshot(data, _player):
    """接上世界时的第一帧：把眼下的处境交代清楚，模型才知道自己在哪儿。

    顺手把玩家在游戏里的名字写上：所有事件旁白里出现的都是这个名字，模型得先知道
    它指的就是玩家本人，才不会当成另一个人。名字来自模组，没配置也能说清。
    """
    player = (data.get("player_name") or "").strip()
    who = f"玩家（{player}）" if player else "玩家"
    clauses = [
        f"你正和{who}在"
        f"{name_of(DIMENSION_NAMES, data.get('dimension'))}的{name_of(BIOME_NAMES, data.get('biome'))}"
    ]
    # 下界 / 末地没有天空，也就没有昼夜与天气这回事
    if data.get("sky"):
        clauses.append("现在是白天" if data.get("phase") == "day" else "现在是夜里")
        clauses.append(_weather_phrase(data))
    health = f"你的血量 {number(data.get('maid_health'))}/{number(data.get('maid_max_health'))}"
    if as_int(data.get("player_max_health"), 0) > 0:
        health += f"，玩家 {number(data.get('player_health'))}/{number(data.get('player_max_health'))}"
    return f"{'，'.join(clauses)}。{health}。"


def biome(data, _player):
    return f"周围的环境变了，现在来到了{name_of(BIOME_NAMES, data.get('biome'))}。"


def dimension(data, _player):
    came_from = name_of(DIMENSION_NAMES, data.get("from_dimension"))
    went_to = name_of(DIMENSION_NAMES, data.get("to_dimension"))
    return f"你跟着玩家从{came_from}来到了{went_to}。"


def time_phase(data, _player):
    return "天黑了。" if data.get("phase") == "night" else "天亮了。"


def weather(data, _player):
    if data.get("thundering"):
        return "变天了，外面在打雷。"
    if data.get("raining"):
        return "开始下雨了。"
    return "雨停了，天放晴了。"


def low_health(data, _player):
    who = "你" if data.get("who") == "maid" else "玩家"
    now = number(data.get("health"))
    most = number(data.get("max_health"))
    return f"{who}的血量只剩 {now}/{most}，快撑不住了。"


def maid_hurt(data, player_name):
    """女仆受伤。模组会把一小段连续挨打合成一条，所以这里要照顾「几下」。"""
    attacker = (data.get("attacker") or "").strip()
    if attacker and player_name and attacker == player_name:
        cause = "被玩家打了"
    elif attacker:
        cause = f"被{attacker}打了"
    else:
        cause = DAMAGE_HINTS.get((data.get("damage_type") or "").strip(), "挨了打")
    count = as_int(data.get("count"), 1)
    times = f"连续 {count} 下，" if count > 1 else ""
    damage = number(data.get("damage"))
    now = number(data.get("health"))
    most = number(data.get("max_health"))
    return f"你{cause}，{times}一共掉了 {damage} 点血，现在是 {now}/{most}。"


def threat_near(data, _player):
    """敌对生物靠近。说清是什么、多远、在哪个方向；还没看见它就补一句。

    身边那一层只剩这一条还在写旁白：它是唯一「得当场叫醒她」的观察，其余几类都由
    开口时那份快照带过去。
    """
    who = name_of(MOB_NAMES, data.get("type"), "什么东西")
    where = direction_text(data.get("direction"))
    text = f"有{who}靠过来了，大约 {number(data.get('distance'))} 格，在你{where}。"
    if not data.get("visible"):
        text += "你还没看到它。"
    return text


def action_done(data, _player):
    """动作收尾。三种结局都要说：被顶掉也要说，否则角色会一直以为还在做。"""
    action = (data.get("action") or "").strip()
    status = (data.get("status") or "").strip()
    if status == "done":
        if action == "go_to":
            if (data.get("target") or "") == "bed":
                return "你走到了床边。"
            return "你走到了玩家身边。"
        return ACTION_DONE_PHRASES.get(action, "这件事你做完了。")
    if status == "cancelled":
        return "刚才那件事被新的事顶掉了，没做成。"
    reason = (data.get("reason") or "").strip()
    return f"刚才那件事没做成：{reason}" if reason else "刚才那件事没做成。"


def _weather_phrase(data):
    if data.get("thundering"):
        return "外面在打雷"
    if data.get("raining"):
        return "外面在下雨"
    return "天气晴朗"


FORMATTERS = {
    "state_snapshot": snapshot,
    "biome_change": biome,
    "dimension_change": dimension,
    "time_phase_change": time_phase,
    "weather_change": weather,
    "low_health": low_health,
    "maid_hurt": maid_hurt,
    "threat_near": threat_near,
    "action_done": action_done,
}

# 这几类当场请角色开口（挨打、血量告急、敌对生物靠近、动作收尾）；
# 其余走「攒够几条」那套，不然每一次散步都会变成一次生成。
URGENT = frozenset({"low_health", "maid_hurt", "threat_near", "action_done"})
