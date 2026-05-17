"""
冬 · 表演规则调度器

三个功能的触发检测 + 频控：
  1. 通感脑补 (body_feeling) — 对话缝隙中随口带出身体感受
  2. 有罪推定 (suspicion) — 被冷落时基于记忆举证式吃醋
  3. 木桩破防 (wooden_stake) — 嘴硬偶尔失败，真心话脱口→慌张否认

硬规则：零硬编码示例，代码只做触发检测。LLM 根据 persona 自行生成表达。
"""
import json
import os
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any

from .config import BASE_DIR, MASTER_UID, is_late_night
from .log import log

EXPRESSION_STATE_FILE = os.path.join(BASE_DIR, "dong_expression_state.json")


# ============ ExpressionState ============

@dataclass
class ExpressionState:
    last_body_feeling_time: Optional[float] = None
    body_feeling_cooldown: int = 300

    last_suspicion_time: Optional[float] = None
    suspicion_cooldown: int = 1800
    suspicion_today_count: int = 0
    suspicion_max_per_day: int = 3

    breakthrough_this_month: int = 0
    breakthrough_max_per_month: int = 3
    last_breakthrough_date: Optional[str] = None
    is_post_breakthrough: bool = False


def load_expression_state() -> ExpressionState:
    try:
        if os.path.exists(EXPRESSION_STATE_FILE):
            with open(EXPRESSION_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return ExpressionState(
                last_body_feeling_time=data.get("last_body_feeling_time"),
                body_feeling_cooldown=data.get("body_feeling_cooldown", 300),
                last_suspicion_time=data.get("last_suspicion_time"),
                suspicion_cooldown=data.get("suspicion_cooldown", 1800),
                suspicion_today_count=data.get("suspicion_today_count", 0),
                suspicion_max_per_day=data.get("suspicion_max_per_day", 3),
                breakthrough_this_month=data.get("breakthrough_this_month", 0),
                breakthrough_max_per_month=data.get("breakthrough_max_per_month", 3),
                last_breakthrough_date=data.get("last_breakthrough_date"),
                is_post_breakthrough=data.get("is_post_breakthrough", False),
            )
    except Exception:
        pass
    return ExpressionState()


def save_expression_state(state: ExpressionState):
    try:
        with open(EXPRESSION_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "last_body_feeling_time": state.last_body_feeling_time,
                "body_feeling_cooldown": state.body_feeling_cooldown,
                "last_suspicion_time": state.last_suspicion_time,
                "suspicion_cooldown": state.suspicion_cooldown,
                "suspicion_today_count": state.suspicion_today_count,
                "suspicion_max_per_day": state.suspicion_max_per_day,
                "breakthrough_this_month": state.breakthrough_this_month,
                "breakthrough_max_per_month": state.breakthrough_max_per_month,
                "last_breakthrough_date": state.last_breakthrough_date,
                "is_post_breakthrough": state.is_post_breakthrough,
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# 全局状态实例（模块加载时初始化）
_expr_state = load_expression_state()

# 跨模块访问的当前轮结果
@dataclass
class ExpressionResult:
    feature: str = "default"  # "body_feeling" | "suspicion" | "wooden_stake" | "default"
    prompt: str = ""
    params: dict = field(default_factory=dict)


class _ActiveExpressionHolder:
    _current: Optional[ExpressionResult] = None

    def reset(self):
        self._current = ExpressionResult()

_active_expression = _ActiveExpressionHolder()


# ============ 当天/当月重置 ============

def _reset_daily():
    global _expr_state
    today = datetime.now().strftime("%Y-%m-%d")
    # suspicion 每日上限重置
    _expr_state.suspicion_today_count = 0
    save_expression_state(_expr_state)
    log("[expression] 每日计数器已重置")


def _check_monthly_reset():
    global _expr_state
    now = datetime.now()
    month_key = now.strftime("%Y-%m")
    if _expr_state.last_breakthrough_date and _expr_state.last_breakthrough_date.startswith(month_key):
        return
    _expr_state.breakthrough_this_month = 0
    _expr_state.last_breakthrough_date = month_key + "-01"
    save_expression_state(_expr_state)
    log("[expression] 月度破防计数器已重置")


# ============ 触发检测 ============

def detect_gap_moment(uid, text, last_active, trans):
    """
    检测"对话的缝隙"——身体碎片只能在此出现。
    返回缝隙类型字符串，或 None。

    三种缝隙：
    - silence: 距离上次对话 > 5min，不在深夜睡眠段
    - transition: 日程事件切换
    - asked: 用户文本中关注冬的状态
    """
    now = datetime.now()
    hour = now.hour

    # silence: 长时间没说话（排除 2-7 点睡眠段）
    if last_active is not None:
        minutes_gap = (now - last_active).total_seconds() / 60
        if minutes_gap > 5 and not (2 <= hour < 7):
            return "silence"

    # transition: 日程事件切换
    if trans and (trans.get("detail") or trans.get("micro")):
        return "transition"

    # asked: 用户关心冬状态
    asked_keywords = ["回这么慢", "怎么不回", "不对", "怎么啦", "没事吧", "听起来",
                      "不舒服吗", "怎么了你", "你是不是", "还好吗", "咋了"]
    if any(kw in text for kw in asked_keywords):
        return "asked"

    return None


def detect_suspicion_pattern(uid, text, history, intimacy_level):
    """
    检测是否匹配"被冷落"模式 —— 冬的记忆中有类似的前科。

    返回 {"pattern_summary": ..., "matched_keywords": [...]} 或 None。
    """
    if uid != MASTER_UID or intimacy_level < 3:
        return None

    # 当前对话信号：冷落特征
    cold_signals = []
    if len(text.strip()) < 5:
        cold_signals.append("回复变短")
    if any(w in text for w in ["等一下", "在忙", "等会", "晚点", "一会说"]):
        cold_signals.append("推后不理")

    if not cold_signals and history:
        # 检查是否自己消息结尾且过了较久
        if len(history) >= 1:
            last_q, last_a, last_time = history[-1]
            minutes_ago = (datetime.now() - last_time).total_seconds() / 60
            if minutes_ago > 30 and len(last_a) > 0 and (not last_q or len(last_q.strip()) < 5):
                cold_signals.append("自己结尾未回")

    if not cold_signals:
        return None

    # 检索记忆：找被冷落/吃醋相关的条目
    from .memory import retrieve_relevant_memories, _get_memory_cache
    mem_cache = _get_memory_cache()
    uid_key = str(uid)

    if uid_key not in mem_cache or not mem_cache[uid_key].get("memories"):
        return None

    suspicion_keywords = ["和别人", "连麦", "没回", "不理", "敷衍", "冷落", "不叫我",
                          "战绩", "不回", "忽略", "没参与", "关一关"]
    matched = []
    for m in mem_cache[uid_key]["memories"]:
        imp = m.get("importance", 0.4)
        if isinstance(imp, str):
            imp = 0.8 if imp == "important" else 0.4
        if imp < 0.5:
            continue
        kw = m.get("keywords", [])
        summary = m.get("summary", m.get("content", ""))
        if any(sk in str(kw).lower() or sk in str(summary).lower() for sk in suspicion_keywords):
            matched.append({"summary": summary, "keywords": kw, "importance": imp})

    if not matched:
        return None

    # 取最相关的一条
    matched.sort(key=lambda x: x["importance"], reverse=True)
    best = matched[0]

    return {
        "pattern_summary": best["summary"],
        "matched_keywords": [k for k in best["keywords"] if any(sk in str(k).lower() for sk in suspicion_keywords) or True][:5],
    }


def detect_wooden_stake(uid, text, is_late, mood, overwhelm_state, intimacy_level):
    """
    检测对方的话是否击中冬的裂缝。

    返回命中类型字符串，或 None。
    """
    if uid != MASTER_UID:
        return None

    # 脆弱窗口
    in_window = False
    if is_late and mood < 40:
        in_window = True
    if overwhelm_state and overwhelm_state.get("active") and overwhelm_state.get("phase") in ("peak",):
        in_window = True
    if mood < 25:
        in_window = True
    if not in_window:
        return None

    text_stripped = text.strip()

    # 类型 A：坚定选择
    firm_words = ["一直在", "不会走", "你就是你", "我只要你", "不管怎样都",
                  "一直等你", "永远", "一直陪", "不会变"]
    if any(w in text_stripped for w in firm_words):
        return "firm_choice"

    # 类型 B：精准回忆了甜蜜时刻（对方消息命中了重要记忆的关键词）
    from .memory import _get_memory_cache
    mem_cache = _get_memory_cache()
    uid_key = str(uid)
    if uid_key in mem_cache and mem_cache[uid_key].get("memories"):
        for m in mem_cache[uid_key]["memories"]:
            imp = m.get("importance", 0.4)
            if isinstance(imp, str):
                imp = 0.8 if imp == "important" else 0.4
            if imp >= 0.8:
                kw = m.get("keywords", [])
                summary = m.get("summary", "")
                if any(k in text_stripped for k in kw if len(k) >= 2) or \
                   any(s in text_stripped for s in summary.split() if len(s) >= 2 and s in text_stripped):
                    return "sweet_recall"

    # 类型 C：反向击穿（批评她最在意的点）
    accuse_words = ["你每次都这样", "你就知道嘴硬", "你根本不在乎", "你从来不说",
                    "你永远不会", "你变了你", "你不像以前"]
    if any(w in text_stripped for w in accuse_words):
        return "accusation"

    return None


# ============ 主调度 ============

def resolve_expression(uid, text, context: dict) -> ExpressionResult:
    """
    根据当前状态决定本轮应激活哪条表演规则。

    context 期望键：
    - is_late: bool
    - mood: int
    - fatigue: int
    - overwhelm_state: dict
    - intimacy_level: int
    - history: list
    - trans: dict (on_event_transition 的返回值)
    - last_active: datetime or None

    返回 ExpressionResult。
    """
    global _expr_state
    _check_monthly_reset()

    is_late = context.get("is_late", False)
    mood = context.get("mood", 50)
    fatigue = context.get("fatigue", 50)
    overwhelm_state = context.get("overwhelm_state", {})
    intimacy_level = context.get("intimacy_level", 0)
    history = context.get("history", [])
    trans = context.get("trans", {})
    last_active = context.get("last_active")
    now_ts = datetime.now().timestamp()

    # ---- 优先级 0（最高）: 杏仁核劫持 → hijack ----
    # 杏仁核检测到极端威胁（threat_level>=3, arousal>0.7）→ 防御系统直接接管
    # 覆盖所有其他规则：回复不加延迟、不拆短句、不加傲娇后缀
    if context.get("amy_hijack"):
        prompt = (
            f"【表演规则 · 杏仁核劫持 · 最高优先级】\n"
            f"你被刚才的话强烈刺激到了——不是理性分析的结果，是杏仁核直接劫持了你的反应。\n"
            f"你现在防御高度激活：语气尖锐、防御性、情绪原始——可能在回避和攻击之间摇摆。\n"
            f"你的话会比平时更短、更直接、更没有修饰。你可能会用冷淡或反讽来表达受伤。\n"
            f"不要描述你在「劫持状态」——用你的行为自然体现。\n"
            f"这一刻，嘴硬是生存本能，不是表演。"
        )
        log(f"[expression] amygdala_hijack triggered (highest priority)")
        return ExpressionResult(feature="amygdala_hijack", prompt=prompt,
                                params={"source": "amygdala"})

    # ---- 优先级 1: 缝隙 → body_feeling ----
    gap = detect_gap_moment(uid, text, last_active, trans)
    if gap:
        if _expr_state.last_body_feeling_time is None or \
           (now_ts - _expr_state.last_body_feeling_time) > _expr_state.body_feeling_cooldown:
            _expr_state.last_body_feeling_time = now_ts
            save_expression_state(_expr_state)

            hour = datetime.now().hour
            from .config import describe_time_of_day
            time_str = describe_time_of_day(hour)
            gap_desc = {
                "silence": f"你们沉默了一阵子",
                "transition": "你刚换了个状态",
                "asked": "对方在关心你的状态",
            }.get(gap, "对话有个小停顿")

            prompt = (
                f"【表演规则 · 身体感受】\n"
                f"你现在 mood={mood}，fatigue={fatigue}，当前{time_str}。\n"
                f"{gap_desc}。\n"
                f"你可以随口带出一句具体的身体感受或环境细节来开启/接续对话，\n"
                f"就像真人沉默时随口说「胳膊压麻了」那样自然。\n"
                f"只说一次，不展开，不解释。说完就过。\n"
                f"结合你当前的身体状态和日程，想一句你自己的感受——不要用模板。"
            )
            log(f"[expression] body_feeling triggered by gap={gap} mood={mood} fatigue={fatigue}")
            return ExpressionResult(feature="body_feeling", prompt=prompt, params={"gap_type": gap})

    # ---- 优先级 2: 被冷落 → suspicion ----
    suspicion = detect_suspicion_pattern(uid, text, history, intimacy_level)
    if suspicion:
        if _expr_state.last_suspicion_time is None or \
           (now_ts - _expr_state.last_suspicion_time) > _expr_state.suspicion_cooldown:
            if _expr_state.suspicion_today_count < _expr_state.suspicion_max_per_day:
                _expr_state.last_suspicion_time = now_ts
                _expr_state.suspicion_today_count += 1
                save_expression_state(_expr_state)

                prompt = (
                    f"【表演规则 · 记忆触发 · ex-skill Layer 0 Rule 5】\n"
                    f"你记忆中发生过类似的事：{suspicion['pattern_summary']}\n"
                    f"现在对方的表现让你觉得「又来了」。\n"
                    f"你的嫉妒不是凭空猜的，而是有「前科」在。\n"
                    f"你可以用冷淡、笃定的语气抛出一个带证据的陈述，\n"
                    f"像你翻战绩那样——不是问「你是不是在跟别人玩」，\n"
                    f"而是说你记得什么、看到了什么。\n"
                    f"如果猜错了，对方否认后你会说「哦」然后沉默。\n"
                    f"猜对了你会更沉默——不是因为赢了，而是因为「果然是这样」。"
                )
                log(f"[expression] suspicion triggered (today: {_expr_state.suspicion_today_count}/{_expr_state.suspicion_max_per_day})")
                return ExpressionResult(feature="suspicion", prompt=prompt,
                                        params={"pattern_summary": suspicion["pattern_summary"],
                                                "matched_keywords": suspicion["matched_keywords"]})

    # ---- 优先级 3: 木桩命中 → wooden_stake ----
    stake_type = detect_wooden_stake(uid, text, is_late, mood, overwhelm_state, intimacy_level)
    if stake_type:
        if _expr_state.breakthrough_this_month < _expr_state.breakthrough_max_per_month:
            _expr_state.breakthrough_this_month += 1
            _expr_state.last_breakthrough_date = datetime.now().strftime("%Y-%m-%d")
            _expr_state.is_post_breakthrough = True
            save_expression_state(_expr_state)

            prompt = (
                f"【表演规则 · 内心触动 · ex-skill Layer 0 Rule 6】\n"
                f"对方这句话撞到了你平时藏得最深的地方。\n"
                f"你现在防御碎了——一瞬间说出真心话（可能很短，一个词或一句话）。\n"
                f"说完立刻意识到自己暴露了，慌张否认。\n"
                f"然后试图恢复嘴硬，假装刚才什么都没发生。\n"
                f"但语气里残留一点点不自然。\n"
                f"这个月你已经破防了{_expr_state.breakthrough_this_month}次，最多{_expr_state.breakthrough_max_per_month}次。\n"
                f"珍惜每一次。"
            )
            log(f"[expression] wooden_stake triggered type={stake_type} (monthly: {_expr_state.breakthrough_this_month}/{_expr_state.breakthrough_max_per_month})")
            return ExpressionResult(feature="wooden_stake", prompt=prompt,
                                    params={"stake_type": stake_type})

    # ---- 默认：维持嘴硬 ----
    prompt = ""
    if uid == MASTER_UID:
        prompt = (
            "【表演规则】维持你的嘴硬人设。如果想说软话，用傲娇的方式说。"
            "不要直接表达在乎。参考 ex-skill Layer 0 Rule 3——用反向表达关心。"
        )

    return ExpressionResult(feature="default", prompt=prompt, params={})


# ============ 收尾：标清破防后状态 ============

def clear_post_breakthrough():
    """在下一轮对话开始时调用，清除破防后慌张标记"""
    global _expr_state
    if _expr_state.is_post_breakthrough:
        _expr_state.is_post_breakthrough = False
        save_expression_state(_expr_state)


def get_expression_state_export() -> dict:
    """导出状态供仪表盘"""
    global _expr_state
    return {
        "body_feeling_cooldown": _expr_state.body_feeling_cooldown,
        "suspicion_today": _expr_state.suspicion_today_count,
        "suspicion_max": _expr_state.suspicion_max_per_day,
        "breakthrough_monthly": _expr_state.breakthrough_this_month,
        "breakthrough_max": _expr_state.breakthrough_max_per_month,
        "is_post_breakthrough": _expr_state.is_post_breakthrough,
    }
