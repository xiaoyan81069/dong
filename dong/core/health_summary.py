"""
冬 · 健康报告汇总 — 自然语言总结
收集 API / 自愈 / Agent / 质量 / 前端 状态，
输出一段人话总结，如：
"API正常但主力限流、数据自愈修复过1次、Agent Loop可正常使用"
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger("dong.core.health_summary")
__all__ = ["generate_health_summary"]
_HISTORY_FILE = Path(__file__).resolve().parent.parent / "dong_health_history.jsonl"
def generate_health_summary() -> str:
    """生成健康报告的自然语言总结（中文，顿号分隔）。"""
    fragments: List[str] = []
    # 1. API 状态
    api_frag = _summarize_api()
    if api_frag:
        fragments.append(api_frag)
    # 2. 数据自愈
    heal_frag = _summarize_healing()
    if heal_frag:
        fragments.append(heal_frag)
    # 3. Agent Loop / 技能
    agent_frag = _summarize_agent()
    if agent_frag:
        fragments.append(agent_frag)
    # 4. 回复质量
    quality_frag = _summarize_quality()
    if quality_frag:
        fragments.append(quality_frag)
    # 5. 前端连接
    frontend_frag = _summarize_frontend()
    if frontend_frag:
        fragments.append(frontend_frag)
    # 6. 健康检查项
    check_frag = _summarize_checks()
    if check_frag:
        fragments.append(check_frag)
    if not fragments:
        return "一切正常✅"
    return "、".join(fragments)
# ────────────────────────────────────────────
# 分项汇总
# ────────────────────────────────────────────
def _summarize_api() -> str:
    try:
        from .api_gateway import gateway
        gs = gateway.stats()
        circuits = gs.get("circuits", {})
        if not circuits:
            return "API未配置"
        open_names = [n for n, c in circuits.items() if c.get("state") == "OPEN"]
        half_open = [n for n, c in circuits.items() if c.get("state") == "HALF_OPEN"]
        closed_names = [n for n, c in circuits.items() if c.get("state") == "CLOSED"]
        in_cooldown = gs.get("global_cooldown", False)
        if in_cooldown:
            rem = gs.get("cooldown_remaining", 0)
            return f"所有API熔断中(冷却{rem:.0f}s)"
        parts = []
        if open_names:
            # 提取中文名
            labels = [_friendly_name(n) for n in open_names]
            parts.append(f"{'+'.join(labels)}限流熔断")
            if closed_names:
                parts.append("其余正常")
        elif half_open:
            labels = [_friendly_name(n) for n in half_open]
            parts.append(f"{'+'.join(labels)}试探恢复中")
            if closed_names:
                parts.append("其余正常")
        else:
            parts.append("API正常")
        # 限流但未熔断的（failures > 0）
        throttled = [n for n, c in circuits.items()
                     if c.get("state") == "CLOSED" and c.get("failures", 0) > 0]
        if throttled and not open_names:
            labels = [_friendly_name(n) for n in throttled]
            parts.append(f"{'+'.join(labels)}有限流迹象")
        return "".join(parts)
    except Exception:
        return "API状态未知"
def _summarize_healing() -> str:
    """总结最近一次启动自愈情况。"""
    try:
        if not _HISTORY_FILE.exists():
            return ""
        # 从末尾反向读取，避免全量加载
        with open(_HISTORY_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return ""
            chunk_size = 4096
            buf = b""
            pos = size
            while pos > 0 and b"\n" not in buf:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                buf = f.read(read_size) + buf
            lines = buf.decode("utf-8").strip().split("\n")
        for line in reversed(lines):
            try:
                rec = json.loads(line)
                # startup_check 记录（有 results 字段）
                if "results" not in rec:
                    continue
                repairs = sum(
                    1 for r in rec.get("results", [])
                    if r.get("auto_fixed") or r.get("repair_count", 0) > 0
                )
                fatal = rec.get("has_fatal", False)
                warns = sum(
                    1 for r in rec.get("results", [])
                    if not r.get("passed") and not r.get("auto_fixed")
                )
                if fatal:
                    return "启动检查有致命错误⛔"
                if repairs > 0:
                    return f"数据自愈修复过{repairs}次"
                if warns > 0:
                    return f"有{warns}项检查告警"
                return ""
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return ""
def _summarize_agent() -> str:
    try:
        from .skill_memory import skill_memory
        stats = skill_memory.stats()
        total = stats.get("total", 0)
        if total > 0:
            top = stats.get("top_skills", [])
            top_desc = top[0].description[:10] if top else ""
            return f"Agent Loop有{total}个技能" + (f"(最常用:{top_desc})" if top_desc else "")
        return "Agent Loop可正常使用"
    except ImportError:
        return ""
    except Exception:
        return ""
def _summarize_quality() -> str:
    try:
        from .quality_monitor import check_quality_drift, get_latest_metrics
        alert = check_quality_drift()
        if alert:
            return alert
        latest = get_latest_metrics()
        if latest:
            avg = latest.get("avg_length", 0)
            tsun = latest.get("tsundere_rate", 0)
            return f"回复质量正常(均长{avg:.0f}字 口癖{tsun:.0%})"
    except Exception:
        pass
    return ""
def _summarize_frontend() -> str:
    try:
        from .health_registry import registry
        chk = registry.get_check("frontend_poll")
        if chk:
            if chk.last_result:
                return "前端已连接"
            elif chk.consecutive_failures > 0:
                return "前端未连接"
    except Exception:
        pass
    return ""
def _summarize_checks() -> str:
    """汇总健康检查项的异常。"""
    try:
        from .health_registry import registry
        snap = registry.snapshot()
        failing = [n for n, info in snap.items() if not info.get("last_result", True)]
        if failing:
            return f"检查项{'+'.join(failing)}未通过"
    except Exception:
        pass
    return ""
def _friendly_name(provider_name: str) -> str:
    """把 provider 名变成友好中文名。"""
    name_map = {
        "主力": "主力", "备用": "备用", "分析": "分析", "多模态": "多模态",
    }
    if provider_name in name_map:
        return name_map[provider_name]
    # 取 model 名中的关键部分
    return provider_name[:6]