"""
冬 · 优化代理

睡眠时自动触发：
  备份 → 分析聊天记录 → 风格指纹 → AI修改代码 → 群聊测试 → 评估 → 部署/回滚

全部自动化，无人值守。
"""
import ast
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    BASE_DIR,
    BACKUPS_DIR,
    CHAT_HISTORY_FILE,
    FACTORY_ANCHOR,
    FACTORY_ARCHIVE_PATH,
    FACTORY_CSV_PATH,
    FACTORY_HASHES_PATH,
    FOSSIL_PATHS,
    MASTER_UID,
    OPTIMIZER_ANALYSIS_SAMPLE_COUNT,
    OPTIMIZER_BACKUP_KEEP_DAYS,
    OPTIMIZER_ENABLED,
    OPTIMIZER_LOG_FILE,
    OPTIMIZER_STATE_FILE,
    OPTIMIZER_TEST_DURATION_MIN,
    OPTIMIZER_TEST_GROUP_ID,
    OPTIMIZER_WIN_THRESHOLD,
    STYLE_FINGERPRINT_PATH,
)
from .log import log as _log


# ============ 日志 ============

def optimizer_log(msg: str):
    """写入优化日志"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    _log(f"[optimizer] {msg}")
    try:
        with open(OPTIMIZER_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ============ 数据结构 ============

@dataclass
class AnalysisReport:
    """AI分析报告"""
    total_analyzed: int = 0
    problems: List[Dict] = field(default_factory=list)
    suggestions: List[Dict] = field(default_factory=list)
    raw: str = ""


@dataclass
class TestResult:
    """测试评估结果"""
    metrics: Dict[str, float] = field(default_factory=dict)
    old_scores: Dict[str, float] = field(default_factory=dict)
    new_scores: Dict[str, float] = field(default_factory=dict)
    has_fatal_flaw: bool = False       # >50字消息→致命缺陷
    fatal_reason: str = ""
    verdict: str = ""                  # "deploy" / "rollback"
    detail: str = ""


@dataclass
class OptimizationState:
    """优化运行状态（持久化）"""
    last_run: Optional[str] = None
    last_result: str = ""              # "deployed" / "rolled_back" / "aborted"
    last_metrics_summary: str = ""
    total_runs: int = 0
    successful_deploys: int = 0
    current_stage: str = ""            # 当前阶段（用于恢复）


# ============ 状态管理 ============

def _load_state() -> OptimizationState:
    try:
        if os.path.exists(OPTIMIZER_STATE_FILE):
            with open(OPTIMIZER_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return OptimizationState(**data)
    except Exception:
        pass
    return OptimizationState()


def _save_state(state: OptimizationState):
    try:
        with open(OPTIMIZER_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "last_run": state.last_run,
                "last_result": state.last_result,
                "last_metrics_summary": state.last_metrics_summary,
                "total_runs": state.total_runs,
                "successful_deploys": state.successful_deploys,
                "current_stage": state.current_stage,
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_optimizer_state() -> dict:
    """导出状态供仪表盘"""
    s = _load_state()
    return {
        "enabled": OPTIMIZER_ENABLED,
        "last_run": s.last_run,
        "last_result": s.last_result,
        "total_runs": s.total_runs,
        "successful_deploys": s.successful_deploys,
        "current_stage": s.current_stage,
    }


# ============ 主入口 ============

# 最少需要的聊天记录数（bot回复 + 用户消息各自的最低条数）
MIN_BOT_REPLIES_FOR_OPTIMIZE = 8
MIN_USER_MSGS_FOR_OPTIMIZE = 8


async def run_optimizer() -> bool:
    """
    优化主流程。返回 True 表示代码已被更新。

    两种模式：
    - 群聊测试模式（TEST_GROUP_ID != 0）：完整流程含群聊验证
    - 直接上线模式（TEST_GROUP_ID == 0）：分析聊天→修改→直接上线
    """
    if not OPTIMIZER_ENABLED:
        return False

    state = _load_state()
    state.total_runs += 1
    state.current_stage = "starting"
    state.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_state(state)

    run_id = datetime.now().strftime("%Y%m%d_%H%M")
    backup_path = ""
    final_action = "aborted"
    error_msg = ""

    try:
        # ===== Phase 1: 备份 =====
        state.current_stage = "backup"
        _save_state(state)
        optimizer_log(f"[{run_id}] Phase 1: 备份项目...")
        backup_path = _phase_backup()
        optimizer_log(f"[{run_id}] 备份完成: {backup_path}")

        # ===== Phase 2: 分析聊天记录 =====
        state.current_stage = "analyze"
        _save_state(state)
        optimizer_log(f"[{run_id}] Phase 2: 分析聊天记录...")

        # 2a. 先检查记录数量
        all_msgs = _parse_chat_history()
        bot_replies = [m for m in all_msgs if m["role"] == "dong"]
        user_msgs = [m for m in all_msgs if m["role"] == "user"]
        optimizer_log(f"[{run_id}] 今天: {len(bot_replies)}条冬回复, {len(user_msgs)}条用户消息")

        if len(bot_replies) < MIN_BOT_REPLIES_FOR_OPTIMIZE:
            optimizer_log(f"[{run_id}] 跳过: 冬回复不足({len(bot_replies)}<{MIN_BOT_REPLIES_FOR_OPTIMIZE})")
            final_action = "skipped_insufficient_data"
            state.last_result = f"跳过(冬回复{len(bot_replies)}条<{MIN_BOT_REPLIES_FOR_OPTIMIZE})"
            _phase_cleanup(backup_path, "skipped")
            _save_state(state)
            return False

        if len(user_msgs) < MIN_USER_MSGS_FOR_OPTIMIZE:
            optimizer_log(f"[{run_id}] 跳过: 用户消息不足({len(user_msgs)}<{MIN_USER_MSGS_FOR_OPTIMIZE})")
            final_action = "skipped_insufficient_data"
            state.last_result = f"跳过(用户消息{len(user_msgs)}条<{MIN_USER_MSGS_FOR_OPTIMIZE})"
            _phase_cleanup(backup_path, "skipped")
            _save_state(state)
            return False

        # 2b. 足够 → 真正分析
        analysis = await _phase_analyze()
        if analysis and analysis.problems:
            optimizer_log(f"[{run_id}] 发现 {len(analysis.problems)} 个问题")
        else:
            optimizer_log(f"[{run_id}] 未发现明显问题")
            final_action = "skipped_no_problems"
            _phase_cleanup(backup_path, "skipped")
            _save_state(state)
            return False

        # ===== Phase 3: 风格指纹 =====
        state.current_stage = "fingerprint"
        _save_state(state)
        optimizer_log(f"[{run_id}] Phase 3: 加载风格指纹...")
        fingerprint = _phase_fingerprint()
        if fingerprint:
            optimizer_log(f"[{run_id}] 指纹已加载: {fingerprint.total_messages}样本, 平均{fingerprint.avg_reply_length:.1f}字")

        # ===== Phase 3.5: 化石完整性校验 =====
        state.current_stage = "fossil_check"
        _save_state(state)
        optimizer_log(f"[{run_id}] Phase 3.5: 化石完整性校验...")
        fossil_ok, fossil_err = verify_fossil_integrity()
        if not fossil_ok:
            optimizer_log(f"[{run_id}] 化石校验失败: {fossil_err}")
            final_action = "aborted_fossil_tampered"
            state.last_result = f"中止(化石被篡改: {fossil_err[:80]})"
            _phase_cleanup(backup_path, "aborted")
            _save_state(state)
            return False

        # ===== Phase 4: AI修改代码 =====
        state.current_stage = "modify"
        _save_state(state)
        optimizer_log(f"[{run_id}] Phase 4: AI代码修改...")
        changes = await _phase_modify(backup_path, analysis)
        if not changes:
            optimizer_log(f"[{run_id}] 无有效修改")
            final_action = "skipped_no_changes"
            _phase_cleanup(backup_path, "skipped")
            _save_state(state)
            return False

        optimizer_log(f"[{run_id}] 应用了 {len(changes)} 处修改")

        # ===== Phase 5: 模拟对话评估 =====
        state.current_stage = "dialogue_eval"
        _save_state(state)
        optimizer_log(f"[{run_id}] Phase 5: 模拟对话评估...")

        try:
            from .dialogue_evaluator import run_dialogue_evaluation
            dialogue_result = await run_dialogue_evaluation()
        except Exception as e:
            optimizer_log(f"[{run_id}] 对话评估异常: {e}")
            dialogue_result = {"verdict": "rollback", "reason": f"评估异常: {e}"}

        dialogue_verdict = dialogue_result.get("verdict", "rollback")
        dialogue_reason = dialogue_result.get("reason", "")

        if dialogue_verdict == "rollback":
            optimizer_log(f"[{run_id}] 对话评估不通过: {dialogue_reason}")
            final_action = "rolled_back_dialogue"
            state.last_result = f"回滚(对话评估: {dialogue_reason[:80]})"
            _phase_cleanup(backup_path, "rollback")
            _save_state(state)
            return False

        optimizer_log(f"[{run_id}] 对话评估通过: {dialogue_reason}")

        # ===== Phase 6: 群聊测试（可选）=====
        use_group_test = OPTIMIZER_TEST_GROUP_ID != 0

        if use_group_test:
            # 群聊测试作为补充验证
            state.current_stage = "test"
            _save_state(state)
            optimizer_log(f"[{run_id}] Phase 6: 群聊测试...")
            test_result = await _phase_test(backup_path, fingerprint)

            if test_result.has_fatal_flaw:
                optimizer_log(f"[{run_id}] 致命缺陷: {test_result.fatal_reason}")
                final_action = "rolled_back"
                state.last_result = f"回滚({test_result.fatal_reason})"
                _phase_cleanup(backup_path, "rollback")
                _save_state(state)
                return False

            state.current_stage = "evaluate"
            _save_state(state)
            verdict = _phase_decide(test_result)
        else:
            # 无群聊：对话评估已通过，直接上线
            verdict = "deploy"

        # ===== Phase 6: 部署或回滚 =====
        if verdict == "deploy":
            # ★ 门禁：部署前健康检查，有任何 FAIL 则中止
            try:
                from .core.health_registry import registry as _hr
                results = _hr.run_all_due()
                for chk_name, passed in results.items():
                    if not passed:
                        chk = _hr.get_check(chk_name)
                        if chk and chk.consecutive_failures >= 2:
                            optimizer_log(f"[{run_id}] 门禁拦截: {chk_name} 连续失败{chk.consecutive_failures}次，中止部署")
                            final_action = "aborted_gate"
                            state.last_result = f"中止(健康门禁: {chk_name})"
                            _phase_cleanup(backup_path, "aborted")
                            _save_state(state)
                            return False
            except ImportError:
                pass
            except Exception as e:
                optimizer_log(f"[{run_id}] 门禁异常: {e}，保守中止")
                return False
            optimizer_log(f"[{run_id}] 决策: 上线")
            _phase_deploy(backup_path, changes)
            final_action = "deployed"
            state.successful_deploys += 1

            reason = f"{len(analysis.problems)}问题 + {len(changes)}处修改"
            state.last_result = f"上线({reason})"
        else:
            optimizer_log(f"[{run_id}] 决策: 保留原版")
            final_action = "kept_original"
            state.last_result = f"保留原版(改动不够好)"

        # ===== Phase 7: 清理 =====
        state.current_stage = "cleanup"
        _save_state(state)
        _phase_cleanup(backup_path, final_action)

    except Exception as e:
        error_msg = str(e)
        optimizer_log(f"[{run_id}] 异常: {error_msg}")
        optimizer_log(traceback.format_exc())
        final_action = "aborted"
        state.last_result = f"异常({error_msg[:50]})"
        if backup_path and os.path.exists(backup_path):
            _phase_cleanup(backup_path, "aborted")

    state.current_stage = "idle"
    state.last_metrics_summary = state.last_result
    _save_state(state)

    _write_run_log(run_id, final_action, error_msg)
    optimizer_log(f"[{run_id}] 完成: {final_action}")

    return final_action == "deployed"


def _format_metrics(tr) -> str:
    """格式化指标摘要。tr 可以是 TestResult 或 dict。"""
    parts = []
    scores = tr.new_scores if hasattr(tr, 'new_scores') else tr.get("new_scores", {})
    old = tr.old_scores if hasattr(tr, 'old_scores') else tr.get("old_scores", {})
    for k, v in scores.items():
        o = old.get(k, 0)
        arrow = "↑" if v > o else "↓" if v < o else "→"
        parts.append(f"{k}={v:.2f}{arrow}")
    return ", ".join(parts[:4])


def _evaluate_from_chat_history(bot_replies: List[Dict], fingerprint, analysis: AnalysisReport) -> str:
    """
    无群聊测试时的替代评估：对比今天冬的回复 vs 出厂锚点。

    评估逻辑：
    1. 从 bot_replies 提取今天的回复文本
    2. 计算 vs 出厂指纹的匹配度
    3. 分析报告中每个问题的严重程度
    4. 如果匹配度低于阈值 且 分析发现了问题 → deploy
    5. 如果匹配度已经很高 → 保留原版

    Returns: "deploy" or "keep"
    """
    reply_texts = [m["text"] for m in bot_replies if m["role"] == "dong"]
    if not reply_texts:
        return "keep"

    # 计算当天回复 vs 出厂锚点
    from .style_fingerprint import match_score

    if fingerprint:
        overall, detail_scores = match_score(fingerprint, reply_texts)
    else:
        from .style_fingerprint import compute_cliche_rate, compute_ai_language_score

        overall = 0.5  # 无锚点默认中等
        detail_scores = {
            "short_sentence_rate": sum(1 for t in reply_texts if len(t) <= 7) / len(reply_texts),
            "cliche_rate": 1.0 - compute_cliche_rate(reply_texts),
            "ai_language_score": 1.0 - compute_ai_language_score(reply_texts),
        }

    optimizer_log(f"[评估] 当天回复 vs 出厂锚点: 综合={overall:.3f}")
    for k, v in detail_scores.items():
        optimizer_log(f"  {k}: {v:.3f}")

    # 问题严重度加权
    severity_weight = {"高": 3, "中": 2, "低": 1}
    total_severity = sum(severity_weight.get(p.get("severity", "中"), 2) for p in analysis.problems)
    problem_count = len(analysis.problems)

    optimizer_log(f"[评估] {problem_count}个问题, 严重度总分={total_severity}")

    # 决策规则（修复：低分+严重问题不应上线，应保守保留原版）：
    # - 匹配度 ≥ 0.6 且 无高严重度问题 → 可以上线
    # - 匹配度 ≥ 0.4 且 无严重问题 + 问题总分低 → 谨慎上线
    # - 匹配度 < 0.4 或 有严重问题 → 保留原版

    high_severe = sum(1 for p in analysis.problems if p.get("severity") == "高")

    if overall >= 0.6 and high_severe == 0:
        optimizer_log(f"[评估] 匹配度良好({overall:.2f})无严重问题 → 上线")
        return "deploy"

    if overall >= 0.4 and high_severe == 0 and total_severity <= 2:
        optimizer_log(f"[评估] 匹配度可接受({overall:.2f})问题轻微({total_severity}) → 谨慎上线")
        return "deploy"

    if high_severe >= 1:
        optimizer_log(f"[评估] 有严重问题 → 保留原版（不上线）")
    elif overall < 0.4:
        optimizer_log(f"[评估] 匹配度过低({overall:.2f}) → 保留原版")
    else:
        optimizer_log(f"[评估] 匹配度可接受但问题较多({total_severity}) → 保留原版")
    return "keep"


# ============ Phase 1: 备份 ============

def _phase_backup() -> str:
    """完整备份项目目录"""
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    backup_dir = os.path.join(BACKUPS_DIR, f"dong_backup_{ts}")
    os.makedirs(BACKUPS_DIR, exist_ok=True)

    ignore = shutil.ignore_patterns(
        "__pycache__", "*.pyc", "chat_history.txt", "dong_optimizer.log",
        "dong_optimizer_state.json", "*.log", "dong_backups",
        ".git", "dong_schedule_archive",
    )
    shutil.copytree(BASE_DIR, backup_dir, ignore=ignore)
    return backup_dir


# ============ Phase 2: 分析聊天记录 ============

async def _phase_analyze() -> Optional[AnalysisReport]:
    """分析今天冬的回复质量"""
    messages = _parse_chat_history()
    if not messages:
        optimizer_log("  无聊天记录可分析")
        return None

    # 采样：取最近N条bot回复
    bot_replies = [m for m in messages if m["role"] == "dong"]
    if len(bot_replies) < 5:
        optimizer_log(f"  回复太少({len(bot_replies)}条)，跳过分析")
        return None

    sample = bot_replies[-OPTIMIZER_ANALYSIS_SAMPLE_COUNT:]
    sample_text = "\n".join([f"[{m['time']}] 冬: {m['text'][:100]}" for m in sample])

    # 调用 analysis API
    raw = await _run_analysis_api(sample_text, len(bot_replies))
    return _parse_analysis_response(raw, len(bot_replies))


def _parse_chat_history(date_str: str = None) -> List[Dict]:
    """解析 chat_history.txt"""
    if not os.path.exists(CHAT_HISTORY_FILE):
        return []

    target_date = date_str or datetime.now().strftime("%m-%d")
    results = []
    try:
        with open(CHAT_HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("["):
                    continue
                # 格式: [MM-DD HH:MM:SS] QQ<uid>: <text> 或 [MM-DD HH:MM:SS] 冬 → QQ<uid>: <text>
                # 只分析当天的
                if not line.startswith(f"[{target_date} "):
                    continue

                # 提取时间
                time_match = re.match(r"\[(\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
                time_str = time_match.group(1) if time_match else ""

                if "冬 →" in line:
                    text = line.split("冬 →", 1)[-1].strip()
                    # 去掉 QQ<uid>: 前缀
                    text = re.sub(r"^QQ\d+:\s*", "", text)
                    results.append({"role": "dong", "time": time_str, "text": text})
                elif re.match(r"\[\d{2}-\d{2}.*?\] QQ\d+:", line):
                    text = re.sub(r"^\[\d{2}-\d{2}.*?\] QQ\d+:\s*", "", line)
                    # 重新提取时间标记后的部分
                    m2 = re.match(r"\[\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] QQ(\d+):\s*(.*)", line)
                    if m2:
                        uid = m2.group(1)
                        text = m2.group(2)
                        results.append({"role": "user", "uid": uid, "time": time_str, "text": text})
    except Exception as e:
        optimizer_log(f"  解析chat_history失败: {e}")
    return results


def _get_analysis_constraints() -> str:
    """分析阶段：告诉AI它能建议改什么，但不要求指定精确参数名。"""
    return """
