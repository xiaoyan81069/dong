"""
Agent Loop v3 — 子任务栈 + 暂停恢复 + 障碍检测
"""
import asyncio, json, base64, os, re, subprocess, time, tempfile
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
import requests
from .log import log
from .config import _get_cfg

MAX_AGENT_STEPS = 15
PAUSED_TASKS_FILE = os.path.join(os.path.dirname(__file__), "agent_paused_tasks.json")
PENDING_PLANS_FILE = os.path.join(os.path.dirname(__file__), "agent_pending_plans.json")
STATUS_FILE = os.path.join(os.path.dirname(__file__), "dong_status.json")

# ── 暂停会话 ──
@dataclass
class PausedSession:
    subtask_stack: List[str]
    history: List[str]
    uid: int
    task_desc: str
    paused_at_step: int = 0

_paused: Dict[int, PausedSession] = {}
_paused_tasks = _paused
_pending_plans: Dict[int, Dict] = {}
_last_failed_steps: Dict[int, Dict] = {}

def _classify_failure(message: str) -> str:
    text = (message or "").lower()
    if any(k in text for k in ("timeout", "timed out", "超时", "network", "connection", "429", "503")):
        return "network_timeout"
    if any(k in text for k in ("syntaxerror", "py_compile", "invalid syntax", "语法")):
        return "syntax_error"
    if any(k in text for k in ("permission", "access denied", "权限", "denied", "forbidden")):
        return "permission_denied"
    if any(k in text for k in ("not_found", "找不到", "未找到", "不存在")):
        return "target_not_found"
    return "unknown"

def _retry_hint(reason: str) -> str:
    return {
        "network_timeout": "网络/接口超时，等待后原步骤重试。",
        "syntax_error": "语法错误，要求重新生成可执行动作后重试。",
        "permission_denied": "权限不足，改用键盘/窗口聚焦/替代路径执行。",
        "target_not_found": "目标未找到，重新观察界面并换定位方式。",
        "unknown": "结果未达成，重新观察后调整动作。",
    }.get(reason, "重新观察后调整动作。")

def _record_failed_step(uid: int, subs: List[str], history: List[str], task: str, step: int, reason: str, detail: str):
    _last_failed_steps[uid] = {
        "subs": list(subs),
        "history": list(history[-20:]),
        "task": task,
        "step": step,
        "reason": reason,
        "detail": detail,
        "created_at": time.time(),
    }

def _needs_retry(result: str) -> bool:
    text = (result or "").lower()
    return any(k in text for k in (
        "失败", "错误", "超时", "权限", "找不到", "未找到",
        "failed", "error", "timeout", "denied", "not found"
    ))

def has_paused(uid: int) -> bool:
    _load_paused_tasks()
    return uid in _paused

def clear_paused(uid: int):
    _paused.pop(uid, None)
    _save_paused_tasks()

def _session_to_dict(s: PausedSession) -> Dict:
    return {
        "subtask_stack": s.subtask_stack,
        "history": s.history,
        "uid": s.uid,
        "task_desc": s.task_desc,
        "paused_at_step": s.paused_at_step,
    }

def _save_paused_tasks():
    try:
        data = {str(uid): _session_to_dict(s) for uid, s in _paused.items()}
        tmp = PAUSED_TASKS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PAUSED_TASKS_FILE)
    except Exception as e:
        log(f"[Agent] 保存暂停任务失败: {e}")

def _load_paused_tasks():
    if _paused:
        return
    if not os.path.exists(PAUSED_TASKS_FILE):
        return
    try:
        with open(PAUSED_TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for uid_s, raw in data.items():
            uid = int(uid_s)
            _paused[uid] = PausedSession(
                subtask_stack=list(raw.get("subtask_stack", [])),
                history=list(raw.get("history", [])),
                uid=uid,
                task_desc=raw.get("task_desc", ""),
                paused_at_step=int(raw.get("paused_at_step", 0)),
            )
    except Exception as e:
        log(f"[Agent] 加载暂停任务失败: {e}")

def list_tasks(uid: int = 0) -> str:
    _load_paused_tasks()
    items = [(k, v) for k, v in _paused.items() if not uid or k == uid]
    if not items:
        return "没有进行中的任务。"
    lines = ["进行中的任务："]
    for task_uid, s in items:
        cur = s.subtask_stack[0] if s.subtask_stack else "等待恢复"
        lines.append(f"- QQ{task_uid}: {s.task_desc[:60]} | 当前: {cur} | step={s.paused_at_step}")
    return "\n".join(lines)

def _save_pending_plans():
    try:
        data = {str(uid): plan for uid, plan in _pending_plans.items()}
        tmp = PENDING_PLANS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PENDING_PLANS_FILE)
    except Exception as e:
        log(f"[Agent] 保存待确认计划失败: {e}")

