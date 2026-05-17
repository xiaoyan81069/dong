"""
冬 · 状态系统 — 心境流内核
核心：心境向量、驱动计算、mood_vector_to_text
"""
import json
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from ..config import MEMORY_FILE, MASTER_UID
from ..log import log


# ============ 共享 AI 调用辅助 ============
def _call_ai_simple(sys_prompt: str, user_prompt: str, task: str = "chat",
                     temperature: float = 0.5, max_tokens: int = 150,
                     timeout: int = 10):
    """简化AI调用——通过统一网关。失败返回None，调用方负责fallback。"""
    from ..core.api_gateway import gateway
    return gateway.call_simple(
        sys_prompt, user_prompt,
        task=task,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


# ============ 心境流内核 (MoodFlowKernel) ============
# 模块级变量 —— 仅 asyncio 协程内读写（_build_system_prompt 调用链）
# WebSocket 线程只收消息入队列，不触碰这些变量，因此无竞态。
_current_mood_vec = None        # 当前心境向量 dict {8维: float}
_expected_mood_vec = None       # 预期心境向量 dict
_last_mood_update_ts = 0.0      # 上次心境更新时间戳
_stream_cache = ""              # 体验流缓存（最近1条，≤30字）
_last_stream_update_ts = 0.0    # 上次体验流更新时间戳


def _update_expected_vector(current: Dict[str, float], expected: Dict[str, float],
                            dt_minutes: float) -> Dict[str, float]:
    """预期向量 = 指数平滑 + 激素驱动维度的半衰期衰减"""
    alpha = 0.4 if dt_minutes < 30 else 0.2

    # 只有受激素直接驱动的维度才衰减
    HALF_LIVES = {"defensiveness": 240, "social_approach": 180, "arousal": 60}

    new_expected = {}
    for dim in expected:
        smooth_val = alpha * current.get(dim, 0.5) + (1 - alpha) * expected.get(dim, 0.5)
        half_life = HALF_LIVES.get(dim, float("inf"))
        if half_life > 0:
            decay = 0.5 ** (dt_minutes / half_life)
            smooth_val *= decay
        new_expected[dim] = max(0.0, min(1.0, smooth_val))

    return new_expected


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def compute_drives(uid: Optional[int] = None) -> Dict[str, float]:
    """
    计算内驱力三元组。

    - social_thirst: 社交渴求，近6小时与主号互动越少→越高
    - curiosity: 好奇心，近24小时新记忆越少→越高
    - unfinished_business: 未完成事件，固定0.3（记忆图谱暂未实现）

    返回 {social_thirst, curiosity, unfinished_business}，全部 0-1。
    """
    # === social_thirst: 近6小时与主号的互动次数 ===
    social_thirst = 0.5
    try:
        from ..memory import conversation_history
        target_uid = uid if uid is not None else MASTER_UID
        history = conversation_history.get(target_uid, [])
        now = datetime.now()
        six_hours_ago = now - timedelta(hours=6)
        recent_count = sum(1 for entry in history if isinstance(entry, tuple) and len(entry) >= 3 and entry[2] >= six_hours_ago)
        social_thirst = 1.0 - min(1.0, recent_count / 3.0) * 0.5
        social_thirst = _clamp(social_thirst)
    except Exception:
        pass

    # === curiosity: 近24小时新增记忆条目数 ===
    curiosity = 0.5
    try:
        if os.path.exists(MEMORY_FILE):
            file_mtime = os.path.getmtime(MEMORY_FILE)
            hours_since_mod = (time.time() - file_mtime) / 3600.0
            if hours_since_mod < 24:
                try:
                    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    total = sum(
                        len(v.get("memories", v)) if isinstance(v, dict)
                        else (len(v) if isinstance(v, list) else 0)
                        for v in data.values()
                    )
                    recent_ratio = min(1.0, 24.0 / max(hours_since_mod, 1.0))
                    new_estimate = max(0, int(total * recent_ratio * 0.3))
                except Exception:
                    new_estimate = 3
            else:
                new_estimate = 0
            curiosity = 1.0 - min(1.0, new_estimate / 5.0)
            curiosity = _clamp(curiosity)
    except Exception:
        pass

    return {
        "social_thirst": round(social_thirst, 3),
        "curiosity": round(curiosity, 3),
        "unfinished_business": 0.3,
    }


def compute_mood_vector(hormones: Dict[str, float], dt_minutes: float = 0,
                        drives: Optional[Dict[str, float]] = None,
                        hour: Optional[int] = None) -> Tuple[Dict[str, float], Dict[str, float], float]:
    """
    心境向量计算 —— 纯函数，0次API。

    Args:
        hormones: get_hormone_snapshot() 返回的 dict
        dt_minutes: 距上次心境更新的分钟数
        drives: 可选的内驱力 dict (social_thirst, curiosity, unfinished_business)
        hour: 当前小时（用于深夜调节）

    Returns:
        (current_vec, expected_vec, internal_surprise)
    """
    global _expected_mood_vec, _last_mood_update_ts

    d = hormones.get("dopamine", 50)
    a = hormones.get("adrenaline", 30)
    c = hormones.get("cortisol", 20)
    o = hormones.get("oxytocin", 50)
    s = hormones.get("serotonin", 60)

    if hour is None:
        hour = datetime.now().hour

    # === 8维初始计算 ===
    defensiveness = _clamp(c / 100.0)
    social_approach = _clamp((o - c * 0.5) / 100.0)

    current_vec = {
        "valence":           _clamp((d + s) / 200.0),
        "arousal":           _clamp((a + (100 - s)) / 200.0),
        "social_approach":   social_approach,
        "defensiveness":     defensiveness,
        "playfulness":       _clamp(d / 150.0),
        "nostalgia":         _clamp((o - d) / 100.0),
        "fatigue_weight":    _clamp(drives.get("fatigue", 50) / 100.0) if drives else 0.5,
        "filter_strength":   _clamp(defensiveness + (1 - social_approach) / 2.0, 0.1, 1.0),
    }

    # === 深夜调节 ===
    if hour >= 23 or hour < 5:
        current_vec["defensiveness"] = _clamp(current_vec["defensiveness"] - 0.3)
        current_vec["social_approach"] = _clamp(current_vec["social_approach"] + 0.2)
        current_vec["nostalgia"] = _clamp(current_vec["nostalgia"] + 0.2)

    # === 内驱力偏置 ===
    if drives:
        thirst = drives.get("social_thirst", 0.5)
        if thirst > 0.6:
            current_vec["defensiveness"] = _clamp(current_vec["defensiveness"] - 0.15)
            current_vec["social_approach"] = _clamp(current_vec["social_approach"] + 0.1)

    # === 预期向量: 指数平滑 + 衰减 ===
    if _expected_mood_vec is None:
        _expected_mood_vec = dict(current_vec)
    else:
        _expected_mood_vec = _update_expected_vector(current_vec, _expected_mood_vec, dt_minutes)

    # === 内部惊讶: 欧几里得距离 + 关键维度异常 ===
    internal_surprise = _compute_internal_surprise(current_vec, _expected_mood_vec)

    # 更新模块级变量
    _last_mood_update_ts = time.time()

    return current_vec, _expected_mood_vec, internal_surprise


def _compute_internal_surprise(current: Dict[str, float], expected: Dict[str, float]) -> float:
    """内部惊讶 = 整体偏移幅度 + 关键维度异常，纯规则，0次API"""
    key_dims = ["valence", "arousal", "social_approach", "defensiveness"]
    total_shift = sum((current.get(d, 0.5) - expected.get(d, 0.5)) ** 2 for d in key_dims) ** 0.5

    key_anomalies = sum(
        1 for d in ["valence", "social_approach"]
        if abs(current.get(d, 0.5) - expected.get(d, 0.5)) > 0.2
    )

    return min(1.0, total_shift * 2.0 + key_anomalies * 0.2)


def mood_vector_to_text(vec: Dict[str, float]) -> str:
    """
    心境向量 → 自然语言描述（3-5句中文，冬的口吻）。

    纯规则翻译，0次API。中间值(0.4-0.6)用模糊表达如"有点""好像"。
    """
    lines = []

    # defensiveness
    d = vec.get("defensiveness", 0.5)
    if d > 0.65:
        lines.append("你此刻防御心很强，话到嘴边会咽回去")
    elif d > 0.55:
        lines.append("你有点防备，话不会全说出来")

    # social_approach
    sa = vec.get("social_approach", 0.5)
    if sa > 0.65:
        lines.append("你其实想靠近他，但不习惯直接说")
    elif sa > 0.55:
        lines.append("你好像有点想搭理他")

    # defensiveness + social_approach 同时偏高 → 拧巴
    if d > 0.5 and sa > 0.5:
        lines.append("你想靠近但又怕受伤，话会有点拧巴")

    # nostalgia
    nos = vec.get("nostalgia", 0.5)
    if nos > 0.65:
        lines.append("你怀旧情绪很浓，脑子里闪回了很多过去的事")
    elif nos > 0.5:
        lines.append("你有点怀旧，脑子里闪过一些过去的事")

    # playfulness
    play = vec.get("playfulness", 0.5)
    if play > 0.65:
        lines.append("你很想逗他，手痒")
    elif play > 0.5:
        lines.append("你今天有点想逗他")

    # fatigue
    fat = vec.get("fatigue_weight", 0.5)
    if fat > 0.75:
        lines.append("你很累，回复可能只有一两个字")
    elif fat > 0.55:
        lines.append("你有点累，不想说太多话")

    # arousal
    ars = vec.get("arousal", 0.5)
    if ars > 0.75:
        lines.append("你很兴奋，坐不住那种")
    elif ars > 0.55:
        lines.append("你有点躁，说不上来")

    # valence
    val = vec.get("valence", 0.5)
    if val < 0.3:
        lines.append("你今天心情不太好")
    elif val > 0.75:
        lines.append("你今天心情很不错")

    # filter
    fil = vec.get("filter_strength", 0.5)
    if fil > 0.8:
        lines.append("你说话会自己在心里过一遍再说")

    if not lines:
        lines.append("你现在状态比较平稳")

    return "。".join(lines) + "。"