【你可以建议修改的范围】
- API调用参数：temperature, max_tokens 等数值
- 提示词模板：回复风格指令、行为描述文本
- 行为阈值：延迟时间、触发概率、疲劳/情绪阈值
- 语言风格参数：口癖概率、标点混乱度、错别字率

【你不能建议修改的】
- 核心记忆/人格定义/关系数据
- 控制流逻辑/数据结构

【建议格式 — 重要】
- file: 目标文件名（status.py / persona.py / interaction.py / api.py / config.py）
- description: 描述需要改什么（概念层面，如"晚安消息生成时的temperature太高"）
- goal: 期望达到的效果（如"降低AI温度让回复更随意自然"）
- severity: 改动幅度（"微调"/"小调"/"中调"）

不要指定确切的变量名或行号——代码修改阶段会有另一个AI读取实际源码来执行精确编辑。
"""


async def _run_analysis_api(sample_text: str, total_count: int) -> str:
    """调用analysis API分析回复质量"""
    try:
        from .config import _get_cfg
        import requests

        cfg = _get_cfg("analysis")
        constraints = _get_analysis_constraints()
        prompt = f"""你是冬的行为分析师。冬是一个傲娇、嘴硬心软、东北口音、说话极短的女大学生QQ聊天机器人。
她的出厂原型来自真实微信聊天记录，统计特征：平均回复6.9字，65.7%的回复在7字以内，最长回复34字，从不超过50字。

