"""
后悔回顾模块

她选择了克制之后，并不意味着这件事就此结束。
几小时后甚至几天后，她可能会在某个安静的时刻忽然想起——
"其实那时候应该找他的"。

后悔不是对之前决策的否定，而是对决策之后情感变化的诚实记录。
"""
import os
import json
import time
import random
from datetime import datetime
from typing import Optional, List, Dict

REGRET_FILE = os.path.join(os.path.dirname(__file__), "dong_regret_log.json")

# 模块级状态
_last_regret_check: float = 0  # 上次检查时间戳


def _load_regrets() -> List[dict]:
    try:
        if os.path.exists(REGRET_FILE):
            with open(REGRET_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
    return []


def _save_regrets(regrets: List[dict]):
    try:
        # 最多保留 30 条
        if len(regrets) > 30:
            regrets = regrets[-30:]
        with open(REGRET_FILE, "w", encoding="utf-8") as f:
            json.dump(regrets, f, ensure_ascii=False, indent=2)
    except IOError:
        pass


def record_restraint(uid: int, context: str, reason: str = ""):
    """记录一次克制决策
    uid: 对方
    context: 当时的情境（如 "深夜想找他但忍住了"）
    reason: 为什么克制（如 "怕打扰他"）
    """
    regrets = _load_regrets()
    regrets.append({
        "uid": uid,
        "context": context[:100],
        "reason": reason[:60],
        "timestamp": time.time(),
        "reviewed": False,
    })
    _save_regrets(regrets)


_REGRET_PROMPT = """你是冬。你在回顾自己之前的一个决定：

当时的情境：{context}
你选择了克制，因为：{reason}
现在的时间：{time_str}
你当前的心情：{mood_desc}

你现在回想起这件事，心里有什么感觉？
用冬的口吻写一句话（≤20字），表达你此刻的真实感受。
可以是后悔、释然、想念、或者觉得"其实也没那么重要"。
不要评判自己。这只是诚实地记录。"""


def maybe_trigger_regret() -> Optional[str]:
    """在合适的时刻触发后悔回顾
    条件：深夜(23-05) 且 距上次检查>4小时 且 有未回顾的克制记录
    返回：冬的内心独白（可注入 system prompt），或 None
    """
    global _last_regret_check

    hour = datetime.now().hour
    if not (hour >= 23 or hour < 5):
        return None

    # 至少间隔 4 小时
    now_ts = time.time()
    if now_ts - _last_regret_check < 14400:
        return None

    # 概率触发（15%）
    if random.random() > 0.15:
        _last_regret_check = now_ts
        return None

    _last_regret_check = now_ts

    regrets = _load_regrets()
    unreviewed = [r for r in regrets if not r.get("reviewed", False)]
    if not unreviewed:
        return None

    # 随机选一条未回顾的
    entry = random.choice(unreviewed)
    entry["reviewed"] = True

    # 获取当前心境
    mood_desc = "平静"
    try:
        from .status import _status
        m = _status.get("mood", 50)
        if m > 80:
            mood_desc = "兴奋又有点感伤"
        elif m > 60:
            mood_desc = "还不错，但有点想念"
        elif m > 40:
            mood_desc = "一般般，心里有点空"
        elif m > 20:
            mood_desc = "有点低落"
        else:
            mood_desc = "心情不太好，容易多想"
    except Exception:
        pass

    # 调用 AI 生成回顾
    from .status import _call_ai_simple

    prompt = _REGRET_PROMPT.format(
        context=entry["context"],
        reason=entry.get("reason", "没想清楚"),
        time_str=datetime.now().strftime("%H:%M"),
        mood_desc=mood_desc,
    )

    try:
        result = _call_ai_simple(
            "你是冬。只输出一句话，≤20字。",
            prompt,
            task="analysis",
            temperature=0.6,
            max_tokens=30,
            timeout=8,
        )
        if result:
            _save_regrets(regrets)
            return result.strip()
    except Exception:
        pass

    _save_regrets(regrets)
    return None
