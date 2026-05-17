"""
冬的休眠守护模块

职责：
  1. 启动时AI判断睡眠状态（Item 1：启动时自动检测睡眠时间）
  2. 休眠时紧急唤醒双通道判断（Item 3v2：双通道判断机制）
  3. 休眠时消息积压队列

架构：
  - 判断通道独立于冬完整人格（不走 system prompt / 记忆 / 表达规则）
  - analysis API, max_tokens=10, temperature=0, A/B/C 三选一
  - 预筛层：白名单(MASTER_UID + 高亲密度) + 关键词
  - A/B → 简化唤醒 prompt + 迷糊回复 → 据激素决定继续睡/失眠
  - C → 积压队列等自然醒
"""
import os
import json
import time
import random
from datetime import datetime
from typing import Optional, List, Dict, Tuple

QUEUE_FILE = os.path.join(os.path.dirname(__file__), "dong_sleep_queue.json")

# 模块级标志：判断通道调过的次数（防抖：同一 uid 同一分钟不重复判断）
_last_judge: Dict[str, float] = {}  # key: f"{uid}:{hour}:{minute//5}", val: last_result


# ── 预筛层 ───────────────────────────────────────────

# 紧急唤醒关键词（命中任一 → 送入判断通道）
_WAKE_KEYWORDS = [
    # 情绪/状态
    "睡不着", "失眠", "难受", "不开心", "哭了",
    "我想你", "想你了", "想你",
    "救命", "急", "紧急",
    # 主号的特殊触发
    "在吗", "睡了吗", "醒着吗",
    # 冬在意的事
    "许嵩", "新歌", "新专辑",
    "猫", "小猫", "我家猫",
]


def _pre_filter(uid: int, text: str, intimacy: float = 0.0) -> bool:
    """预筛层：白名单 + 关键词 + 亲密度
    返回 True = 值得送入判断通道
    """
    from .config import MASTER_UID

    # 主号永远预筛通过
    if uid == MASTER_UID:
        return True

    # 高亲密度（>= 60）且不是纯表情/图片
    if intimacy >= 60 and len(text.strip()) >= 3:
        return True

    # 关键词命中
    text_lower = text.lower()
    for kw in _WAKE_KEYWORDS:
        if kw in text_lower:
            return True

    return False


# ── 判断通道 ─────────────────────────────────────────

_JUDGE_PROMPT = """你是冬的睡眠守护器。冬正在睡觉。
以下事件发生了：
- 发消息的人：{uid}
- 消息内容：{text}
- 当前时间：{time_str}
- 冬的激素状态：疲劳{fa} 情绪{mo}

冬是一个在意许嵩、在意主号深夜联系、不太在意普通闲聊的女大学生。

这个事件值得叫醒冬吗？
A. 必须叫醒 — 不醒会后悔
B. 可以醒 — 有点在意但不是紧急
C. 继续睡 — 醒了也不会回

只回答A/B/C。"""


def _judge_message(uid: int, text: str) -> str:
    """判断通道：调用 analysis API（max_tokens=10, temperature=0）
    返回 'A' | 'B' | 'C'，失败返回 'C'
    """
    from .status import _call_ai_simple

    now = datetime.now()
    time_str = now.strftime("%H:%M")

    # 防抖：同一 uid + 同一 5 分钟窗口内不重复判断
    key = f"{uid}:{now.hour}:{now.minute // 5}"
    if key in _last_judge:
        return _last_judge[key]

    # 读取当前激素
    try:
        from .status import _status
        fa = _status.get("fatigue", 50)
        mo = _status.get("mood", 50)
    except Exception:
        fa, mo = 50, 50

    user_prompt = _JUDGE_PROMPT.format(
        uid=uid, text=text[:80], time_str=time_str, fa=fa, mo=mo
    )

    try:
        result = _call_ai_simple(
            "你是冬的睡眠守护器。只回答A/B/C一个字母，不要任何额外文字。",
            user_prompt,
            task="analysis",
            temperature=0,
            max_tokens=10,
            timeout=8,
        )
        if result and len(result) >= 1:
            answer = result.strip()[0].upper()
            if answer in ("A", "B", "C"):
                _last_judge[key] = answer
                return answer
    except Exception:
        pass

    _last_judge[key] = "C"
    return "C"


# ── 简化唤醒 prompt ─────────────────────────────────

def _build_wake_prompt() -> str:
    """构建冬被临时唤醒时的简化 system prompt
    不带记忆 / 表达规则 / 冲突追踪 / 出厂记忆 —— 只带最核心的身份和状态
    """
    from .config import LOADED_PERSONA
    from . import status as _st

    persona = LOADED_PERSONA or "你是冬。"

    parts = [persona]

    # 心境（最简版）
    try:
        hormones = _st.get_hormone_snapshot()
        hour = datetime.now().hour
        current_vec, _, _ = _st.compute_mood_vector(
            hormones, 5.0, hour=hour
        )
        mood_text = _st.mood_vector_to_text(current_vec)
        if mood_text:
            parts.append(mood_text)
    except Exception:
        pass

    parts.append(
        "你刚刚被叫醒了。你还没完全清醒，脑子迷迷糊糊的。"
        "回复要短，要带点刚醒的迷糊感。语气比平时更软、更没防备。"
        "可以说'嗯...'、'刚醒...'这类话。不要长篇大论。"
        f"现在是{datetime.now().strftime('%H:%M')}。"
    )

    return "\n\n".join(parts)