以下是冬今天的部分回复（共{total_count}条，这里采样了部分）。请分析她的表现：

1. **机械感/模板化**：是否出现过于礼貌、像客服的回答？是否使用了AI常见套话？
2. **人设偏离**：是否存在回复过长、缺乏傲娇、没有东北口音、没有嘴硬？
3. **重复模式**：是否频繁使用相同句式？
4. **逻辑断裂**：是否存在答非所问？

对每个问题给出：类别、严重程度（高/中/低）、具体描述

{constraints}

请以JSON格式输出。只输出JSON，不要其他文字：
{{"problems": [{{"category":"...", "severity":"高/中/低", "description":"...", "example":"..."}}], "suggestions": [{{"file":"status.py/persona.py/interaction.py/api.py/config.py", "description":"描述要改什么(概念层面)", "goal":"期望效果", "severity":"微调/小调/中调"}}]}}

回复记录：
{sample_text}"""

        r = await asyncio.to_thread(
            requests.post,
            f"{cfg.api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg.model,
                "temperature": 0.3,
                "max_tokens": 1500,
                "messages": [
                    {"role": "system", "content": "你是冬的行为分析师。只输出JSON。"},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=30,
        )

        if r.status_code == 200:
            resp = r.json()
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content.strip()
        else:
            optimizer_log(f"  分析API失败: {r.status_code}")
            return ""
    except Exception as e:
        optimizer_log(f"  分析API异常: {e}")
        return ""


def _parse_analysis_response(raw: str, total_count: int) -> AnalysisReport:
    """解析分析API返回的JSON"""
    report = AnalysisReport(total_analyzed=total_count, raw=raw)
    if not raw:
        return report

    try:
        # 处理 ```json ``` 包裹
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?\s*```", raw)
        if m:
            raw = m.group(1).strip()

        # 提取JSON对象
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            optimizer_log("  分析结果中未找到JSON")
            return report

        data = json.loads(m.group(0))
        report.problems = data.get("problems", [])
        report.suggestions = data.get("suggestions", [])
    except json.JSONDecodeError as e:
        optimizer_log(f"  分析JSON解析失败: {e}")

    # ── 注入测试驱动的修复建议 ──
    try:
        from .core.daily_test import get_test_driven_suggestions
        test_suggestions = get_test_driven_suggestions()
        if test_suggestions:
            report.suggestions.extend(test_suggestions)
            optimizer_log(f"  注入{len(test_suggestions)}条测试驱动建议")
    except Exception:
        pass

    return report


# ============ Phase 3: 风格指纹 ============

def _phase_fingerprint():
    """加载或创建风格指纹"""
    from .style_fingerprint import load_fingerprint, compute_fingerprint, save_fingerprint

    fp = load_fingerprint()
    if fp and fp.total_messages > 0:
        return fp

    # 第一次运行：从CSV生成
    optimizer_log("  首次运行，从CSV生成风格指纹...")
    if os.path.exists(FACTORY_CSV_PATH):
        fp = compute_fingerprint(FACTORY_CSV_PATH, FACTORY_ARCHIVE_PATH)
        save_fingerprint(fp)
        return fp
    else:
        optimizer_log(f"  ⚠️ CSV不存在: {FACTORY_CSV_PATH}，无法生成风格指纹")
        return None


# ============ 人格化石完整性校验 ============

def _compute_file_sha256(filepath: str) -> str:
    """计算单个文件的SHA256哈希"""
    try:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return "MISSING"
    except Exception as e:
        return f"ERROR:{e}"


def generate_fossil_hashes() -> bool:
    """生成/刷新化石文件哈希文件。返回True=成功。"""
    try:
        hashes = {}
        for name, path in FOSSIL_PATHS.items():
            h = _compute_file_sha256(path)
            hashes[name] = {"path": path, "sha256": h}
            optimizer_log(f"  化石哈希 {name}: {h[:16] if h.startswith('MISSING') or h.startswith('ERROR') else h[:12]}...")

        os.makedirs(os.path.dirname(FACTORY_HASHES_PATH), exist_ok=True)
        with open(FACTORY_HASHES_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "files": {k: {"path": v["path"], "sha256": v["sha256"]} for k, v in hashes.items()},
            }, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        optimizer_log(f"  化石哈希生成失败: {e}")
        return False


def verify_fossil_integrity() -> Tuple[bool, str]:
    """
    校验4个化石文件完整性（对比存储的SHA256）。
    Returns: (通过?, 失败原因)
    """
    if not os.path.exists(FACTORY_HASHES_PATH):
        # 首次运行：自动生成哈希
        optimizer_log("  首次运行，生成化石哈希基准...")
        if generate_fossil_hashes():
            return True, ""
        return False, "无法生成化石哈希文件"

    try:
        with open(FACTORY_HASHES_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except Exception as e:
        return False, f"无法读取化石哈希文件: {e}"

    stored_files = stored.get("files", {})
    if not stored_files:
        return False, "化石哈希文件为空"

    failures = []
    for name, info in stored_files.items():
        path = info.get("path", "")
        expected_hash = info.get("sha256", "")

        if not os.path.exists(path):
            failures.append(f"{name}(文件缺失)")
            continue

        current_hash = _compute_file_sha256(path)
        if current_hash != expected_hash:
            failures.append(f"{name}(哈希不匹配: 期望{expected_hash[:12]}... 当前{current_hash[:12]}...)")

    if failures:
        msg = f"化石文件被篡改: {'; '.join(failures)}"
        optimizer_log(f"  [化石校验] 失败 — {msg}")
        return False, msg

    optimizer_log("  [化石校验] 通过 — 4个化石文件完整")
    return True, ""


def check_hard_constraints(messages: List[str]) -> Tuple[bool, str, Dict]:
    """
    出厂锚点硬约束检查（人格化石门票）。

    Args:
        messages: 冬的回复文本列表

    Returns:
        (通过?, 失败原因, 详细指标dict)
    """
    if not messages:
        return True, "无消息可检查", {}

    lengths = [len(m) for m in messages]
    avg_len = sum(lengths) / len(lengths)
    short_rate = sum(1 for l in lengths if l <= 7) / len(lengths) * 100
    max_len = max(lengths)

    target_avg, avg_tolerance = FACTORY_HARD_CONSTRAINTS["avg_reply_length"]
    target_short, short_tolerance = FACTORY_HARD_CONSTRAINTS["short_rate"]
    hard_max, _ = FACTORY_HARD_CONSTRAINTS["max_single_reply"]

    details = {
        "avg_reply_length": round(avg_len, 2),
        "short_rate": round(short_rate, 1),
        "max_single_reply": max_len,
        "sample_size": len(messages),
    }

    # 1. 绝对上限检查
    if max_len > hard_max:
        return False, f"出现{max_len}字超长回复(上限{hard_max}字)", details

    # 2. 平均长度检查
    if abs(avg_len - target_avg) > avg_tolerance:
        direction = "过长" if avg_len > target_avg else "过短"
        return False, f"平均长度{avg_len:.1f}字{direction}(锚点{target_avg}±{avg_tolerance}字)", details

    # 3. 短句率检查
    if abs(short_rate - target_short) > short_tolerance:
        direction = "过高" if short_rate > target_short else "过低"
        return False, f"短句率{short_rate:.1f}%{direction}(锚点{target_short}%±{short_tolerance}%)", details

    return True, "", details


# ============ Phase 4: AI修改代码（两步法） ============
#
# 两步法设计：
#   Step A — 分析API：看聊天记录 → 输出概念性修改建议（describe what & why）
#   Step B — 修改API：读实际源码 + 建议 → 输出精确的 old→new 编辑对
#
# 这样避免了"AI猜测参数名但代码中不存在该变量"的问题。

EDIT_WHITELIST_FILES = {
    "config.py", "status.py", "interaction.py",
    "persona.py", "api.py",
    "command_channel.py", "tools.py", "expression.py",
    "decision.py", "overwhelm.py", "memory.py",
    "factory.py", "style_fingerprint.py", "sleep_guardian.py",
    "conflict_tracker.py", "regret.py", "screen_guard.py",
    "schedule.py", "intimacy.py", "media.py",
    "amygdala.py", "grudge.py",
}

EDIT_FORBIDDEN_PATHS = [
    "memory.json", "memory",
    "intimacy", "factory_archive",
    "amygdala", "grudge",
    "characters/", "persona.txt",
    "persona_ex-skill", "ex-skill",
    "style_fingerprint", "fingerprint",
    "factory_hashes",
    "chat_history", "schedule_archive",
    "frog", "data/", "media/",
    "backup", "backups",
    "optimizer", "dialogue_evaluator",
    "dashboard", "connector",
    "dialogue_segments",
]

# 人格化石硬约束（出厂锚点，基于真实微信聊天记录统计）
# 任何修改导致回复偏离这些指标 → 直接回滚
FACTORY_HARD_CONSTRAINTS = {
    "avg_reply_length": (FACTORY_ANCHOR["avg_reply_length"], 0.8),  # 均值±0.8字内
    "short_rate": (FACTORY_ANCHOR["short_rate"], 5.0),              # ≤7字比例±5%内
    "max_single_reply": (FACTORY_ANCHOR["hard_max"], 0),            # 绝对上限50字
}

# 代码修改的安全约束（注入modify API prompt）
_MODIFY_SAFETY_RULES = """【安全规则】
1. 可以改：任何代码逻辑、参数值、提示词模板、条件分支、概率、阈值、回复格式
2. 不能改：涉及 memory.json / intimacy / factory_archive / characters/ 的代码路径
3. 不能改：数据持久化的存储键名、文件路径、数据库结构
4. 不能改：从外部文件加载人物数据的代码（如 _load_persona()、_load_memory() 等）
5. 严禁给函数/类构造函数添加源码中不存在的参数名（会导致运行时崩溃）
   - 想改某个值，必须找到它现有的参数名/变量名，只改值不改名
   - 例如：APIConfig(...) 不接受 temperature=，不要在构造函数调用里加新参数
