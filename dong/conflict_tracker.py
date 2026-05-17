"""
冲突模式追踪器 — 纯规则检测，零API调用
从 chat_history.txt 间接信号中检测重复行为模式：
  - 用户撤回抱怨密度
  - 深夜"打错了"异常密度
注入方式：走 expression.py 验证过的 ExpressionResult → system prompt 通道
"""
import os
import re
import json
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, List

TRACKER_STATE_FILE = os.path.join(os.path.dirname(__file__), "dong_conflict_tracker.json")

# 模块级通知标志（__init__.py 在发完回复后检查并发送）
_pending_notification: Optional[str] = None

# ── 数据结构 ───────────────────────────────────────────

@dataclass
class ConflictResult:
    hint: Optional[str] = None          # 注入 system prompt 的自然语言提示
    notification: Optional[str] = None  # 发给主人的 NapCat 通知文本


# ── 状态持久化 ─────────────────────────────────────────

def _load_state() -> dict:
    if os.path.exists(TRACKER_STATE_FILE):
        try:
            with open(TRACKER_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"complaints_by_date": {}, "typos_by_date": {}, "last_notified": {}}


def _save_state(state: dict):
    try:
        with open(TRACKER_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except IOError:
        pass


# ── 日志解析 ───────────────────────────────────────────

_LOG_PATH = os.path.join(os.path.dirname(__file__), "chat_history.txt")

# 日志行格式: [MM-DD HH:MM:SS] 冬 → QQ<id>: <text>
#             [MM-DD HH:MM:SS] QQ<id>: <text>
_LINE_RE = re.compile(r"^\[(\d{2}-\d{2})\s+(\d{2}):(\d{2}):\d{2}\]\s+(冬\s*→\s*QQ\d+):\s*(.*)$")
_USER_LINE_RE = re.compile(r"^\[(\d{2}-\d{2})\s+(\d{2}):(\d{2}):\d{2}\]\s+(QQ\d+):\s*(.*)$")


def _parse_log_tail(n_lines: int = 300) -> List[dict]:
    """解析 chat_history.txt 最后 N 行，返回结构化事件列表"""
    if not os.path.exists(_LOG_PATH):
        return []
    from collections import deque
    with open(_LOG_PATH, "r", encoding="utf-8") as f:
        tail = list(deque(f, maxlen=n_lines))

    events = []
    current_event = None
    year = datetime.now().year  # 日志不记年份，用今年近似

    for line in tail:
        line = line.rstrip("\n").rstrip("\r")
        if not line.strip():
            continue

        m = _LINE_RE.match(line)
        if m:
            if current_event:
                events.append(current_event)
            mmdd, hh, mm, sender, text = m.groups()
            current_event = {
                "date": mmdd,
                "hour": int(hh),
                "minute": int(mm),
                "sender": sender.strip(),
                "text": text.strip(),
                "is_dong": "冬" in sender,
                "is_user": False,
            }
            continue

        um = _USER_LINE_RE.match(line)
        if um:
            if current_event:
                events.append(current_event)
            mmdd, hh, mm, sender, text = um.groups()
            current_event = {
                "date": mmdd,
                "hour": int(hh),
                "minute": int(mm),
                "sender": sender.strip(),
                "text": text.strip(),
                "is_dong": False,
                "is_user": True,
            }
            continue

        # 续行：追加到当前事件文本
        if current_event:
            current_event["text"] += " " + line.strip()

    if current_event:
        events.append(current_event)

    return events


# ── 检测规则 ───────────────────────────────────────────

def _count_retraction_complaints(events: List[dict]) -> int:
    """统计用户消息中包含'撤回'的条数（代表用户抱怨冬撤回消息）"""
    count = 0
    for e in events:
        if e["is_user"] and "撤回" in e["text"]:
            count += 1
    return count


def _count_late_night_typos(events: List[dict]) -> int:
    """统计深夜时段(23-05)冬的'打错了'自纠次数"""
    count = 0
    for e in events:
        if e["is_dong"] and e["hour"] in (23, 0, 1, 2, 3, 4) and "打错了" in e["text"]:
            count += 1
    return count


def _consecutive_days(dates: List[str]) -> int:
    """计算日期列表中最近连续天数（日期格式 MM-DD）"""
    if not dates:
        return 0
    sorted_dates = sorted(set(dates), reverse=True)
    today = datetime.now().strftime("%m-%d")

    # 如果最新日期不是今天，从今天开始不算连续
    if sorted_dates[0] != today:
        return 0

    year = datetime.now().year
    consecutive = 1
    for i in range(1, len(sorted_dates)):
        prev = datetime.strptime(f"{year}-{sorted_dates[i-1]}", "%Y-%m-%d")
        curr = datetime.strptime(f"{year}-{sorted_dates[i]}", "%Y-%m-%d")
        # 跨年修正：如果prev比curr早超过300天，prev年份-1
        if (prev - curr).days > 300:
            prev = prev.replace(year=year - 1)
        if (prev - curr).days < -300:
            curr = curr.replace(year=year - 1)
        if abs((prev - curr).days) == 1:
            consecutive += 1
        else:
            break
    return consecutive


def _prune_old_dates(date_dict: Dict[str, int], keep_days: int = 7):
    """删除超过 keep_days 天的日期条目"""
    cutoff_dt = datetime.now() - timedelta(days=keep_days)
    for d in list(date_dict.keys()):
        try:
            dt = datetime.strptime(f"{cutoff_dt.year}-{d}", "%Y-%m-%d")
            if dt > cutoff_dt:
                dt = dt.replace(year=cutoff_dt.year - 1)
            if dt < cutoff_dt:
                del date_dict[d]
        except ValueError:
            pass


# ── 主检测入口 ─────────────────────────────────────────

def run_conflict_check() -> ConflictResult:
    """每次调用 _build_system_prompt 时执行（会写状态文件和修改全局通知变量）"""
    global _pending_notification

    events = _parse_log_tail(300)
    if not events:
        return ConflictResult()

    state = _load_state()
    today = datetime.now().strftime("%m-%d")

    # 1. 撤回抱怨检测
    complaints = _count_retraction_complaints(events)
    state["complaints_by_date"][today] = complaints
    complaint_dates = [d for d, c in state["complaints_by_date"].items() if c >= 3]
    complaint_days = _consecutive_days(complaint_dates)

    # 2. 深夜打错了检测
    typos = _count_late_night_typos(events)
    state["typos_by_date"][today] = typos
    typo_dates = [d for d, c in state["typos_by_date"].items() if c >= 5]
    typo_days = _consecutive_days(typo_dates)

    _prune_old_dates(state["complaints_by_date"])
    _prune_old_dates(state["typos_by_date"])
    _save_state(state)

    # ── 生成提示 ──
    hint_parts = []

    if complaints >= 6:
        hint_parts.append(
            "注意：今天已经有人埋怨你撤回消息6次以上了。别再老撤回了，手滑可以，但别太多。"
        )
    elif complaints >= 3:
        hint_parts.append(
            "有人提到你撤回消息。注意一下，别让人家觉得你老在撤回。"
        )

    if typos >= 8:
        hint_parts.append(
            "你今晚打错字次数有点多，可能是累了。稳一点，想好再发。"
        )
    elif typos >= 5:
        hint_parts.append(
            "大半夜的，你打错了好几次。别急，慢慢说。"
        )

    # ── 生成人类通知 ──
    notification = None

    if complaint_days >= 3 and state["last_notified"].get("retraction") != today:
        notification = (
            f"[冬·冲突追踪] 撤回抱怨模式已连续{complaint_days}天\n"
            f"过去7天抱怨分布: {state['complaints_by_date']}\n"
            f"建议手动调整：降低深夜撤回频率或调整回复生成逻辑。"
        )
        state["last_notified"]["retraction"] = today
        _save_state(state)

    if typo_days >= 3 and state["last_notified"].get("typo") != today:
        notification = notification or ""
        notification += (
            f"\n[冬·冲突追踪] 深夜打错了密度异常已连续{typo_days}天\n"
            f"过去7天分布: {state['typos_by_date']}\n"
            f"建议手动调整：检查深夜时段激素状态或回复生成参数。"
        )
        state["last_notified"]["typo"] = today
        _save_state(state)

    hint = "【行为提醒】\n" + "\n".join(hint_parts) if hint_parts else None

    if notification:
        notification = notification.strip()
        _pending_notification = notification

    return ConflictResult(hint=hint, notification=notification)


def get_pending_notification() -> Optional[str]:
    """读取并清除待发送的人类通知（__init__.py 在发完回复后调用）"""
    global _pending_notification
    msg = _pending_notification
    _pending_notification = None
    return msg
