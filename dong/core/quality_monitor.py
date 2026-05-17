"""
冬 · 回复质量漂移监控
每次跑 20 组 Q&A，记录平均长度/口癖率到 health_history.jsonl，
下次对比超阈值报警。
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
logger = logging.getLogger("dong.core.quality_monitor")
__all__ = ["QualityMetrics", "run_quality_sample", "record_quality", "check_quality_drift"]
# ────────────────────────────────────────────
# 固定测试提示词（覆盖日常/傲娇触发/深度/指令）
# ────────────────────────────────────────────
TEST_PROMPTS = [
    "你在干嘛", "想你了", "今天好累", "晚安", "吃了吗",
    "你好", "你在哪", "你烦不烦", "哼", "明天考试",
    "我错了", "你喜欢吃什么", "下雨了", "我好困",
    "你真可爱", "不理你了", "明天出去玩", "我想听歌",
    "你生气了吗", "给我看看你的猫",
]
# 口癖（傲娇标记）
TSUNDERE_MARKERS = [
    "才没有", "哼", "那咋了", "不是", "谁理你", "别管我",
    "烦死", "才不", "谁说", "才不要", "不儿", "嘎嘎",
    "咋整", "那又咋了", "才不是", "谁要", "少来", "做梦",
    "哼哼", "才没", "你才", "又没", "谁稀罕",
]
# AI 套话
AI_CLICHES = [
    "作为AI", "作为一个AI", "人工智能", "大语言模型",
    "无法提供", "不能提供", "如果需要帮助", "总而言之",
    "综上所述", "希望这能帮到你", "需要注意的是",
    "有什么我可以帮助", "如果你有任何",
]
# 漂移阈值
_LENGTH_DRIFT_THRESHOLD = 0.35
_TSUNDERE_DRIFT_THRESHOLD = 0.25
_SHORT_RATE_DRIFT_THRESHOLD = 0.15
_HISTORY_FILE = Path(__file__).resolve().parent.parent / "dong_health_history.jsonl"
# ────────────────────────────────────────────
# 指标
# ────────────────────────────────────────────
@dataclass
class QualityMetrics:
    avg_length: float = 0.0
    short_rate: float = 0.0        # ≤7字比例
    tsundere_rate: float = 0.0     # 含口癖比例
    ai_cliche_rate: float = 0.0    # 含AI套话比例
    max_length: int = 0
    sample_count: int = 0
def _measure_one(reply: str) -> Dict[str, Any]:
    length = len(reply) if reply else 0
    return {
        "length": length,
        "short": length <= 7,
        "tsundere": any(m in reply for m in TSUNDERE_MARKERS),
        "cliche": any(c in reply for c in AI_CLICHES),
    }
# ────────────────────────────────────────────
# 采样
# ────────────────────────────────────────────
def run_quality_sample(
    prompts: List[str] = None,
    uid: int = 1592741204,
    sleep_between: float = 0.5,
) -> QualityMetrics:
    """
    运行质量采样（同步，需在 to_thread 中调用）。
    对每条 prompt 调用 chat()，收集回复指标。
    """
    from dong.api import chat
    prompts = prompts or TEST_PROMPTS
    measurements = []
    for i, q in enumerate(prompts):
        try:
            reply = chat(q, uid=uid)
            measurements.append(_measure_one(reply or ""))
        except Exception as e:
            logger.warning("质量采样[%d]异常: %s", i, e)
            measurements.append({"length": 0, "short": True, "tsundere": False, "cliche": False})
        if sleep_between > 0 and i < len(prompts) - 1:
            time.sleep(sleep_between)
    if not measurements:
        return QualityMetrics()
    lengths = [m["length"] for m in measurements]
    n = len(measurements)
    return QualityMetrics(
        avg_length=sum(lengths) / n,
        short_rate=sum(m["short"] for m in measurements) / n,
        tsundere_rate=sum(m["tsundere"] for m in measurements) / n,
        ai_cliche_rate=sum(m["cliche"] for m in measurements) / n,
        max_length=max(lengths),
        sample_count=n,
    )
# ────────────────────────────────────────────
# 记录
# ────────────────────────────────────────────
def record_quality(metrics: QualityMetrics) -> None:
    """将质量指标追加到 health_history.jsonl。"""
    record = {
        "ts": time.time(),
        "type": "quality",
        "avg_length": round(metrics.avg_length, 1),
        "short_rate": round(metrics.short_rate, 3),
        "tsundere_rate": round(metrics.tsundere_rate, 3),
        "ai_cliche_rate": round(metrics.ai_cliche_rate, 3),
        "max_length": metrics.max_length,
        "sample_count": metrics.sample_count,
    }
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _trim_history(100)
    except Exception as e:
        logger.warning("质量记录写入失败: %s", e)
# ────────────────────────────────────────────
# 漂移检测
# ────────────────────────────────────────────
def check_quality_drift() -> Optional[str]:
    """
    与上次质量记录对比，返回报警文本（无漂移则返回 None）。
    """
    last_two = _read_quality_records(2)
    if len(last_two) < 2:
        return None
    prev, curr = last_two[0], last_two[1]
    alerts = []
    # 平均长度漂移
    prev_len = prev.get("avg_length", 0)
    curr_len = curr.get("avg_length", 0)
    if prev_len > 0:
        drift = abs(curr_len - prev_len) / prev_len
        if drift > _LENGTH_DRIFT_THRESHOLD:
            d = "变长" if curr_len > prev_len else "变短"
            alerts.append(f"平均回复{d}{drift:.0%}（{prev_len:.1f}→{curr_len:.1f}字）")
    # 口癖率漂移
    prev_t = prev.get("tsundere_rate", 0)
    curr_t = curr.get("tsundere_rate", 0)
    t_drift = abs(curr_t - prev_t)
    if t_drift > _TSUNDERE_DRIFT_THRESHOLD:
        d = "上升" if curr_t > prev_t else "下降"
        alerts.append(f"口癖率{d}{t_drift:.0%}（{prev_t:.0%}→{curr_t:.0%}）")
    # 短回复率偏移
    prev_s = prev.get("short_rate", 0)
    curr_s = curr.get("short_rate", 0)
    s_drift = abs(curr_s - prev_s)
    if s_drift > _SHORT_RATE_DRIFT_THRESHOLD:
        d = "上升" if curr_s > prev_s else "下降"
        alerts.append(f"短回复率{d}{s_drift:.0%}")
    # AI 套话率绝对值
    if curr.get("ai_cliche_rate", 0) > 0.1:
        alerts.append(f"AI套话率{curr['ai_cliche_rate']:.0%}")
    if not alerts:
        return None
    return "⚠ 回复质量漂移: " + "；".join(alerts)
def get_latest_metrics() -> Optional[Dict]:
    """读取最近一条质量记录。"""
    recs = _read_quality_records(1)
    return recs[0] if recs else None
# ────────────────────────────────────────────
# 内部工具
# ────────────────────────────────────────────
def _read_quality_records(n: int = 2) -> List[Dict]:
    if not _HISTORY_FILE.exists():
        return []
    try:
        with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        records = []
        for line in reversed(lines):
            try:
                obj = json.loads(line)
                if obj.get("type") == "quality":
                    records.append(obj)
            except json.JSONDecodeError:
                continue
            if len(records) >= n:
                break
        records.reverse()
        return records
    except Exception:
        return []
def _trim_history(max_lines: int = 100):
    if not _HISTORY_FILE.exists():
        return
    try:
        with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        if len(lines) > max_lines:
            with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(lines[-max_lines:]) + "\n")
    except Exception:
        pass