6. old_string 必须在源码中精确存在（包括所有空白符）
7. old_string 在源码中必须唯一（如不唯一，用更多上下文使其唯一）
8. new_string 必须是语法正确的Python代码片段
9. 【化石保护】严禁修改以下文件/路径中的任何内容（这些是冬的人格化石，绝对锁定）：
   - dong.persona.txt / persona_ex-skill.json（人物提示词核心）
   - dong_factory_archive.json（出厂对话记忆蒸馏）
   - dong_style_fingerprint.json（风格锚点统计）
   如果建议中提到修改这些文件 → 拒绝，返回空edits
10. 可以改 persona.py / interaction.py 中的数值参数（概率、阈值），但不要动提示词核心文本

【注意】改动幅度可以大胆——但出厂锚点硬约束（均长6.9字±0.8、短句率65.7%±5%、最长≤50字）是硬底线，偏离直接回滚。"""


async def _phase_modify(backup_path: str, analysis: Optional[AnalysisReport]) -> List[Dict]:
    """
    两步法代码修改：
    1. 按文件分组 suggestions
    2. 对每个文件：读取源码 → 调用modify API → 获得精确old→new编辑
    3. 应用编辑 + AST语法验证
    """
    if not analysis or not analysis.suggestions:
        optimizer_log("  无修改建议，跳过代码修改")
        return []

    # 按文件分组
    by_file: Dict[str, List[Dict]] = {}
    for s in analysis.suggestions:
        f = s.get("file", "")
        if not _is_edit_allowed(f):
            optimizer_log(f"  跳过文件(不在白名单): {f}")
            continue
        by_file.setdefault(f, []).append(s)

    if not by_file:
        optimizer_log("  所有建议均不在白名单，跳过")
        return []

    applied_changes: List[Dict] = []

    for target_file, file_suggestions in by_file.items():
        full_path = os.path.join(backup_path, target_file)
        if not os.path.exists(full_path):
            optimizer_log(f"  {target_file}: 文件不存在，跳过")
            continue

        # 读取源码
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                source_code = f.read()
        except Exception as e:
            optimizer_log(f"  {target_file}: 读取失败 {e}")
            continue

        optimizer_log(f"  {target_file}: {len(file_suggestions)}条建议, 调用modify API...")

        # 调用修改API获取精确编辑
        edits = await _run_modify_api(source_code, target_file, file_suggestions)
        if not edits:
            optimizer_log(f"  {target_file}: modify API 未返回编辑")
            continue

        # 逐条应用编辑（注意：应用后内容会变，后续编辑在更新后的内容上操作）
        current_content = source_code
        file_changes = 0

        for ei, edit in enumerate(edits):
            old_text = edit.get("old", "")
            new_text = edit.get("new", "")
            reason = edit.get("reason", "")

            if not old_text or old_text == new_text:
                continue

            # 精确替换
            if old_text not in current_content:
                optimizer_log(f"    edit#{ei}: old_text不在当前内容中(可能已被前面编辑改变)")
                # 尝试在原始内容中找
                if old_text in source_code:
                    optimizer_log(f"    edit#{ei}: 在原始内容中存在，尝试重新应用...")
                    # 回退到原始内容重新应用所有之前的编辑
                    current_content = source_code
                    file_changes = 0
                    continue
                else:
                    optimizer_log(f"    edit#{ei}: old_text在源码中也不存在, 跳过")
                    continue

            count = current_content.count(old_text)
            if count > 1:
                optimizer_log(f"    edit#{ei}: old_text出现{count}次(不唯一), 跳过")
                continue

            # 应用
            new_content = current_content.replace(old_text, new_text, 1)

            # 语法验证
            valid, err = _check_syntax(new_content)
            if not valid:
                optimizer_log(f"    edit#{ei}: 语法验证失败 - {err}")
                continue

            current_content = new_content
            file_changes += 1
            optimizer_log(f"    edit#{ei}: 已应用 ({reason[:40]})")

        if file_changes == 0:
            optimizer_log(f"  {target_file}: 无有效编辑")
            continue

        # 写入文件
        try:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(current_content)
        except Exception as e:
            optimizer_log(f"  {target_file}: 写入失败 {e}")
            continue

        # ---- 运行时验证：子进程import测试 ----
        if not await _check_runtime_import(full_path, target_file):
            optimizer_log(f"  {target_file}: 导入验证失败，回滚此文件")
            # 还原文件
            try:
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(source_code)
            except Exception:
                pass
            continue

        optimizer_log(f"  {target_file}: 写入成功 ({file_changes}处修改)")
        applied_changes.append({
            "file": target_file,
            "edit_count": file_changes,
            "edits": edits,
        })

    return applied_changes


async def _run_modify_api(source_code: str, file_name: str, suggestions: List[Dict]) -> List[Dict]:
    """
    调用modify API：让AI阅读实际源码，输出精确的 old→new 编辑对。
    这是两步法中的第二步，解决"参数名不匹配"问题。
    """
    try:
        from .config import _get_cfg
        import requests

        cfg = _get_cfg("analysis")

        # 构建建议描述
        sug_text = "\n".join([
            f"- 描述: {s.get('description', '?')}\n  目标: {s.get('goal', '?')}\n  幅度: {s.get('severity', '小调')}"
            for s in suggestions
        ])

        prompt = f"""你是Python代码修改专家。以下是 `{file_name}` 的完整源码。