# ── 启动时睡眠检测（Item 1）────────────────────────

_STARTUP_CHECK_PROMPT = """你是冬。你刚刚重启/恢复了。
请判断你此刻应该处于什么状态：
- 当前时间：{time_str}
- 是否深夜：{is_late}
- 你的激素状态：疲劳{fa} 情绪{mo}
- 之前是否在睡觉：{was_sleeping}
- 中断时长：约{interruption_minutes}分钟
- 刚才是否在聊天：{was_chatting}

规则：
- 如果只是中断了十几二十分钟且刚才正在聊天 → 先上线说一句告别的话再睡
- 如果中断时间较长且处于深夜 → 直接继续睡
- 如果不在睡眠时段 → 正常苏醒
- 如果白天且中断很短 → 正常苏醒

只回答一个词：sleep（继续睡）、goodbye_then_sleep（告别后睡）、awake（正常苏醒）"""


def startup_sleep_check(
    was_sleeping: bool,
    was_chatting: bool,
    interruption_minutes: float,
) -> str:
    """启动时AI判断睡眠状态
    返回 'sleep' | 'goodbye_then_sleep' | 'awake'
    """
    from .status import _call_ai_simple
    from .config import is_late_night

    now = datetime.now()
    time_str = now.strftime("%H:%M")
    is_late = is_late_night()

    # 快速硬规则：白天 + 不是睡觉状态 → 直接苏醒（省一次API）
    if not is_late and not was_sleeping and interruption_minutes < 120:
        return "awake"

    # 快速硬规则：深夜 + 不是睡觉状态 + 中断短 + 在聊天 → 告别后睡
    if is_late and not was_sleeping and was_chatting and interruption_minutes < 30:
        return "goodbye_then_sleep"

    # 快速硬规则：深夜 + 睡觉状态 + 中断长 → 继续睡
    if is_late and was_sleeping and interruption_minutes > 60:
        return "sleep"

    # 处于灰色地带 → AI 判断
    try:
        from .status import _status
        fa = _status.get("fatigue", 50)
        mo = _status.get("mood", 50)
    except Exception:
        fa, mo = 50, 50

    prompt = _STARTUP_CHECK_PROMPT.format(
        time_str=time_str,
        is_late="是" if is_late else "否",
        fa=fa, mo=mo,
        was_sleeping="是" if was_sleeping else "否",
        interruption_minutes=int(interruption_minutes),
        was_chatting="是" if was_chatting else "否",
    )

    try:
        result = _call_ai_simple(
            "你是冬。只回答一个词。",
            prompt,
            task="analysis",
            temperature=0,
            max_tokens=10,
            timeout=8,
        )
        if result:
            result = result.strip().lower()
            if "goodbye" in result or "告别" in result:
                return "goodbye_then_sleep"
            if "sleep" in result or "睡" in result:
                return "sleep"
            if "awake" in result or "醒" in result:
                return "awake"
    except Exception:
        pass

    # fallback：白天醒，深夜睡
    return "awake" if not is_late else "sleep"


# ── 消息积压队列 ────────────────────────────────────

def _load_queue() -> List[dict]:
    try:
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
    return []


def _save_queue(queue: List[dict]):
    try:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
    except IOError:
        pass


def enqueue_message(uid: int, text: str):
    """休眠期间将消息存入积压队列"""
    queue = _load_queue()
    queue.append({
        "uid": uid,
        "text": text[:200],
        "received_at": time.time(),
    })
    # 最多保留 50 条
    if len(queue) > 50:
        queue = queue[-50:]
    _save_queue(queue)


def get_queued_messages() -> List[dict]:
    """苏醒后读取积压消息队列"""
    return _load_queue()


def clear_queue():
    """苏醒后清空积压队列"""
    _save_queue([])


# ── 主入口：休眠时处理入站消息 ──────────────────────

def handle_message_during_sleep(uid: int, text: str) -> Optional[str]:
    """休眠时处理一条入站消息
    返回:
        None   → 继续睡（消息已入队）
        "wake" → 临时苏醒（调用方需生成回复）
    """
    from .status import _status

    # 读取亲密度
    intimacy = _status.get("intimacy", {}).get(str(uid), 0) if "intimacy" in _status else 0

    # 1. 预筛
    if not _pre_filter(uid, text, intimacy):
        enqueue_message(uid, text)
        return None

    # 2. 判断通道
    decision = _judge_message(uid, text)

    if decision == "C":
        enqueue_message(uid, text)
        return None

    # A 或 B → 临时苏醒
    return "wake"


def get_wake_reply_prompt() -> str:
    """返回临时苏醒时的系统 prompt"""
    return _build_wake_prompt()
