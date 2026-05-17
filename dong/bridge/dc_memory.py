"""
DC模式独立记忆库 —— 主人直连Claude时的持久化记忆
Crash-safe: 每次写入先写临时文件再替换
"""
import json, os, time, threading

_DONG_DIR = os.path.dirname(os.path.dirname(__file__))
MEMORY_FILE = os.path.join(_DONG_DIR, "claude_dc_memory.json")
LOG_FILE = os.path.join(_DONG_DIR, "claude_dc_log.jsonl")
_lock = threading.Lock()

MAX_SESSIONS = 20
MAX_LOG_LINES = 500
MAX_CONTEXT_CHARS = 3000


def _load():
    if not os.path.exists(MEMORY_FILE):
        return _default()
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return _default()


def _default():
    return {
        "version": "1.0",
        "created": time.time(),
        "sessions": [],
        "current": {"task": "", "progress": "", "started": 0},
        "context": "",
        "known_issues": [],
        "fixes_applied": [],
        "backlog": [],
    }


def _save(data: dict):
    data["last_updated"] = time.time()
    tmp = MEMORY_FILE + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, MEMORY_FILE)


def _trim(data: dict):
    """裁剪旧数据"""
    if len(data["sessions"]) > MAX_SESSIONS:
        data["sessions"] = data["sessions"][-MAX_SESSIONS:]
    if len(data["known_issues"]) > 50:
        data["known_issues"] = data["known_issues"][-50:]
    if len(data["fixes_applied"]) > 50:
        data["fixes_applied"] = data["fixes_applied"][-50:]
    if len(data["backlog"]) > 30:
        data["backlog"] = data["backlog"][-30:]
    return data


# ── 日志 ──

def log(level: str, msg: str):
    """追加日志行"""
    entry = {"ts": time.time(), "lvl": level, "msg": msg[:500]}
    try:
        with _lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _rotate_log()
    except:
        pass


def _rotate_log():
    if not os.path.exists(LOG_FILE):
        return
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > MAX_LOG_LINES:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-MAX_LOG_LINES // 2:])
    except:
        pass


def read_log(tail: int = 30) -> str:
    if not os.path.exists(LOG_FILE):
        return "(空)"
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-tail:])
    except:
        return "(读取失败)"


# ── 会话管理 ──

def session_start():
    """新会话开始"""
    data = _trim(_load())
    data["sessions"].append({
        "start": time.time(),
        "end": None,
        "crashed": False,
        "summary": "",
    })
    _save(data)
    log("SESSION", "start")
    return data


def session_end(summary: str = ""):
    """正常结束会话"""
    data = _load()
    if data["sessions"]:
        s = data["sessions"][-1]
        s["end"] = time.time()
        s["summary"] = summary[:200]
    _save(data)
    log("SESSION", f"end: {summary[:100]}")


def session_crashed():
    """检测上次会话是否崩溃"""
    data = _load()
    if data["sessions"] and data["sessions"][-1]["end"] is None:
        return True
    return False


# ── 上下文 ──

def save_context(summary: str):
    """保存会话上下文摘要"""
    data = _load()
    data["context"] = summary[:MAX_CONTEXT_CHARS]
    _save(data)


def load_context() -> str:
    return _load().get("context", "")


# ── 当前任务 ──

def set_current_task(task: str, progress: str = ""):
    data = _load()
    data["current"] = {"task": task[:500], "progress": progress[:500], "started": time.time()}
    _save(data)
    log("TASK", f"set: {task[:100]}")


def update_progress(progress: str):
    data = _load()
    data["current"]["progress"] = progress[:500]
    _save(data)


def clear_current_task():
    data = _load()
    old = data["current"].get("task", "")
    data["current"] = {"task": "", "progress": "", "started": 0}
    _save(data)
    if old:
        log("TASK", f"done: {old[:100]}")


# ── 问题追踪 ──

def add_known_issue(issue: str):
    data = _trim(_load())
    data["known_issues"].append({"ts": time.time(), "issue": issue[:300], "fixed": False})
    _save(data)
    log("ISSUE", issue[:200])


def mark_issue_fixed(issue_pattern: str):
    data = _load()
    for i in data["known_issues"]:
        if issue_pattern in i["issue"]:
            i["fixed"] = True
    _save(data)


def add_fix(description: str):
    data = _trim(_load())
    data["fixes_applied"].append({"ts": time.time(), "fix": description[:300]})
    _save(data)
    log("FIX", description[:200])


# ── 积压任务 ──

def add_backlog(item: str):
    data = _trim(_load())
    data["backlog"].append({"ts": time.time(), "item": item[:300], "done": False})
    _save(data)


def mark_backlog_done(item_pattern: str):
    data = _load()
    for i in data["backlog"]:
        if item_pattern in i["item"]:
            i["done"] = True
    _save(data)


# ── 状态摘要 ──

def status_summary() -> str:
    """生成可读状态摘要"""
    data = _load()
    lines = []
    lines.append("═══ DC记忆库状态 ═══")
    sessions = data["sessions"]
    if sessions:
        last = sessions[-1]
        crashed = last["end"] is None
        lines.append(f"上次会话: {time.strftime('%m-%d %H:%M', time.localtime(last['start']))}")
        lines.append(f"状态: {'[WARN] 异常结束(崩溃)' if crashed else '[OK] 正常结束'}")
    cur = data["current"]
    if cur["task"]:
        lines.append(f"当前任务: {cur['task']}")
        lines.append(f"进度: {cur['progress']}")
    issues = [i for i in data["known_issues"] if not i["fixed"]]
    if issues:
        lines.append(f"未修复问题: {len(issues)}个")
        for i in issues[-5:]:
            lines.append(f"  - {i['issue'][:80]}")
    backlog = [b for b in data["backlog"] if not b["done"]]
    if backlog:
        lines.append(f"积压任务: {len(backlog)}个")
        for b in backlog[-5:]:
            lines.append(f"  - {b['item'][:80]}")
    ctx = data.get("context", "")
    if ctx:
        lines.append(f"上下文: {ctx[:200]}...")
    lines.append("══════════════════════")
    return "\n".join(lines)