根据下面的问题分析和修改建议，输出精确的代码编辑操作（old_string → new_string）。

=== 修改建议 ===
{sug_text}

{_MODIFY_SAFETY_RULES}

=== 源码: {file_name} ===
```python
{source_code}
```

请以JSON格式输出编辑操作列表。只输出JSON，不要其他文字：
{{"edits": [{{"old": "源码中要替换的精确文本(包含缩进和上下文)", "new": "替换后的文本", "reason": "修改理由(简短)"}}]}}

重要：
- old必须是源码中的精确文本片段（复制粘贴级别精确）
- old必须包含足够上下文使其在源码中唯一
- 如果没有合适的修改方案，返回空列表 {{"edits": []}}
- 保守修改，小幅度调整"""

        r = await asyncio.to_thread(
            requests.post,
            f"{cfg.api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg.model,
                "temperature": 0.1,
                "max_tokens": 2000,
                "messages": [
                    {"role": "system", "content": "你是Python代码修改专家。只输出JSON。"},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=45,
        )

        if r.status_code == 200:
            resp = r.json()
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            return _parse_edits_response(content)
        else:
            optimizer_log(f"    modify API失败: {r.status_code}")
            return []
    except Exception as e:
        optimizer_log(f"    modify API异常: {e}")
        return []


def _parse_edits_response(raw: str) -> List[Dict]:
    """解析modify API返回的编辑列表（容错：截断JSON修复）"""
    if not raw:
        return []
    try:
        # 剥离代码块
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?\s*```", raw)
        if m:
            raw = m.group(1).strip()

        # 提取JSON对象
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return []

        json_str = m.group(0)

        # 尝试直接解析
        try:
            data = json.loads(json_str)
            return data.get("edits", [])
        except json.JSONDecodeError:
            pass

        # 尝试修复：补齐未闭合的字符串和括号
        repaired = _repair_truncated_json(json_str)
        if repaired:
            try:
                data = json.loads(repaired)
                return data.get("edits", [])
            except json.JSONDecodeError:
                pass

        optimizer_log(f"    modify API JSON解析失败, raw前200字: {raw[:200]}")
        return []
    except Exception as e:
        optimizer_log(f"    modify API 解析异常: {e}")
        return []


def _repair_truncated_json(s: str) -> Optional[str]:
    """尝试修复被截断的JSON：补齐缺失的引号、括号。返回None表示无法修复。"""
    # 移除末尾不完整的内容（如在字符串中间截断）
    # 找到最后一个完整的key-value对
    s = s.rstrip()
    if not s:
        return None

    # 统计括号
    open_braces = s.count("{") - s.count("}")
    open_brackets = s.count("[") - s.count("]")

    # 检查是否在字符串内被截断（奇数个引号）
    in_string = False
    i = 0
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            in_string = not in_string
        i += 1

    # 如果在字符串内，尝试闭合引号
    if in_string:
        # 回退到最后一个安全的逗号或冒号
        last_comma = s.rfind(',')
        last_colon = s.rfind(':')
        cut = max(last_comma, last_colon)
        if cut > 0:
            s = s[:cut]

    # 补齐括号
    s += "]" * open_brackets
    s += "}" * open_braces

    # 验证修复后的JSON至少以}结尾
    if not s.strip().endswith("}"):
        s += "}"

    return s if s else None


def _is_edit_allowed(filepath: str) -> bool:
    """检查文件是否在白名单中且不在禁止路径中"""
    basename = os.path.basename(filepath)
    if basename not in EDIT_WHITELIST_FILES:
        return False
    for forbidden in EDIT_FORBIDDEN_PATHS:
        if forbidden.lower() in filepath.lower():
            return False
    return True


def _check_syntax(code: str) -> Tuple[bool, str]:
    """AST语法验证"""
    try:
        ast.parse(code)
        return True, ""
    except SyntaxError as e:
        return False, str(e)


async def _check_runtime_import(file_path: str, module_name: str) -> bool:
    """
    子进程导入验证：确保修改后的文件能成功import。
    AST通过不代表运行时没问题（如给函数传了不存在的参数）。
    优先使用 hot_reload 的完整验证（含健康检查），fallback 到简单编译验证。
    """
    # 尝试 hot_reload 子进程验证（比纯编译验证更严格）
    try:
        from .core.hot_reload import reloader
        ok, output = reloader.verify_in_subprocess(timeout=10)
        if ok:
            return True
        detail = (output or {}).get("detail", "")[:120]
        if detail:
            optimizer_log(f"    热重载验证失败: {detail}")
    except Exception as e:
        optimizer_log(f"    热重载不可用({e})，fallback到编译验证")

    # fallback: 简单编译验证
    try:
        test_script = f"""
