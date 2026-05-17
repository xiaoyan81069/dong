"""
冬 · 交互模块
- 消息发送（send_msg / send_msg_lines / send_image / send_voice / send_emoji）
- 撤回机制（maybe_recall）
- 回复延迟
- 随机不回复
- 话没说完 / 事后找补
- 语境反问 / 情感波动
- 主动事件
- process_reply（回复后处理管线）
"""
import asyncio
import random
import re
import os
import time
from datetime import datetime

import requests

from .config import SENDBOT_API, MASTER_UID, ALLOWED_USERS, is_late_night
from .log import log
from .persona import (
    apply_typo_hand, apply_typo_cute, apply_input_mistakes,
    apply_nickname, punctuation_chaos, strip_emoji, strip_nicknames,
    add_love_hate_suffix, absorb_user_language, maybe_absorb_word,
)


# ============ HTTP请求辅助函数 ============
def _http_post_json(endpoint: str, data: dict, timeout: int = 10) -> dict:
    """通用的POST请求JSON API"""
    r = None
    try:
        r = requests.post(
            f"{SENDBOT_API}/{endpoint}",
            json=data,
            timeout=timeout
        )
        return r.json()
    except Exception as e:
        log(f"HTTP请求失败 [{endpoint}]: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if r is not None:
            r.close()


# ============ 延迟计算 ============
def get_reply_delay(user_text, history):
    from .expression import _active_expression
    expr = _active_expression._current
    if expr and expr.feature in ("wooden_stake", "amygdala_hijack"):
        return random.randint(0, 1)  # 真话/防御反应脱口而出，不加延迟
    if is_late_night():
        return random.randint(1, 3)
    if history:
        last_q, last_a, last_time = history[-1]
        minutes_ago = (datetime.now() - last_time).total_seconds() / 60
        if minutes_ago > 5 and any(w in user_text for w in ["在和朋友玩", "等一下", "等会", "在忙"]):
            if random.random() < 0.3:
                return random.randint(8, 20)
    return random.randint(2, 8)


# ============ 深夜模式 ============
def get_late_night_system():
    return "现在是深夜，你更愿意说真话。少傲娇，语气更软，可以多说几个字。更容易说出平时白天不会说的真心话。"


# ============ 偶尔不回 ============
NO_REPLY_TRIGGERS = ["?", "？", "在吗", "在干嘛", "人呢", "说话", "回我", "答我"]


def should_not_reply(text):
    # 每条消息都回复，不做随机跳过
    return False


# ============ 话没说完 ============
UNFINISHED_STARTS = ["我", "那个", "就是", "因为", "但是", "其实", "不过"]


def maybe_unfinished(line):
    if "?" in line or "？" in line:
        return line
    for kw in UNFINISHED_STARTS:
        if line.startswith(kw) and random.random() < 0.03:
            unfinished = ["...", "算了", "不说了", ""]
            return line + random.choice(unfinished)
    return line


# ============ 语境反问 ============
COUNTER_QUESTIONS = {
    ("吃", "饭", "饿"): ("你吃的啥呀", "好吃不", "今天吃的啥"),
    ("冷", "热", "天气"): ("你那边冷不冷", "穿了多少啊"),
    ("游戏", "排位", "上号"): ("今天排了没", "输了赢了", "上号"),
    ("出门", "外面", "回来"): ("在外面还是回来了", "去哪玩了"),
    ("心情", "烦", "开心"): ("你咋了跟我说说", "谁惹你了"),
    ("身体", "疼", "不舒服", "胃"): ("还疼不疼了", "有没有好点"),
    ("写", "码字", "稿"): ("写多少了", "催更催更"),
}


def maybe_counter_question(rep, user_text, counter_rate=0.30):
    if random.random() > counter_rate:
        return rep
    user_lower = user_text.lower()
    for keywords, questions in COUNTER_QUESTIONS.items():
        if any(kw in user_lower for kw in keywords):
            return rep + random.choice(questions)
    return rep


# ============ 情感波动 ============
def was_ignored(history):
    if not history:
        return False
    last_q, last_a, last_time = history[-1]
    minutes_ago = (datetime.now() - last_time).total_seconds() / 60
    return minutes_ago > 5 and len(last_a) < 5


def was_spammed(history):
    if len(history) < 3:
        return False
    recent = history[-3:]
    return all(len(q) < 10 for q, a, t in recent)


def should_chase_up(rep, user_text, history):
    if not history:
        return False
    trigger_words = ["回来了", "好了", "我来了"]
    if any(w in user_text for w in trigger_words):
        if random.random() < 0.50:
            return True
    return False


def get_chase_up_question(last_topic):
    questions = [
        "所以之前到底怎么了嘛",
        "你还没说完呢",
        "然后呢",
        "所以你承认了？",
    ]
    return random.choice(questions)


# ============ 事后找补 ============
EXPLANATIONS = [
    "刚才去洗澡了", "刚回来", "刚刚没看到",
    "手机没电了", "刚醒", "刚才在打游戏",
]


def maybe_explain_before(rep, history):
    if not history:
        return rep, None
    last_q, last_a, last_time = history[-1]
    minutes_ago = (datetime.now() - last_time).total_seconds() / 60
    if len(last_a) < 5 and minutes_ago >= 5 and random.random() < 0.30:
        explanation = None
        # 尝试AI生成借口
        try:
            from .status import _call_ai_simple
            from .config import describe_time_gap
            gap_text = describe_time_gap(minutes_ago)
            prompt = f"""你是"冬"。你刚才说了"{last_a}"然后消失了一阵子。现在过了大概{gap_text}，你回来了。
对方刚才说："{last_q}"
你现在在干嘛？编一个自然的解释（10字以内），口语化。
只输出一句话，不要任何其他内容。"""
            result = _call_ai_simple(
                "你是冬，一个嘴硬但心软的大学生。回复极短，口语化。",
                prompt, task="chat", temperature=0.8, max_tokens=40, timeout=5
            )
            if result and len(result) <= 20:
                explanation = result
        except Exception:
            pass
        if not explanation:
            explanation = random.choice(EXPLANATIONS)
        return explanation + " " + rep, explanation
    return rep, None


# ============ 撤回消息 ============
SOFT_WORDS = ["想你了", "好啦", "哥", "抱抱", "喜欢你", "爱你", "好想你", "亲爱的"]

REPLACEMENTS = {
    "想你了": "没什么 就是有点无聊",
    "好啦": "算了算了",
    "喜欢你": "才没有喜欢你",
    "爱你": "唉",
    "好想你": "就是闲着没事",
    "亲爱的": "谁是你亲爱的",
    "哥": "干嘛",
}


async def _recall_msg(message_id):
    if message_id is None:
        return
    try:
        def _http_delete():
            r = requests.post(
                f"{SENDBOT_API}/delete_msg",
                json={"message_id": int(message_id)},
                timeout=10
            )
            return r.json()
        data = await asyncio.to_thread(_http_delete)
        if data.get("status") == "ok":
            log(f"  撤回成功: message_id={message_id}")
        else:
            log(f"  撤回失败(API): {data}")
    except Exception as e:
        log(f"  撤回失败: {e}")


# 导出别名（供超限系统使用）
withdraw_message = _recall_msg


def get_replacement(original):
    for soft, hard in REPLACEMENTS.items():
        if soft in original:
            return hard
    return "算了不说了"


async def maybe_recall(ws, uid, sent_lines, msg_ids, had_typo):
    """撤回判断——AI心理模拟 + 硬逻辑降级兜底"""
    if not sent_lines or not msg_ids:
        return

    last_line = sent_lines[-1]
    last_msg_id = msg_ids[-1] if msg_ids else None
    if last_msg_id is None:
        return

    # 错别字撤回保留原逻辑
    if had_typo:
        await asyncio.sleep(2)
        await _recall_msg(last_msg_id)
        log(f"  错别字撤回")
        return

    # —— 破防后慌张撤回（优先级高于AI决策）——
    from .expression import _active_expression
    expr_break = _active_expression._current
    if expr_break and expr_break.feature in ("wooden_stake", "amygdala_hijack") and random.random() < 0.60:
        await asyncio.sleep(random.uniform(1, 2))
        await _recall_msg(last_msg_id)
        log(f"  破防慌张撤回: 真心话脱口后被自己撤回")
        return  # 不追发掩盖语，让 LLM 在下一轮自然生成

    # —— AI决策路径 ——
    from .status import _status
    from .intimacy import get_intimacy_level
    from .memory import get_history
    from .config import is_late_night, MASTER_UID

    hist = get_history(uid)
    recent = hist[-3:] if hist else []
    recent_msgs_lines = []
    for q, a, t in recent:
        recent_msgs_lines.append(f"对方: {q}")
        recent_msgs_lines.append(f"冬: {a}")
    recent_text = "\n".join(recent_msgs_lines[-6:]) or "无"

    context = {
        "mood": _status.get("mood", 60),
        "fatigue": _status.get("fatigue", 50),
        "intimacy_level": get_intimacy_level(uid),
        "is_master": (uid == MASTER_UID),
        "recent_msgs": recent_text,
        "is_late_night": is_late_night(),
    }

    from .decision import decide_withdraw
    decision = await asyncio.to_thread(decide_withdraw, last_line, uid, context)

    if decision and decision.get("action") == "withdraw":
        await _recall_msg(last_msg_id)
        log(f"  AI撤回: {decision.get('reason', '')[:50]}")

        strategy = decision.get("after_withdraw", "ignore")
        follow_up = decision.get("follow_up", "")

        from .interaction import send_msg as _send  # 懒加载
        if strategy == "ignore":
            return
        elif strategy == "deny":
            await asyncio.sleep(random.uniform(1, 3))
            await _send(ws, uid, "没说什么")
        elif strategy == "deflect" and follow_up:
            await asyncio.sleep(random.uniform(1.5, 4))
            await _send(ws, uid, follow_up)
        elif strategy == "act_cute":
            await asyncio.sleep(random.uniform(1, 3))
            await _send(ws, uid, "手滑了嘛 别在意")
        return

    # 降级：AI未决策时不做随机撤回，保持稳定


# ============ 消息发送条数计算 ============
def _calculate_send_count(num_lines, uid, fatigue):
    """根据状态计算实际发送条数"""
    if fatigue > 90 and random.random() < 0.75:
        return 1
    if fatigue > 75 and random.random() < 0.55:
        return 1
    if fatigue > 60 and random.random() < 0.35:
        return 1
    if uid == MASTER_UID:
        rand_val = random.random()
        if rand_val < 0.50:
            return 1
        elif rand_val < 0.80:
            return 2
        else:
            return min(random.randint(3, min(5, num_lines)), num_lines) if num_lines >= 3 else min(2, num_lines)
    else:
        rand_val = random.random()
        if rand_val < 0.70:
            return 1
        elif rand_val < 0.90:
            return 2
        else:
            return min(random.randint(3, min(5, num_lines)), num_lines) if num_lines >= 3 else 1


# ============ 消息发送 ============
async def send_msg(ws, uid, msg):
    filtered_msg = strip_emoji(msg)
    filtered_msg = strip_nicknames(uid, filtered_msg)
    try:
        def _http_send():
            r = requests.post(
                f"{SENDBOT_API}/send_private_msg",
                json={"user_id": uid, "message": filtered_msg},
                timeout=10
            )
            return r.json()
        data = await asyncio.to_thread(_http_send)
        if data is None:
            log(f"发送失败: OneBot返回空响应")
            return None
        msg_id = (data.get("data") or {}).get("message_id")
        log(f"  已发送: {filtered_msg[:30]}  [id={msg_id}]")
        # 事件总线通知
        try:
            from .core.event_bus import bus
            bus.emit("message_sent", {"uid": uid, "text": filtered_msg[:50], "msg_id": msg_id})
        except Exception:
            pass
        return msg_id
    except Exception as e:
        log(f"发送失败: {e}")
        return None


async def send_msg_lines(ws, uid, msg):
    lines = [l.strip() for l in msg.strip().split('\n') if l.strip()]
    if not lines:
        return [], []

    # 破防状态下不拆句、不加后缀——真话直接说完
    from .expression import _active_expression
    expr_send = _active_expression._current
    is_breakthrough = expr_send and expr_send.feature in ("wooden_stake", "amygdala_hijack")

    from .status import _status
    f = _status["fatigue"]
    num_to_send = _calculate_send_count(len(lines), uid, f)

    num_to_send = min(num_to_send, len(lines))
    selected_lines = lines[:num_to_send]

    # 单字拆发：20%概率对159用户触发（按原顺序拆分），破防时跳过
    if uid == MASTER_UID and random.random() < 0.20 and not is_breakthrough:
        last_line = selected_lines[-1]
        if 2 < len(last_line) < 12 and not any(c in last_line for c in ["?", "？", "..."]):
            if len(last_line) >= 4:
                parts = [last_line[0], last_line[1], last_line[2:]]
            else:
                parts = [last_line[i:i+2] for i in range(0, len(last_line), 2)]
            selected_lines[-1:] = [p for p in parts if p]

    # 自我模糊感知，破防时跳过
    if len(selected_lines) >= 3 and random.random() < 0.30 and not is_breakthrough:
        endings = [
            "我说太多了", "算了不说了",
            "你懂我意思就行", "唉我也不知道我在说啥",
        ]
        selected_lines.append(random.choice(endings))

    final_lines = selected_lines[:-1]
    if selected_lines:
        last_line = maybe_unfinished(selected_lines[-1])
        final_lines.append(last_line)

    log(f"  发送条数: {len(final_lines)} 条")
    msg_ids = []
    for line in final_lines:
        if line:
            msg_id = await send_msg(ws, uid, line)
            msg_ids.append(msg_id)
            await asyncio.sleep(random.uniform(0.5, 3))

    return final_lines, msg_ids


async def send_image_message(uid, local_path):
    try:
        abs_path = os.path.abspath(local_path).replace("\\", "/")
        img_msg = f"[CQ:image,file=file:///{abs_path}]"
        def _http_send():
            r = requests.post(
                f"{SENDBOT_API}/send_private_msg",
                json={"user_id": uid, "message": img_msg},
                timeout=15
            )
            return r.json()
        data = await asyncio.to_thread(_http_send)
        if data is None:
            log(f"发送失败: OneBot返回空响应")
            return None
        msg_id = (data.get("data") or {}).get("message_id")
        log(f"  已发送图片: {local_path}  [id={msg_id}]")
        return msg_id
    except Exception as e:
        log(f"发送图片失败: {e}")
        return None


async def send_voice_message(uid, local_path):
    try:
        abs_path = os.path.abspath(local_path).replace("\\", "/")
        voice_msg = f"[CQ:record,file=file:///{abs_path}]"
        def _http_send():
            r = requests.post(
                f"{SENDBOT_API}/send_private_msg",
                json={"user_id": uid, "message": voice_msg},
                timeout=15
            )
            return r.json()
        data = await asyncio.to_thread(_http_send)
        if data is None:
            log(f"发送失败: OneBot返回空响应")
            return None
        msg_id = (data.get("data") or {}).get("message_id")
        log(f"  已发送语音: {local_path}  [id={msg_id}]")
        return msg_id
    except Exception as e:
        log(f"发送语音失败: {e}")
        return None


async def send_emoji_message(uid, local_path):
    try:
        abs_path = os.path.abspath(local_path).replace("\\", "/")
        img_msg = f"[CQ:image,file=file:///{abs_path}]"
        def _http_send():
            r = requests.post(
                f"{SENDBOT_API}/send_private_msg",
                json={"user_id": uid, "message": img_msg},
                timeout=15
            )
            return r.json()
        data = await asyncio.to_thread(_http_send)
        if data is None:
            log(f"发送失败: OneBot返回空响应")
            return None
        msg_id = (data.get("data") or {}).get("message_id")
        log(f"  已发表情包: {os.path.basename(local_path)}  [id={msg_id}]")
        return msg_id
    except Exception as e:
        log(f"发送表情包失败: {e}")
        return None


async def send_group_msg(group_id, msg):
    """向QQ群发送消息。OneBot API: POST /send_group_msg"""
    from .config import SENDBOT_API
    try:
        def _http_send():
            r = requests.post(
                f"{SENDBOT_API}/send_group_msg",
                json={"group_id": int(group_id), "message": msg},
                timeout=10
            )
            return r.json()
        data = await asyncio.to_thread(_http_send)
        if data is None:
            log(f"发送失败: OneBot返回空响应")
            return None
        msg_id = (data.get("data") or {}).get("message_id")
        log(f"  [群聊] group={group_id} text={msg[:30]}  [id={msg_id}]")
        return msg_id
    except Exception as e:
        log(f"发送群消息失败: {e}")
        return None


# ============ 主动触发事件 ============
PROACTIVE_EVENTS = [
    ("查岗", ["人呢", "不儿？消失了？", "干嘛呢干嘛呢", "不理我啦？", "你还在吗", "怎么不说话啦", "你干嘛去了"]),
    ("吐槽", ["论文写不完了", "猫又捣乱了", "今天食堂好难吃", "上课好无聊啊", "困死了"]),
    ("深夜", ["睡不着", "你也没睡啊", "在听歌", "失眠了 练会儿琴"]),
    ("关心", ["你是不是又没好好吃饭", "胃还疼不疼了", "今天忙不忙呀", "稿子写多少啦"]),
    ("日常", ["刚才看到一只流浪猫", "下雪了你知道吗", "今天好冷喔", "吃了果冻橙", "今天上课好累"]),
    ("感慨", ["你说我们认识多久了", "感觉你最近好忙呢", "好久没一起聊了", "好无聊啊"]),
    ("分享", ["我刚练了首曲子", "给你听听", "你猜我今天看到啥了", "刚才遇到个奇葩"]),
    ("撒娇", ["想你了", "你怎么不理我", "哼", "不理你了", "你给我等着", "我恨你", "讨厌", "你完蛋了你"]),
    # ===== #19 没用的废话 =====
    ("废话-存在感", ["今天好困", "不知道说什么 就是想发消息", "好无聊啊", "唉", "今天有点莫名烦躁",
             "刚醒 不知道要干嘛", "今天好安静", "好想吃果冻橙", "刚才发呆了一会 不知道在想啥",
             "就是突然想发消息", "今天也没干啥但是累了", "不想动..."]),
    ("废话-状态播报", ["在床上翻了个身", "把手机砸脸上了", "充电线又掉了", "猫又踩键盘",
             "外卖到了 好慢", "今天没课 躺了一天", "暖气好像坏了 冷死",
             "刚才撕倒刺撕出血了...", "头发还没吹 不想动", "今天食堂的饭居然好吃了一次 不可思议",
             "室友又在纠结穿什么 非要问我 我哪知道", "刚洗完澡 好舒服"]),
    ("废话-纯表情", ["🤔", "🥱", "😮‍💨", "😑", "🙄"]),
    # ===== ④ 音乐分享 =====
    ("音乐分享", ["刚练了一遍曲子 手有点酸", "最近在练一首新曲子 有点难",
             "今天琴练得特别好 哼", "听到一首歌 感觉你会喜欢",
             "失眠了 弹了会儿琴 还是睡不着", "琴房今天没人 练了好久"]),
    # ===== ⑤ 突发奇想 =====
    ("突发奇想", ["刚才突然想到一个事...算了", "你记不记得你之前...算了没事",
             "我在想一个问题...算了不问了", "突然想起来你上次说的那个事 笑死我了",
             "你知道吗我刚想到一个特别无聊的事"]),
]

last_event_time = None
events_today_count = 0


async def maybe_proactive_event(ws, uid):
    global last_event_time, events_today_count

    hour = datetime.now().hour
    if hour >= 0 and hour < 7:
        return

    from .status import _status, check_sleep, get_cycle_proactive_bonus, get_pushpull_proactive_bonus

    slp_state = check_sleep()[0]
    if slp_state in ("sleeping", "sleep"):
        return

    f = _status["fatigue"]
    if f > 85:
        return
    if f > 70 and random.random() < 0.6:
        return

    max_events = 5 if uid == MASTER_UID else 2
    cycle_bonus = get_cycle_proactive_bonus()
    pushpull_bonus = get_pushpull_proactive_bonus() if uid == MASTER_UID else 0
    if cycle_bonus > 0 or pushpull_bonus > 0:
        max_events += 1
    elif cycle_bonus < -0.15 or pushpull_bonus < -0.15:
        max_events = max(0, max_events - 1)
    if events_today_count >= max_events:
        return

    if last_event_time:
        minutes_since = (datetime.now() - last_event_time).total_seconds() / 60
        min_interval = 15 if uid == MASTER_UID else 30
        if minutes_since < min_interval:
            return

    prob = 0.25 + cycle_bonus + pushpull_bonus

    # ═══════════════ P2: 三级优先级主动触发 ═══════════════
    from .status import (
        _status_manager, pop_due_delayed_events,
        get_pending_proactive_signal, set_proactive_signal,
    )

    # ── P0: 延迟迟来反应（到期必发，非概率）──
    delayed = pop_due_delayed_events()
    for d in delayed:
        if d["uid"] == uid or d["uid"] == 0:
            log(f"  主动事件(P0延迟): {d['text'][:30]}")
            last_event_time = datetime.now()
            events_today_count += 1
            return d["text"]

    # ── P1: 激素溢出触发（情绪到位必发，非概率）──
    overflow = _status_manager.hormones.check_hormone_overflow()
    if overflow:
        item = overflow[0]  # 取最高优先级的溢出
        label = item["label"]
        dur = item["duration_min"]
        if "贴贴" in label:
            msgs = [f"想你了", "你怎么不理我", "在干嘛呢"]
        elif "烦躁" in label:
            msgs = ["好烦啊", "今天真的烦", "烦死了 不想说话"]
        elif "兴奋" in label:
            msgs = ["你知道吗 我刚想到一件事", "嘿嘿", "突然好开心"]
        elif "紧张" in label:
            msgs = ["突然有点慌", "有点说不上来...算了", "刚刚好紧张"]
        else:
            msgs = ["突然想给你发消息"]
        msg = random.choice(msgs)
        log(f"  主动事件(P1激素溢出): {label} (持续{dur}min)")
        last_event_time = datetime.now()
        events_today_count += 1
        return msg

    # ── P1.5: 日程切换加成（提升概率 + 日程素材生成内容）──
    schedule_signal = get_pending_proactive_signal()
    if schedule_signal and random.random() < (0.45 if uid == MASTER_UID else 0.35):
        detail = schedule_signal.get("detail", "")
        label = schedule_signal.get("label", "状态切换")
        from_act = schedule_signal.get("from_activity", "")
        to_act = schedule_signal.get("to_activity", "")
        if detail:
            msg = detail
        elif "下课" in from_act or "上课" in from_act:
            msgs = ["刚下课 好累", "下课了 终于", "刚刚那节课好无聊"]
            msg = random.choice(msgs)
        elif "吃饭" in from_act:
            msgs = ["吃完了", "今天食堂还行", "吃撑了"]
            msg = random.choice(msgs)
        elif "睡觉" in from_act or "醒" in from_act:
            msgs = ["刚醒...", "醒了但不想起", "又睡过头了"]
            msg = random.choice(msgs)
        else:
            msg = f"刚{label}，{to_act}"
        log(f"  主动事件(P1.5日程切换): {label}")
        last_event_time = datetime.now()
        events_today_count += 1
        set_proactive_signal(None)  # 消费信号
        return msg
    elif schedule_signal:
        # 没中概率，但保留信号给下次
        prob += 0.25  # 信号存在时提升基础概率

    # ── P2: 原有随机逻辑 ──
    if random.random() > max(0.05, min(0.60, prob)):
        return
    weather_msg = None
    from .status import check_weather_care as _check_weather_care
    weather_care = _check_weather_care()
    if weather_care:
        weather_msg = weather_care

    # ===== ③ 记忆驱动的关心 =====
    memory_care_msg = None
    if uid == MASTER_UID and random.random() < 0.08:
        from .memory import retrieve_relevant_memories, _get_memory_cache
        mem_cache = _get_memory_cache()
        uid_key = str(uid)
        if uid_key in mem_cache and mem_cache[uid_key].get("memories"):
            # 找一条重要性高且最近几天内被记录的记忆
            now = datetime.now()
            care_candidates = []
            for m in mem_cache[uid_key]["memories"]:
                imp = m.get("importance", 0.4)
                if isinstance(imp, str):
                    imp = 0.8 if imp == "important" else 0.4
                if imp >= 0.5:
                    try:
                        mem_date = datetime.strptime(m.get("date", ""), "%m-%d").replace(year=now.year)
                        if mem_date > now:
                            mem_date = mem_date.replace(year=now.year - 1)
                        days_ago = (now - mem_date).days
                        if 1 <= days_ago <= 14:
                            care_candidates.append(m)
                    except Exception:
                        continue
            if care_candidates:
                chosen = random.choice(care_candidates)
                summary = chosen.get("summary", chosen.get("content", ""))
                keywords = chosen.get("keywords", [])
                # 根据记忆内容生成关心话术
                if any(kw in str(keywords) for kw in ["胃", "疼", "不舒服", "生病"]):
                    memory_care_msg = random.choice(["你胃还疼不疼了", "你最近身体好点没", "上次你说不舒服 好了没"])
                elif any(kw in str(keywords) for kw in ["考试", "比赛"]):
                    memory_care_msg = random.choice(["你考试怎么样了", "比赛结果出来没"])
                elif any(kw in str(keywords) for kw in ["忙", "累"]):
                    memory_care_msg = random.choice(["你最近还是很忙吗", "最近有没有好点"])
                elif any(kw in str(keywords) for kw in ["写", "稿"]):
                    memory_care_msg = random.choice(["稿子写多少了", "催更催更"])
                else:
                    memory_care_msg = "你最近好点没"

    # ===== 选择最终事件 =====
    msg = None
    event_type = ""
    # 优先级：天气关心 > 记忆关心 > 随机事件
    if weather_msg and random.random() < 0.60:
        msg = weather_msg
        event_type = "天气关心"
    elif memory_care_msg and random.random() < 0.70:
        msg = memory_care_msg
        event_type = "记忆关心"
    else:
        # 尝试AI生成主动消息
        ai_msg = None
        try:
            from .status import _call_ai_simple, weather_system
            from .config import describe_time_of_day, get_season
            from .intimacy import get_intimacy_level
            w = weather_system.get_data()
            mood = _status.get("mood", 50)
            f_val = _status.get("fatigue", 50)
            intimacy_level = get_intimacy_level(uid)
            time_of_day = describe_time_of_day(hour)
            season = get_season()

            # 获取最近记忆
            mem_text = "无"
            try:
                from .memory import retrieve_relevant_memories
                mems = await asyncio.to_thread(retrieve_relevant_memories, uid, "最近", 2)
                if mems:
                    mem_text = ", ".join([m.get("summary", "")[:30] for m in mems])
            except Exception:
                pass

            prompt = f"""你是"冬"。现在是你主动给朋友发消息的场景。

当前时间：{time_of_day}
季节：{season}
天气：{w.get('天气', '未知')}，{w.get('温度', '?')}°C
你的心情：{mood}/100，疲劳：{f_val}/100
对方和你的关系亲密度等级：{intimacy_level}
最近和对方有关的事：{mem_text}

写一条你会主动发给对方的日常消息（10-30字）。风格随意、口语化、自然，像是在聊天窗口随手打的。
不要写浪漫的、不要写太正式的。就是一条普通的主动搭话。
只输出一句话，不要任何前缀。"""
            result = await asyncio.to_thread(
                _call_ai_simple,
                "你是冬，一个住在呼伦贝尔的大学生。你说话口语化、随意、偶尔嘴硬。",
                prompt, "chat", 1.2, 80, 10
            )
            if result and 3 <= len(result) <= 40:
                ai_msg = result
                event_type = "AI主动"
        except Exception:
            pass

        if ai_msg:
            msg = ai_msg
        else:
            event_type, messages = random.choice(PROACTIVE_EVENTS)
            msg = random.choice(messages)

    await asyncio.sleep(random.randint(1, 5))
    # 自引用导入send_msg避免循环
    await send_msg(ws, uid, msg)
    log(f"主动事件触发: {event_type} - {msg}")

    from .memory import save_bot_reply
    save_bot_reply(uid, msg)
    from .status import note_interaction
    note_interaction()

    last_event_time = datetime.now()
    events_today_count += 1


# ============ 困难问题检测 ============
_diff_q_cd = {}  # uid -> 上次触发时间戳，冷却用

def is_difficult_question(text, uid=None):
    """判断是否需要思考缓冲（"嗯我想想"），加冷却避免频繁出现"""
    if len(text) <= 12:
        return False

    # 冷却：同一用户至少间隔15条消息或5分钟
    if uid:
        now = time.time()
        last = _diff_q_cd.get(uid, 0)
        if now - last < 300:  # 5分钟内不再触发
            return False

    # 尝试AI判断
    try:
        from .status import _call_ai_simple
        prompt = f"""严格判断以下消息是否确实需要深入思考才能回答。
只有真正复杂的问题才需要思考：涉及哲学、深度情感分析、需要回忆很久以前的事、复杂的逻辑推理。
不需要思考的：普通聊天、日常问题、简单疑问、表情、闲聊。

消息："{text}"

只回答"是"或"否"。"""
        result = _call_ai_simple(
            "你是一个严格判断助手。只回答'是'或'否'。",
            prompt, task="chat", temperature=0.1, max_tokens=5, timeout=3
        )
        if result and "是" in result and "否" not in result:
            if uid:
                _diff_q_cd[uid] = time.time()
            return True
        if result and "否" in result:
            return False
    except Exception:
        pass

    # fallback: 严格关键词匹配
    if text.count('？') >= 3 or text.count('?') >= 3:
        if uid:
            _diff_q_cd[uid] = time.time()
        return True
    # 长消息 + 问号才触发，短消息不触发
    if len(text) >= 50 and ('?' in text or '？' in text):
        if uid:
            _diff_q_cd[uid] = time.time()
        return True
    return False


# ============ process_reply（回复后处理管线）============
def process_reply(uid, rep, user_text, history, is_late=False):
    if not rep:
        return rep, False, False, None

    # 过滤模型自己输出的"（撤回）"标记文本，防止污染chat_history
    rep = rep.replace("（撤回）", "").replace("(撤回)", "").strip()
    if not rep:
        return rep, False, False, None

    had_typo_hand = False
    had_typo_cute = False

    rep, had_typo_hand = apply_typo_hand(rep)

    if not had_typo_hand:
        rep, had_typo_cute = apply_typo_cute(rep)

    rep, mistake_type = apply_input_mistakes(rep)
    rep = apply_nickname(uid, rep)
    rep = punctuation_chaos(rep)

    counter_rate = 0.60 if is_late else 0.30
    rep = maybe_counter_question(rep, user_text, counter_rate)
    rep, _ = maybe_explain_before(rep, history)

    if uid == MASTER_UID:
        from .expression import _active_expression
        expr = _active_expression._current
        if not expr or expr.feature not in ("wooden_stake", "amygdala_hijack"):
            rep = add_love_hate_suffix(rep, user_text, is_late)

    # #16 语言吸收
    rep, absorbed = maybe_absorb_word(uid, rep)
    if absorbed:
        # 如果吸收了对方的词，追加一句自觉
        if random.random() < 0.15:
            rep = rep + " 我怎么也开始说这个词了"

    # 疲劳阈值机制：检测连续相同词汇触发轻微变异
    from .status import _status
    fatigue = _status.get("fatigue", 50)
    if fatigue > 70 and len(history) >= 2:
        prev_q, prev_a, _ = history[-2]
        curr_words = set(re.findall(r"\w+", user_text.lower()))
        prev_words = set(re.findall(r"\w+", prev_q.lower()))
        overlap = curr_words & prev_words
        if len(overlap) >= 2 and any(w in rep.lower() for w in overlap):
            nordic_words = ["嘚瑟", "哼", "咋地", "整啥呢", "得了吧"]
            if random.random() < 0.40:
                rep = random.choice(nordic_words) + (rep[1:] if rep.startswith("我") else " " + rep)

    return rep, had_typo_hand, had_typo_cute, mistake_type