def _load_pending_plans():
    if _pending_plans or not os.path.exists(PENDING_PLANS_FILE):
        return
    try:
        with open(PENDING_PLANS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for uid_s, plan in data.items():
            _pending_plans[int(uid_s)] = plan
    except Exception as e:
        log(f"[Agent] 加载待确认计划失败: {e}")

def _is_complex_command(task: str) -> bool:
    if any(k in task for k in BATCH_KEYWORDS):
        return True
    return sum(1 for k in ("修", "检查", "审查", "打开", "发送", "下载", "安装", "全部", "所有") if k in task) >= 2

def _build_execution_plan(task: str, status_context: str) -> List[str]:
    if "健康检查" in task:
        return ["读取 health_registry / 健康状态", "逐项检查失败项", "定位相关代码", "应用修复", "运行 py_compile 验证"]
    if "全部修复" in task or "全修" in task or "把全部" in task:
        return ["定位相关 Python 文件", "逐文件审查问题", "应用可安全自动修复项", "统一 py_compile 验证", "汇报修改清单"]
    if "批量检查" in task or "全部检查" in task:
        return ["定位候选文件", "逐文件执行 review_code", "合并问题报告", "标注行号和修复建议"]
    subs = _decompose_complex_command(task) or []
    if subs:
        return [s.replace("code_review:", "审查 ").replace("code_fix:", "修复 ") for s in subs]
    return ["理解目标", "拆解子任务", "逐步执行", "验证结果", "汇报"]

def _format_plan(task: str, plan: List[str]) -> str:
    lines = [f"计划：{task}"]
    for i, step in enumerate(plan, 1):
        lines.append(f"{i}) {step}")
    lines.append("确认执行请回复 /d ok 或 ok；取消请回复 /d cancel。")
    return "\n".join(lines)

def _load_status_snapshot() -> Dict:
    try:
        if not os.path.exists(STATUS_FILE):
            return {}
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        hormones = data.get("hormones", {}) if isinstance(data.get("hormones"), dict) else {}
        return {
            "mood": data.get("mood"),
            "fatigue": data.get("fatigue"),
            "sleeping": data.get("sleeping"),
            "hormones": {
                k: hormones.get(k) for k in ("dopamine", "serotonin", "cortisol", "adrenaline", "oxytocin")
                if k in hormones
            },
        }
    except Exception as e:
        log(f"[Agent] 读取状态失败: {e}")
        return {}

def _status_prompt(snapshot: Dict) -> str:
    if not snapshot:
        return "状态快照：未知。"
    hormones = snapshot.get("hormones") or {}
    hormone_text = ", ".join(f"{k}={v}" for k, v in hormones.items()) or "无"
    return (
        "状态快照："
        f"mood={snapshot.get('mood', '未知')}, "
        f"fatigue={snapshot.get('fatigue', '未知')}, "
        f"sleeping={snapshot.get('sleeping', False)}, "
        f"hormones({hormone_text})。"
        "行为约束：疲劳高时回复短并少做耗时操作；休眠时拒绝非必要耗时操作；心情差时语气克制。"
    )

def _is_costly_task(task: str) -> bool:
    return any(k in task for k in ("全部", "批量", "下载", "安装", "录制", "视频", "打开", "操作", "修复"))

def _apply_status_policy(task_desc: str, snapshot: Dict) -> Optional[str]:
    if snapshot.get("sleeping") and _is_costly_task(task_desc):
        return "冬现在在休眠，先不执行耗时操作。可以用 tasks 查看已有任务，或等醒来后 continue。"
    return None

def _shape_status_reply(message: str, snapshot: Dict) -> str:
    fatigue = snapshot.get("fatigue")
    mood = snapshot.get("mood")
    try:
        fatigue_high = fatigue is not None and float(fatigue) >= 75
        mood_low = mood is not None and float(mood) < 40
    except Exception:
        fatigue_high = mood_low = False
    if fatigue_high and len(message) > 500:
        message = message[:500].rstrip() + "\n...（疲劳较高，先给短结果）"
    if mood_low:
        message = "我会克制一点处理。\n" + message
    return message

_last_nodes = []

# ── Prompts ──
PLANNER_PROMPT = """拆解任务为子步骤。遇到需登录的软件加"处理登录"。JSON: {"subtasks":["步骤1","步骤2",...]}"""

OBSTACLE_PROMPT = """屏幕上有需要优先处理的障碍吗(登录/弹窗/错误)?
子任务:{subtask}
上一步:{last}
控件:{tree}
输出JSON: {{"has_obstacle":false}} 或 {{"has_obstacle":true,"desc":"描述"}}"""

SUBTASK_DONE_PROMPT = """判断子任务是否完成。JSON: {{"done":false}} 或 {{"done":true,"reason":"原因"}}
子任务:{subtask} 上一步结果:{last} 当前控件:{elements}"""

VLM_PROMPT = """桌面操作助手。看截图+控件树输出意图JSON:
{"thought":"思考","action_type":"click/type/hotkey/launch/NEED_HELP","target_name":"目标","target_type":"Button/Edit","text":"内容","window":"窗口","help_msg":"需要人工做什么(仅NEED_HELP)"}"""

RESOLVER_PROMPT = """根据意图从控件树找ID。只输出动作文本，不要JSON包裹，不要引号，不要多余文字。
正确: click(id=18)
错误: {"action": "click(id=18)"}
动作: click(id=N) / type(text=X,window=Y) / hotkey(keys=K,window=Y) / launch(app=X) / NOT_FOUND"""

BATCH_KEYWORDS = ("批量检查", "全部检查", "全部修复", "都修了", "全修", "把全部", "所有")

# ── JSON解析 ──
def _parse_json(raw: str) -> Optional[Dict]:
    """鲁棒JSON提取"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 去掉markdown包裹
    raw = re.sub(r'^```\w*\s*', '', raw).rstrip('`').strip()
    m = re.search(r'\{[\s\S]*\}', raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ── 规划器 ──
def _local_match(task: str) -> Optional[List[str]]:
    import re as _re
    m = _re.search(r'打开(.{2,4}?)输入(.+)', task)
    if m:
        app_map = {"记事本":"notepad","微信":"weixin","cmd":"cmd","计算器":"calc","qq":"qq"}
        win_map = {"notepad":"记事本","记事本":"记事本","weixin":"微信","微信":"微信","calc":"计算器","qq":"QQ"}
        app = m.group(1).strip()
        text = m.group(2).strip().rstrip("。，,.")
        return [f"launch(app={app_map.get(app,app)})", f"type(text={text},window={win_map.get(app_map.get(app,app),app)})"]
    m = _re.search(r'打开(.{1,3}?)$', task)
    if m and len(task) <= 10:
        app_map = {"记事本":"notepad","微信":"weixin","cmd":"cmd","计算器":"calc","qq":"qq"}
        return [f"launch(app={app_map.get(m.group(1).strip(), m.group(1).strip())})"]
    return None


def _plan_subtasks(task: str, status_context: str = "") -> List[str]:
    complex_subs = _decompose_complex_command(task)
    if complex_subs:
        log(f"[Planner] 复合命令: {complex_subs}")
        return complex_subs

    # 0. 本地按逗号拆解
    import re as _re
    parts = _re.split(r'[，,]\s*', task)
    if len(parts) >= 2:
        subs = [p.strip() for p in parts if p.strip()]
        log(f"[Planner] 本地: {subs}")
        return subs

    cfg = _get_cfg("analysis")
    try:
        r = requests.post(f"{cfg.api_base}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
            json={"model": cfg.model, "max_tokens": 300, "temperature": 0.0,
                  "messages": [{"role":"system","content":PLANNER_PROMPT + "\n" + status_context}, {"role":"user","content":f"任务: {task}"}]},
            timeout=10)
        if r.status_code == 200:
            d = _parse_json(r.json()["choices"][0]["message"]["content"])
            if d:
                subs = d.get("subtasks", [])
                if subs:
                    log(f"[Planner] API: {subs}")
                    return subs
    except Exception as e:
        log(f"[Planner] 异常: {e}")
    return [task]

def _extract_py_targets(task: str, limit: int = 8) -> List[str]:
    targets = []
    for m in re.finditer(r"([A-Za-z0-9_./\\-]+\.py)", task):
        path = m.group(1).replace("\\", "/")
        if path not in targets:
            targets.append(path)
    if targets:
        return targets[:limit]

    base = os.path.dirname(__file__)
    names = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", "dong_backups", "data", "skills"}]
        for name in files:
            if name.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, name), base).replace("\\", "/")
                names.append(rel)
        if len(names) >= limit:
            break
    return names[:limit]

def _decompose_complex_command(task: str) -> Optional[List[str]]:
    normalized = task.strip()
    if not normalized:
        return None
    if "批量检查" in normalized or "全部检查" in normalized:
        return [f"code_review:{p}" for p in _extract_py_targets(normalized)]
    if "全部修复" in normalized or "都修了" in normalized or "全修" in normalized or "把全部" in normalized:
        return [f"code_fix:{p}" for p in _extract_py_targets(normalized)]
    if any(k in normalized for k in BATCH_KEYWORDS):
        parts = re.split(r"[，,；;]\s*", normalized)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) > 1:
            return parts
    return None

async def _run_code_batch(subs: List[str], history: List[str]) -> Dict:
    from . import agent as _agent

    total = len(subs)
    results = []
    for idx, sub in enumerate(list(subs), 1):
        action, _, path = sub.partition(":")
        label = "检查" if action == "code_review" else "修复"
        history.append(f"进度 {idx}/{total}: {label} {path}")
        log(f"[AgentBatch] {idx}/{total} {sub}")

        def _run_once():
            if action == "code_review":
                return _agent._execute_review_code(path)
            review = _agent._execute_review_code(path, "auto-fix")
            if "语法错误" not in review and "[致命]" not in review:
                return f"{path}: 未发现可安全自动修复的致命语法问题。\n{review[:1200]}"
            return f"{path}: 已定位问题，但自动修复需要精确替换上下文。\n{review[:1500]}"

        result = await asyncio.to_thread(_run_once)
        if result.startswith("[错误]"):
            history.append(f"失败 {idx}/{total}: {result[:120]}")
            retry = await asyncio.to_thread(_run_once)
            results.append(f"{idx}. {label} {path}: 重试后 {retry[:500]}")
        else:
            results.append(f"{idx}. {label} {path}: {result[:500]}")
            history.append(f"完成 {idx}/{total}: {label} {path}")

    return {"type": "done", "message": "批量任务完成\n" + "\n\n".join(results)}


# ── 障碍检测 ──
def _detect_obstacle(subtask: str, tree: str, last: str) -> Optional[str]:
    cfg = _get_cfg("analysis")
    try:
        p = OBSTACLE_PROMPT.format(subtask=subtask, tree=tree[:1500], last=last[:200])
        r = requests.post(f"{cfg.api_base}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
            json={"model": cfg.model, "max_tokens": 150, "temperature": 0.0,
                  "messages": [{"role":"user","content":p}]}, timeout=5)
        if r.status_code == 200:
            d = _parse_json(r.json()["choices"][0]["message"]["content"])
            if d and d.get("has_obstacle"):
                log(f"[Obstacle] {d.get('desc','')}")
                return d.get("desc", "未知障碍")
    except Exception as e:
        log(f"[Obstacle] 异常: {e}")
    return None


# ── 子任务完成检查 ──
def _check_done(subtask: str, tree: str, last: str) -> bool:
    cfg = _get_cfg("analysis")
    try:
        el_lines = []
        for line in tree.split("\n")[:15]:
            m = re.match(r'\[\d+\]\s+(\w+)\s+"([^"]+)"', line)
            if m:
                el_lines.append(f"{m.group(1)}:{m.group(2)}")
        p = SUBTASK_DONE_PROMPT.format(subtask=subtask, elements="\n".join(el_lines), last=last[:200])
        r = requests.post(f"{cfg.api_base}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
            json={"model": cfg.model, "max_tokens": 100, "temperature": 0.0,
                  "messages": [{"role":"user","content":p}]}, timeout=5)
        done = False
        if r.status_code == 200:
            d = _parse_json(r.json()["choices"][0]["message"]["content"])
            if d:
                done = d.get("done", False)
            log(f"[SubDone] {subtask[:20]} → {done}")
        return done
    except Exception as e:
        log(f"[SubDone] 异常: {e}")
    return False


# ── VLM 意图 ──
def _call_vlm(prompt: str, img_path: str) -> Optional[Dict]:
    cfg = _get_cfg("vision")
    try:
        with open(img_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        r = requests.post(f"{cfg.api_base}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
            json={"model": cfg.model, "max_tokens": 250, "temperature": 0.1,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role":"system","content": VLM_PROMPT},
                               {"role":"user","content":[{"type":"image_url","image_url":{"url":f"data:image/png;base64,{img_b64}"}},{"type":"text","text":prompt}]}]},
            timeout=25)
        if r.status_code == 200:
            return _parse_json(r.json()["choices"][0]["message"]["content"])
    except Exception as e:
        log(f"[VLM] 异常: {e}")
    return None


# ── 文本解析ID ──
def _resolve(intent: Dict, tree: str) -> Optional[str]:
    cfg = _get_cfg("analysis")
    try:
        r = requests.post(f"{cfg.api_base}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
            json={"model": cfg.model, "max_tokens": 100, "temperature": 0.0,
                  "messages": [{"role":"system","content": RESOLVER_PROMPT},
                               {"role":"user","content": f"意图:{json.dumps(intent,ensure_ascii=False)}\n树:\n{tree[:3000]}"}]},
            timeout=8)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"[Resolver] 异常: {e}")
    return None


# ── 主入口 ──
async def run_computer_task(task_desc: str, uid: int) -> Dict:
    _load_pending_plans()
    status_snapshot = _load_status_snapshot()
    cmd = task_desc.strip().lower()
    if cmd in ("ok", "/d ok", "确认", "执行"):
        plan = _pending_plans.pop(uid, None)
        _save_pending_plans()
        if not plan:
            return {"type": "error", "message": "没有待确认的执行计划。"}
        task_desc = plan.get("task", "")
        if not task_desc:
            return {"type": "error", "message": "待确认计划缺少任务内容。"}
        log(f"[AgentPlan] 确认执行: {task_desc[:80]}")
    elif cmd in ("cancel", "/d cancel", "取消"):
        if uid in _pending_plans:
            _pending_plans.pop(uid, None)
            _save_pending_plans()
            return {"type": "done", "message": "已取消待确认计划。"}
        return {"type": "done", "message": "没有待取消的计划。"}

    if cmd in ("continue", "继续", "/d continue"):
        result = await resume_agent(uid)
        if "message" in result:
            result["message"] = _shape_status_reply(result["message"], status_snapshot)
        return result
    if cmd in ("tasks", "任务", "进行中的任务", "/d tasks"):
        return {"type": "done", "message": _shape_status_reply(list_tasks(uid), status_snapshot)}
    if cmd in ("retry", "重试", "/d retry"):
        failed = _last_failed_steps.get(uid)
        if not failed:
            return {"type": "error", "message": "没有可重试的上一步。"}
        history = list(failed.get("history", []))
        history.append(f"手动重试: {failed.get('reason')} | {failed.get('detail', '')[:120]}")
        result = await _execute(
            list(failed.get("subs", [])),
            history,
            failed.get("task", task_desc),
            uid,
            max(0, int(failed.get("step", 1)) - 1),
        )
        if result.get("type") == "done":
            _last_failed_steps.pop(uid, None)
        if "message" in result:
            result["message"] = _shape_status_reply(result["message"], status_snapshot)
        return result

    blocked = _apply_status_policy(task_desc, status_snapshot)
    if blocked:
        return {"type": "paused", "message": blocked}

    status_context = _status_prompt(status_snapshot)
    task_with_status = f"{status_context}\n\n任务: {task_desc}"

    if cmd not in ("ok", "/d ok", "确认", "执行") and _is_complex_command(task_desc):
        plan = _build_execution_plan(task_desc, status_context)
        _pending_plans[uid] = {
            "task": task_desc,
            "plan": plan,
            "created_at": time.time(),
            "status": status_context,
        }
        _save_pending_plans()
        return {"type": "paused", "message": _format_plan(task_desc, plan)}

    # ── AgentSkills: 技能匹配 → 加载全文 → 增强任务描述 ──
    _skill_name_matched = None
    try:
        from .core.skill_loader import load_skills, match_skill, load_skill_full
        skills = load_skills()
        matched_skill = match_skill(task_desc, skills)
        if matched_skill:
            full = load_skill_full(matched_skill.name)
            if full and full.instructions:
                _skill_name_matched = full.name
                log(f"[Agent] 技能命中: {full.name}")
                task_desc = f"[技能:{full.name}]\n{full.instructions[:500]}\n\n{task_with_status}"
                task_with_status = task_desc
    except Exception as e:
        log(f"[Agent] 技能匹配异常: {e}")

    # 技能记忆：查匹配技能 → 直接回放跳过规划
    try:
        from .core.skill_memory import skill_memory, replay_skill
        matched = skill_memory.query(task_with_status)
        if matched:
            log(f"[Agent] 技能命中: {matched['description'][:25]} (score={matched['score']})")
            result = await replay_skill(matched, uid)
            if result.get("type") == "done":
                return result
            log(f"[Agent] 技能回放失败，回退正常流程")
    except Exception as e:
        log(f"[Agent] 技能查询异常: {e}")

    # 快速简单任务
    fast = _local_match(task_desc)
    if fast:
        log(f"[Agent] 快速模式: {fast}")
        for a in fast:
            _do_action(a)
            await asyncio.sleep(2.0 if "launch" in a else 0.3)
        return {"type": "done", "message": _shape_status_reply("快速完成", status_snapshot)}

    # 规划子任务
    subs = _plan_subtasks(task_desc, status_context)
    saved_subs = list(subs)
    history = [status_context, f"任务: {task_desc} | 子任务: {' → '.join(subs)}"]
    if subs and all(s.startswith(("code_review:", "code_fix:")) for s in subs):
        result = await _run_code_batch(subs, history)
        result["message"] = _shape_status_reply(result.get("message", ""), status_snapshot)
        return result
    result = await _execute(subs, history, task_with_status, uid, 0)
    # 技能记忆：成功执行后存储
    if result.get("type") == "done" and saved_subs:
        try:
            from .core.skill_memory import skill_memory
            skill_memory.store(task_desc, saved_subs)
        except Exception:
            pass
    # ── 自学习：复杂成功任务 → 存为 SKILL.md ──
    if result.get("type") == "done" and len(history) >= 3:
        try:
            from .core.skill_learner import save_learned_skill
            tools_used = list(set(
                step.split(":")[0] if ":" in step else ""
                for step in history if any(t in step for t in ["launch", "click", "type", "hotkey"])
            )) or ["computer_control"]
            learned = save_learned_skill(task_desc, history, tools_used)
            if learned:
                log(f"[Agent] 新技能已学习: {learned}")
                # 重新加载技能列表以包含新技能
        except Exception as e:
            log(f"[Agent] 技能学习异常: {e}")
    if "message" in result:
        result["message"] = _shape_status_reply(result["message"], status_snapshot)
    return result


async def resume_agent(uid: int) -> Dict:
    _load_paused_tasks()
    s = _paused.pop(uid, None)
    if not s:
        return {"type": "error", "message": "没有暂停的任务"}
    _save_paused_tasks()
    s.history.append("用户确认继续")
    # 暂停时的子任务应重试而非跳过
    result = await _execute(s.subtask_stack, s.history, s.task_desc, uid, s.paused_at_step)
    if result.get("type") == "paused":
        _save_paused_tasks()
    return result


async def _execute(subs: List[str], history: List[str], task: str, uid: int, start: int) -> Dict:
    global _last_nodes
    from . import gui_agent
    retries: Dict[str, int] = {}
    max_retries = 3

    for step in range(start + 1, MAX_AGENT_STEPS + 1):
        if not subs:
            return {"type": "done", "message": "完成!\n" + "\n".join(history[-6:])}

        cur = subs[0]
        total = len(subs)
        done_count = len([h for h in history if h.startswith("完成:")])
        history.append(f"进度: {done_count + 1}/{done_count + total} {cur}")
        log(f"[Agent] Step{step} 子任务: {cur}")

        # 1. 观察
        img, vtree, ftree, nodes = gui_agent.generate_ui_tree_and_screenshot(cur)
        _last_nodes = nodes
        if not img:
            reason = _classify_failure("截屏失败")
            count = retries.get(cur, 0) + 1
            retries[cur] = count
            history.append(f"失败分析: {reason} | 截屏失败 | {_retry_hint(reason)}")
            _record_failed_step(uid, subs, history, task, step, reason, "截屏失败")
            if count <= max_retries:
                await asyncio.sleep(1.0)
                continue
            return {"type": "error", "message": "截屏失败，已重试3次"}

        # 2. 障碍检测
        last = history[-1] if history else ""
        obs = await asyncio.to_thread(_detect_obstacle, cur, vtree, last)
        if obs and not cur.startswith("处理障碍"):
            obs_task = f"处理障碍: {obs}"
            subs.insert(0, obs_task)
            history.append(f"障碍: {obs}")
            log(f"[Agent] 插入障碍: {obs_task}")
            continue

        # 3. VLM意图
        vp = f"任务:{task}\n子任务:{cur}\n树:\n{vtree}\n历史:{'; '.join(history[-3:])}"
        intent = await asyncio.to_thread(_call_vlm, vp, img)
        if not intent:
            history.append("VLM失败")
            reason = _classify_failure("VLM timeout")
            count = retries.get(cur, 0) + 1
            retries[cur] = count
            history.append(f"失败分析: {reason} | {_retry_hint(reason)} | {count}/{max_retries}")
            _record_failed_step(uid, subs, history, task, step, reason, "VLM未返回意图")
            if count <= max_retries:
                await asyncio.sleep(1.5)
                continue
            return {"type": "error", "message": "VLM连续失败，已停止。可用 /d retry 重试上一步。"}

        at = intent.get("action_type", "")
        log(f"[Agent] {intent.get('thought','')} | {at}")

        # 4. NEED_HELP → 暂停
        if at == "NEED_HELP":
            msg = intent.get("help_msg", intent.get("text", "需要人工"))
            _paused[uid] = PausedSession(subs, history, uid, task, step)
            _save_paused_tasks()
            return {"type": "paused", "message": f"{msg}\n\n处理完回复「继续」"}

        # 5. 解析动作
        act = _resolve(intent, ftree) if at != "DONE" else "DONE"
        if not act or act == "NOT_FOUND":
            history.append(f"未找到: {intent.get('target_name','')}")
            reason = _classify_failure("NOT_FOUND")
            count = retries.get(cur, 0) + 1
            retries[cur] = count
            history.append(f"失败分析: {reason} | {_retry_hint(reason)} | {count}/{max_retries}")
            _record_failed_step(uid, subs, history, task, step, reason, intent.get("target_name", "NOT_FOUND"))
            if count <= max_retries:
                continue
            return {"type": "error", "message": "目标连续未找到，已停止。可用 /d retry 重试上一步。"}
        if act.startswith("DONE"):
            return {"type": "done", "message": act}

        # 6. 执行
        r = _do_action(act)
        log(f"[Agent] {r[:80]}")
        history.append(f"{act} → {r[:50]}")
        if _needs_retry(r):
            reason = _classify_failure(r)
            count = retries.get(cur, 0) + 1
            retries[cur] = count
            history.append(f"失败分析: {reason} | {_retry_hint(reason)} | {count}/{max_retries}")
            _record_failed_step(uid, subs, history, task, step, reason, r)
            if reason == "permission_denied":
                history.append("换方案: 优先尝试窗口聚焦、键盘快捷键或可访问控件。")
            if count <= max_retries:
                await asyncio.sleep(1.0)
                continue
            return {"type": "error", "message": f"动作执行失败，已重试3次: {r}\n可用 /d retry 手动重试。"}

        # 7. 检查完成
        await asyncio.sleep(1.5)
        _, vtree2, _, _ = gui_agent.generate_ui_tree_and_screenshot(cur)
        if await asyncio.to_thread(_check_done, cur, vtree2, r):
            done = subs.pop(0)
            history.append(f"完成: {done}")
            retries.pop(cur, None)
            _last_failed_steps.pop(uid, None)
        else:
            reason = _classify_failure("未完成")
            count = retries.get(cur, 0) + 1
            retries[cur] = count
            history.append(f"未完成，调整后重试: {cur} | {count}/{max_retries}")
            history.append(f"失败分析: {reason} | {_retry_hint(reason)}")
            _record_failed_step(uid, subs, history, task, step, reason, "完成检查未通过")
            log(f"[Agent] 重试子任务: {cur}")
            if count > max_retries:
                return {"type": "error", "message": "完成检查连续失败，已停止。可用 /d retry 手动重试。"}

        await asyncio.sleep(0.5)

    return {"type": "error", "message": "超时"}


# ── 动作执行 ──
def _do_action(action_str: str) -> str:
    action_str = action_str.strip()
    action_str = re.sub(r'^```\w*\s*', '', action_str).rstrip('`').strip()

    win = ""
    m = re.search(r',?\s*window\s*=\s*([^,\)]+)', action_str)
    if m:
        win = m.group(1).strip().strip('"').strip("'")
        action_str = action_str[:m.start()] + action_str[m.end():]
        action_str = action_str.replace("(,", "(").replace(", )", ")").replace(",,", ",")

    # launch
    m = re.search(r'launch\s*\(\s*app\s*=\s*(.+?)\s*\)', action_str, re.IGNORECASE)
    if m:
        return _do_launch(m.group(1).strip().strip('"').strip("'"))

    # click(id=N)
    m = re.match(r'click\s*\(\s*id\s*=\s*(\d+)\s*\)', action_str, re.IGNORECASE)
    if m:
        tid = int(m.group(1))
        for el in _last_nodes:
            if el.get("id") == tid:
                try:
                    import pyautogui as _pg
                    _pg.click(el["center"][0], el["center"][1])
                    return f"点击[{tid}] {el.get('name','')[:20]}"
                except Exception as e:
                    return f"点击失败: {e}"
        return f"找不到[{tid}]"

    # click(x,y)
    m = re.search(r'click\s*\(\s*x\s*=\s*(\d+)\s*,\s*y\s*=\s*(\d+)\s*\)', action_str, re.IGNORECASE)
    if m:
        import pyautogui as _pg
        _pg.click(int(m.group(1)), int(m.group(2)))
        return f"点击({m.group(1)},{m.group(2)})"

    # type
    m = re.search(r'type\s*\(\s*text\s*=\s*(.+?)\s*\)', action_str, re.IGNORECASE)
    if m:
        return _do_type(m.group(1).strip().strip('"').strip("'").rstrip(","), win)

    # hotkey
    m = re.search(r'hotkey\s*\(\s*keys?\s*=\s*(.+?)\s*\)', action_str, re.IGNORECASE)
    if m:
        keys = m.group(1).strip().strip('"').strip("'")
        if win:
            from .tools import _ensure_window_focused
            _ensure_window_focused(win)
        import pyautogui as _pg
        _pg.hotkey(*keys.split("+"))
        return f"快捷键: {keys}"

    # press
    m = re.search(r'press\s*\(\s*key\s*=\s*(.+?)\s*\)', action_str, re.IGNORECASE)
    if m:
        import pyautogui as _pg
        _pg.press(m.group(1).strip().strip('"').strip("'"))
        return f"按键: {m.group(1)[:10]}"

    # NEED_HELP
    if action_str.startswith("NEED_HELP"):
        m = re.search(r'text\s*=\s*(.+?)\s*\)', action_str)
        t = m.group(1).strip().strip('"').strip("'") if m else "需要人工"
        return f"求助: {t}"

    # describe
    m = re.search(r'describe\s*\(\s*text\s*=\s*(.+?)\s*\)', action_str, re.IGNORECASE)
    if m:
        t = m.group(1).strip().strip('"').strip("'")
        return f"观察: {t}"

    return f"无法解析: {action_str[:40]}"


def _do_type(text: str, win: str = "") -> str:
    if win:
        from .tools import _ensure_window_focused
        tmap = {"notepad":"记事本","记事本":"记事本","wechat":"微信","微信":"微信","calc":"计算器","计算器":"计算器","cmd":"命令提示符","qq":"QQ"}
        _ensure_window_focused(tmap.get(win.lower(), win))
    try:
        import pyautogui as _pg
        tmp = tempfile.NamedTemporaryFile(mode='wb', suffix='.txt', delete=False)
        try:
            tmp.write(text.encode('gbk', errors='replace'))
            tmp.close()
            # 安全剪贴板写入：用文件重定向代替shell管道
            with open(tmp.name, 'r', encoding='gbk', errors='replace') as _tf:
                subprocess.run(["clip"], stdin=_tf, timeout=5)
            time.sleep(0.2)
            _pg.hotkey("ctrl", "v")
            return f"已输入: {text}"
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
    except Exception as e:
        return f"输入失败: {e}"


# ════════════════════════════════════════════
# 自然语言 /d 命令解析
# ════════════════════════════════════════════

# 意图→固定命令映射表
_INTENT_MAP = {
    "fix": {
        "keywords": ["修", "改", "fix", "修复", "加", "删", "去掉", "替换", "改一下", "修一下"],
        "template": "fix {target} {params}",
        "get_cmd": lambda target, params: f"fix {target} {params}" if target else f"fix {params}",
    },
    "review": {
        "keywords": ["看", "检查", "审查", "review", "代码质量", "有没有问题", "帮我看"],
        "template": "review {target}",
        "get_cmd": lambda target, params: f"review {target}" if target else "review agent.py",
    },
    "query": {
        "keywords": ["查", "找", "在哪", "是什么", "定位", "locate", "搜索", "grep"],
        "template": "locate {target}",
        "get_cmd": lambda target, params: f"locate {target}" if target else f"search_code {params}",
    },
    "git": {
        "keywords": ["提交", "commit", "推送", "push", "记录", "log", "git", "版本"],
        "template": "git {params}",
        "get_cmd": lambda target, params: _resolve_git_cmd(target, params),
    },
    "report": {
        "keywords": ["日报", "今天干了啥", "汇总", "report", "报告", "周报"],
        "template": "report",
        "get_cmd": lambda target, params: "report" + (" 周" if "周" in params else ""),
    },
    "test": {
        "keywords": ["测试", "test", "跑测试", "跑一下测试"],
        "template": "test {target}",
        "get_cmd": lambda target, params: f"test {target}" if target else "test",
    },
    "doc": {
        "keywords": ["文档", "doc", "怎么用", "用法", "API", "库"],
        "template": "doc {target} {params}",
        "get_cmd": lambda target, params: _resolve_doc_cmd(target, params),
    },
    "manage": {
        "keywords": ["开关", "关闭", "打开", "off", "on", "permit", "forbid", "允许", "禁止"],
        "template": "{params}",
        "get_cmd": lambda target, params: _resolve_manage_cmd(target, params),
    },
    "validate": {
        "keywords": ["验证", "validate", "编译", "通过没"],
        "template": "validate {target}",
        "get_cmd": lambda target, params: f"validate {target}" if target else "validate",
    },
    "diff": {
        "keywords": ["diff", "变更", "改了什么", "区别"],
        "template": "diff",
        "get_cmd": lambda target, params: "diff",
    },
    "context": {
        "keywords": ["上下文", "context", "会话", "session"],
        "template": "context",
        "get_cmd": lambda target, params: "context",
    },
}


def _resolve_doc_cmd(target: str, params: str) -> str:
    """从自然语言中提取要查的库名"""
    # 常见库名模式
    lib_match = re.search(r'([a-zA-Z_][a-zA-Z0-9_]*)', params)
    lib = lib_match.group(1) if lib_match else params
    # 去掉常见的无用词
    for noise in ["怎么用", "用法", "文档", "doc", "查", "一下", "库"]:
        lib = lib.replace(noise, "")
    lib = lib.strip()
    return f"doc {lib}" if lib else "doc"


def _resolve_git_cmd(target: str, params: str) -> str:
    if any(k in params for k in ["提交", "commit"]):
        return f"git commit {target}" if target else "git status"
    if any(k in params for k in ["记录", "log", "历史"]):
        return "git log"
    if any(k in params for k in ["diff", "变更", "区别"]):
        return "git diff"
    return "git status"


def _resolve_manage_cmd(target: str, params: str) -> str:
    full = target + " " + params
    if "关闭" in full or "off" in full.lower():
        if "suggest" in full.lower():
            return "suggest off"
        return "suggest off"
    if "允许" in full or "permit" in full.lower() or "打开" in full:
        if "L2" in full or "l2" in full.lower():
            return "permit L2"
        if "L3" in full or "l3" in full.lower():
            return "permit L3"
        return "permit L2"
    if "禁止" in full or "forbid" in full.lower():
        if "写" in full or "L1" in full:
            return "forbid L1"
        return "forbid L2"
    return params


def _extract_target(text: str, intent_keywords: list) -> tuple:
    """从文本中提取目标文件和额外参数"""
    # 常见的文件名模式
    file_match = re.search(r'([A-Za-z0-9_./\\-]+\.py)', text)
    target = file_match.group(1).replace("\\", "/") if file_match else ""
    # 保留完整原始文本作为params，不做关键词剥离
    # resolver自行判断语义
    params = text.strip()
    return target, params


def parse_nl_command(text: str) -> Optional[str]:
    """
    自然语言→固定命令解析。
    返回解析后的命令字符串，或 None 表示无法匹配。
    """
    text = text.strip()
    if not text:
        return None

    # 已经是固定命令→直接放行
    known_commands = [
        "fix ", "修复 ", "review ", "locate ", "depend ", "search_code ",
        "git ", "diff", "changed", "status", "tasks", "done",
        "test ", "测试 ", "validate ", "验证 ", "sandbox",
        "context", "new", "doc ", "文档 ", "audit", "audit ",
        "log ", "日志 ", "knowledge", "知识库", "report", "报告",
        "suggest", "pref", "偏好", "index ", "索引 ", "refs ", "引用 ",
        "permit ", "forbid ", "允许 ", "禁止 ", "patch",
    ]
    low = text.lower()
    for cmd in known_commands:
        if low.startswith(cmd.lower()):
            return text  # 已是固定命令，直接返回

    # 自然语言意图匹配（长关键词权重更高）
    best_intent = None
    best_score = 0
    for intent_name, intent_def in _INTENT_MAP.items():
        score = sum(len(kw) for kw in intent_def["keywords"] if kw in low)
        if score > best_score:
            best_score = score
            best_intent = intent_name

    if best_score == 0:
        return None  # 完全无法匹配

    intent_def = _INTENT_MAP[best_intent]
    target, params = _extract_target(text, intent_def["keywords"])
    cmd = intent_def["get_cmd"](target, params)
    log(f"[NL] {text[:40]} → intent={best_intent} → /d {cmd}")
    return cmd


def _do_launch(app: str) -> str:
    app_map = {"记事本":"notepad.exe","notepad":"notepad.exe","计算器":"calc.exe","calc":"calc.exe",
               "微信":"weixin://","wechat":"weixin://","cmd":"cmd.exe","终端":"cmd.exe",
               "资源管理器":"explorer.exe","explorer":"explorer.exe"}
    title_map = {"notepad":"记事本","notepad.exe":"记事本","calc":"计算器","calc.exe":"计算器"}
    cmd = app_map.get(app.lower())
    if cmd is None:
        return f"不支持的应用: {app} (可用: {', '.join(app_map.keys())})"
    try:
        if "://" in str(cmd):
            subprocess.Popen(["cmd", "/c", "start", cmd], shell=False)
        else:
            subprocess.Popen([cmd], shell=False)
        target = title_map.get(app.lower(), app)
        from .tools import _ensure_window_focused
        for _ in range(10):
            time.sleep(0.3)
            if _ensure_window_focused(target):
                return f"已启动 {app}"
        return f"已启动 {app}"
    except Exception as e:
        return f"启动失败 {app}: {e}"