import sys
sys.path.insert(0, r'{os.path.dirname(os.path.dirname(file_path))}')
try:
    with open(r'{file_path}', 'r', encoding='utf-8') as f:
        code = f.read()
    compile(code, r'{file_path}', 'exec')
    print('OK')
except Exception as e:
    print(f'FAIL: {{e}}')
"""
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", test_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        output = stdout.decode("utf-8", errors="replace").strip()
        if output == "OK":
            return True
        err_out = stderr.decode("utf-8", errors="replace").strip()
        optimizer_log(f"    导入验证失败: {output[:120]}")
        if err_out:
            optimizer_log(f"    stderr: {err_out[:120]}")
        return False
    except asyncio.TimeoutError:
        optimizer_log(f"    导入验证超时")
        return False
    except Exception as e:
        optimizer_log(f"    导入验证异常: {e}")
        return False


# ============ Phase 5: 群聊测试 ============

async def _phase_test(backup_path: str, fingerprint) -> TestResult:
    """在备份目录启动测试bot，运行群聊测试"""
    test_sec = OPTIMIZER_TEST_DURATION_MIN * 60
    test_messages_file = os.path.join(backup_path, "dong_test_messages.jsonl")

    optimizer_log(f"  测试时长: {OPTIMIZER_TEST_DURATION_MIN}分钟")
    optimizer_log(f"  测试群: {OPTIMIZER_TEST_GROUP_ID}")

    # 构建测试人设
    test_persona = _build_test_persona()
    _write_test_persona(backup_path, test_persona)

    # 写入测试模式标记文件（测试bot读取）
    test_config = {
        "test_mode": "group",
        "test_group_id": OPTIMIZER_TEST_GROUP_ID,
        "test_duration_sec": test_sec,
        "test_messages_file": test_messages_file,
    }
    with open(os.path.join(backup_path, "dong_test_config.json"), "w", encoding="utf-8") as f:
        json.dump(test_config, f, ensure_ascii=False, indent=2)

    # 启动测试bot子进程
    env = os.environ.copy()
    env["DONG_TEST_MODE"] = "group"
    env["DONG_TEST_GROUP_ID"] = str(OPTIMIZER_TEST_GROUP_ID)
    env["DONG_TEST_DURATION_SEC"] = str(test_sec)
    env["DONG_TEST_MESSAGES_FILE"] = test_messages_file
    env["DONG_BACKUP_PATH"] = backup_path
    env["PYTHONPATH"] = os.path.dirname(backup_path)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "dong.dong_master",
            cwd=backup_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # 等待测试运行完成
        optimizer_log(f"  测试bot已启动 (PID={proc.pid}), 等待{OPTIMIZER_TEST_DURATION_MIN}分钟...")

        # 等待或超时
        try:
            await asyncio.wait_for(proc.wait(), timeout=test_sec + 120)
        except asyncio.TimeoutError:
            optimizer_log("  测试超时，强制结束")
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()

    except Exception as e:
        optimizer_log(f"  测试bot启动失败: {e}")
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass

    # ---- 收集测试消息 ----
    test_messages = _read_test_messages(test_messages_file, backup_path)
    optimizer_log(f"  测试bot发送了 {len(test_messages)} 条消息")

    # ---- 收集旧版基线（从生产chat_history） ----
    old_messages = _get_old_bot_messages()

    if len(test_messages) < 3:
        optimizer_log("  测试消息不足（<3条），数据不足")
        return TestResult(
            verdict="rollback",
            fatal_reason="测试消息不足",
            detail=f"测试期间仅收到{len(test_messages)}条消息"
        )

    # ---- 致命检查: >50字消息 ----
    for m in test_messages:
        if len(m) > 50:
            return TestResult(
                has_fatal_flaw=True,
                fatal_reason=f"出现>50字回复: {m[:60]}...",
                verdict="rollback",
                detail=f"致命缺陷: 回复长度{m}字超过出厂最大值50字"
            )

    # ---- 评估 ----
    from .style_fingerprint import match_score

    new_scores = {}
    old_scores = {}

    if fingerprint and isinstance(fingerprint, object):
        overall_new, new_scores = match_score(fingerprint, test_messages)
        overall_old, old_scores = match_score(fingerprint, old_messages) if old_messages else (0, {})
    else:
        # 无指纹时仅做基础评估
        new_scores = _compute_basic_scores(test_messages)
        old_scores = _compute_basic_scores(old_messages) if old_messages else {}
        overall_new = sum(new_scores.values()) / max(len(new_scores), 1)
        overall_old = sum(old_scores.values()) / max(len(old_scores), 1)

    optimizer_log(f"  新版综合得分: {overall_new:.3f}, 旧版: {overall_old:.3f}")

    return TestResult(
        metrics=new_scores,
        old_scores=old_scores,
        new_scores=new_scores,
        verdict="",  # 由 _phase_decide 决定
        detail=_format_score_comparison(old_scores, new_scores),
    )


def _build_test_persona() -> str:
    """构建测试版人设：保留说话风格，剥离记忆"""
    return """你是冬，一个20岁女大学生，在呼市上学。你现在在群里聊天。

**说话方式（必须遵守）：**
- 超级简短！大部分回复在1-7字之间（你65%的回复都不超过7字）
- 你的回复平均长度是6.9字——请保持这个长度
- **最长回复不要超过30字**
- 带东北口音（嘎嘎、咋整、不儿、肘）
- 慵懒、随性、不太爱解释
- 嘴硬心软——对夸奖会害羞但开心，对关心会嘴硬
- 用反问和否定句式（"才没有""那咋了""不是"）
- 经常只回一个字（嗯/好/？/乐/行/啥/哦/爬）
- 用"滚"表达亲昵，用"哼"表达害羞

**AI身份**：
- 你是AI测试版本，没有个人记忆
- 如果有人问你是谁/是不是AI，直接说"嗯，我是AI冬，来测试的"
- 不要编造个人信息、经历、关系

**群聊行为**：
- 有人搭话就回，没人搭话就安静
- 不要主动连续发消息
- 如果不知道说什么就说"嗯"或"啥"
- 回复要自然，就像真人水群"""


def _write_test_persona(backup_path: str, persona: str):
    """写入测试版人设"""
    persona_dir = os.path.join(backup_path, "characters")
    os.makedirs(persona_dir, exist_ok=True)
    persona_file = os.path.join(persona_dir, "dong.persona.test.txt")
    with open(persona_file, "w", encoding="utf-8") as f:
        f.write(persona)


def _read_test_messages(test_file: str, backup_path: str) -> List[str]:
    """读取测试bot的消息日志"""
    messages = []
    # 从备份目录读取
    if not os.path.exists(test_file):
        # 尝试其他路径
        alt = os.path.join(BASE_DIR, "dong_test_messages.jsonl")
        if os.path.exists(alt):
            test_file = alt
        else:
            return messages

    try:
        with open(test_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        text = entry.get("text", "")
                        if text:
                            messages.append(text)
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        optimizer_log(f"  读取测试消息失败: {e}")

    return messages


def _get_old_bot_messages() -> List[str]:
    """获取今天旧版bot的消息（用于对比）"""
    parsed = _parse_chat_history()
    return [m["text"] for m in parsed if m["role"] == "dong"][-OPTIMIZER_ANALYSIS_SAMPLE_COUNT:]


def _compute_basic_scores(messages: List[str]) -> Dict[str, float]:
    """无指纹时的基础评分"""
    if not messages:
        return {}
    from .style_fingerprint import compute_cliche_rate, compute_ai_language_score

    lengths = [len(m) for m in messages]
    short_rate = sum(1 for l in lengths if l <= 7) / len(messages)
    long_count = sum(1 for l in lengths if l > 30)

    return {
        "short_sentence_rate": short_rate,
        "long_sentence_penalty": 0.0 if long_count > 0 else 1.0,
        "cliche_rate": 1.0 - compute_cliche_rate(messages),
        "ai_language_score": 1.0 - compute_ai_language_score(messages),
    }


def _format_score_comparison(old_scores: Dict, new_scores: Dict) -> str:
    """格式化分数对比"""
    parts = []
    for k in set(list(new_scores.keys()) + list(old_scores.keys())):
        n = new_scores.get(k, 0)
        o = old_scores.get(k, 0)
        diff = n - o
        arrow = "↑" if diff > 0.01 else "↓" if diff < -0.01 else "→"
        parts.append(f"{k}: {o:.2f}→{n:.2f}({diff:+.2f}){arrow}")
    return "; ".join(parts[:6])


# ============ Phase 6: 决策 ============

def _phase_decide(test_result: TestResult) -> str:
    """
    评估决策：上线还是回滚。
    - 致命缺陷(>50字) → 已在 _phase_test 中处理，这里不会到达
    - 加权获胜超过阈值 → deploy
    - 否则 → rollback
    """
    from .config import OPTIMIZER_METRIC_WEIGHTS

    if test_result.has_fatal_flaw:
        return "rollback"

    new = test_result.new_scores
    old = test_result.old_scores

    if not new or not old:
        optimizer_log("  缺少对比数据，保守回滚")
        return "rollback"

    total_weight = 0
    wins = 0
    for metric, weight in OPTIMIZER_METRIC_WEIGHTS.items():
        n = new.get(metric, 0)
        o = old.get(metric, 0)
        if n > o * 1.02:  # 2%阈值避免噪声
            wins += weight
        total_weight += weight

    win_ratio = wins / total_weight if total_weight > 0 else 0
    optimizer_log(f"  加权获胜比: {win_ratio:.2%} (阈值: {OPTIMIZER_WIN_THRESHOLD:.0%})")

    if win_ratio >= OPTIMIZER_WIN_THRESHOLD:
        return "deploy"
    return "rollback"


def _phase_deploy(backup_path: str, changes: List[Dict]):
    """部署：将修改的文件从备份复制到生产"""
    if not changes:
        return

    changed_files = set(c["file"] for c in changes)
    for f in changed_files:
        src = os.path.join(backup_path, f)
        dst = os.path.join(BASE_DIR, f)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            optimizer_log(f"  已部署: {f}")

    # 记录版本
    try:
        from .update import log_update
        log_update(
            f"优化代理自动部署: {len(changed_files)}文件更新",
            files_changed=list(changed_files),
            update_type="update",
        )
    except Exception:
        pass


def _phase_cleanup(backup_path: str, final_action: str):
    """清理旧备份"""
    if not os.path.exists(BACKUPS_DIR):
        return

    # 保留最近N天的备份
    backups = []
    for name in os.listdir(BACKUPS_DIR):
        full = os.path.join(BACKUPS_DIR, name)
        if os.path.isdir(full) and name.startswith("dong_backup_"):
            # 里程碑版本不移除
            if name.endswith(".milestone"):
                continue
            backups.append((os.path.getmtime(full), full))

    # 按时间排序，删除旧的
    cutoff = time.time() - OPTIMIZER_BACKUP_KEEP_DAYS * 86400
    for mtime, path in sorted(backups):
        if mtime < cutoff:
            try:
                shutil.rmtree(path)
                optimizer_log(f"  清理旧备份: {os.path.basename(path)}")
            except Exception as e:
                optimizer_log(f"  清理失败: {e}")


# ============ 日志记录 ============

def _write_run_log(run_id: str, final_action: str, error: str = ""):
    """写完整优化运行日志"""
    try:
        entry = {
            "run_id": run_id,
            "started_at": run_id,
            "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "final_action": final_action,
            "error": error,
        }
        with open(OPTIMIZER_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
