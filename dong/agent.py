"""
冬 · 智能体引擎 — 对标 Claude Code
合成版：DeepSeek骨架(异步+记忆API) + GLM工具(bash安全+search_memory+list_dir) + 自修复

- 异步 aiohttp，不阻塞冬的消息循环
- 记忆：用 memory.py API + claude_dc_memory.py API，不裸写JSON
- 7工具：read_file / write_file / edit_file / search_code / run_bash / search_memory / list_directory
- 安全：json.loads解析 / _safe_path沙箱 / bash白名单+黑名单+管道校验 / shlex.split
- 技能：解析 SKILL.md 触发条件自动匹配
- 配置：环境变量优先，fallback 到冬的分析模型
"""
import os, re, json, asyncio, subprocess, shlex, threading, time, traceback, tempfile, py_compile, ast, importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any, Callable, Awaitable, Tuple

import aiohttp

from .log import log
from .config import BASE_DIR, MASTER_UID

# ════════════════════════════════════════════
# 配置（环境变量 → fallback 冬的分析模型）
# ════════════════════════════════════════════

AGENT_API_KEY = os.environ.get("DONG_AGENT_API_KEY", "")
AGENT_API_BASE = os.environ.get("DONG_AGENT_API_BASE", "https://api.openai.com/v1")
AGENT_MODEL = os.environ.get("DONG_AGENT_MODEL", "gpt-4o")
AGENT_MAX_TOKENS = int(os.environ.get("DONG_AGENT_MAX_TOKENS", "4096"))
AGENT_TEMPERATURE = float(os.environ.get("DONG_AGENT_TEMPERATURE", "0.3"))
MAX_TOOL_ROUNDS = int(os.environ.get("DONG_AGENT_MAX_ROUNDS", "20"))
CONTEXT_COMPRESS_AFTER = int(os.environ.get("DONG_AGENT_COMPRESS_AFTER", "12"))
READ_PAGE_LINES = int(os.environ.get("DONG_AGENT_READ_PAGE_LINES", "500"))

PROJECT_DIR = os.path.abspath(BASE_DIR)
PROJECT_INDEX_FILE = os.path.join(PROJECT_DIR, "agent_project_index.json")
TASK_HISTORY_FILE = os.path.join(PROJECT_DIR, "task_history.jsonl")
TASK_STATE_FILE = os.path.join(PROJECT_DIR, "agent_task_state.json")
LEARNED_FIXES_FILE = os.path.join(PROJECT_DIR, "agent_learned_fixes.json")
TEAM_STATE_FILE = os.path.join(PROJECT_DIR, "agent_team_state.json")
CODE_SNIPPETS_FILE = os.path.join(PROJECT_DIR, "agent_code_snippets.json")
_CONTEXT_STATE: Dict[int, Dict[str, Any]] = {}
_RECENT_READ_FILES: List[str] = []
_PROJECT_INDEX: Optional[Dict[str, Any]] = None
_PROJECT_INDEX_LOCK = threading.RLock()
_TEAM_STATE: Dict[str, Any] = {}

# ════════════════════════════════════════════
# 变更追踪（/d diff /d changed）
# ════════════════════════════════════════════
_CHANGE_LOG: Dict[str, Any] = {}  # 最近一次 fix 的完整变更记录
# 结构: {"issue": str, "targets": list, "snapshots": {rel: old_content},
#        "changed": [rel], "rolled_back": [rel], "rollback_reasons": {},
#        "validation": {...}, "repair_log": [...], "time": str}

# ════════════════════════════════════════════
# 主动建议引擎 (第3项)
# ════════════════════════════════════════════
_SUGGEST_ENABLED: bool = True
_SUGGEST_LOW_AUTO_FIX: bool = True  # 低严重度自动修复
_SUGGEST_REPORT_TIME: str = "21:00"  # 默认日报推送时间
_SUGGEST_FIXES_TODAY: List[str] = []  # 今日自动修复记录
_SUGGEST_PENDING: List[str] = []  # 待确认项（中严重度）
_SUGGEST_HIGH_ALERTS: List[str] = []  # 高严重度告警


def _health_alert(level: str, module: str, message: str):
    """统一健康告警入口 — 与建议引擎共用通知管道"""
    if level == "low":
        # 低优先级：只记录，不通知
        _SUGGEST_FIXES_TODAY.append(f"[{module}] {message}")
    elif level == "medium":
        # 中优先级：攒日报
        _SUGGEST_PENDING.append(f"[{module}] {message}")
    elif level == "high":
        # 高优先级：立刻推送（QQ事件 + 微信告警）
        _SUGGEST_HIGH_ALERTS.append(f"[{module}] {message}")
        try:
            from .bridge.pet_agent_bridge import push_event
            push_event("alert", f"[{module}] {message}", priority="high")
        except Exception:
            pass
        # 微信后台推送，不阻塞
        threading.Thread(target=_push_wechat_alert, args=(message,), daemon=True).start()


# ════════════════════════════════════════════
# 文档缓存 (队列B)
# ════════════════════════════════════════════
_DOC_CACHE: Dict[str, str] = {}
_DOC_CACHE_FILE = os.path.join(PROJECT_DIR, "agent_doc_cache.json")
_AUDIT_FILE = os.path.join(PROJECT_DIR, "agent_audit.jsonl")
_SANDBOX_DIR = os.path.join(PROJECT_DIR, "sandbox")


def _sandbox_path(rel: str) -> str:
    """获取沙箱路径"""
    return os.path.join(_SANDBOX_DIR, rel)


def _sandbox_copy(rel: str) -> bool:
    """复制文件到沙箱"""
    src = _safe_path(rel)
    if not os.path.isfile(src):
        return False
    dst = _sandbox_path(rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    import shutil
    shutil.copy2(src, dst)
    return True


def _sandbox_verify(rel: str) -> Tuple[bool, str]:
    """在沙箱中验证文件"""
    sandbox_file = _sandbox_path(rel)
    if not os.path.isfile(sandbox_file):
        return False, "沙箱文件不存在"
    return _verify_python_file(sandbox_file)


def _sandbox_promote(rel: str) -> bool:
    """沙箱验证通过→同步到真实文件"""
    src = _sandbox_path(rel)
    dst = _safe_path(rel)
    if not os.path.isfile(src):
        return False
    import shutil
    shutil.copy2(src, dst)
    return True


def _sandbox_clean(rel: str = ""):
    """清理沙箱"""
    if rel:
        p = _sandbox_path(rel)
        if os.path.isfile(p):
            os.remove(p)
    else:
        import shutil
        if os.path.isdir(_SANDBOX_DIR):
            shutil.rmtree(_SANDBOX_DIR, ignore_errors=True)


def _sandbox_status() -> str:
    """沙箱状态"""
    if not os.path.isdir(_SANDBOX_DIR):
        return "沙箱为空。"
    files = []
    for root, dirs, names in os.walk(_SANDBOX_DIR):
        for name in names:
            rel = os.path.relpath(os.path.join(root, name), _SANDBOX_DIR)
            files.append(rel)
    if not files:
        return "沙箱为空。"
    return f"📦 沙箱 ({len(files)}个文件):\n  " + "\n  ".join(files[:15])


def _audit_tool(name: str, args: Dict, result: str, duration_ms: float):
    """记录工具调用审计日志"""
    try:
        entry = json.dumps({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tool": name,
            "args": {k: str(v)[:100] for k, v in args.items()},
            "result": str(result)[:200],
            "duration_ms": int(duration_ms),
            "level": _SECURITY_LEVELS.get(name, 0),
        }, ensure_ascii=False)
        with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass


def _get_audit_summary(date: str = "") -> str:
    """读取审计日志摘要"""
    if not os.path.exists(_AUDIT_FILE):
        return "无审计记录。"
    today = date or datetime.now().strftime("%Y-%m-%d")
    count = 0
    l2_count = 0
    tools = {}
    try:
        with open(_AUDIT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if today in line:
                    count += 1
                    try:
                        e = json.loads(line.strip())
                        name = e.get("tool", "?")
                        tools[name] = tools.get(name, 0) + 1
                        if e.get("level", 0) >= 2:
                            l2_count += 1
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        return f"[错误] 读取审计日志失败: {e}"
    lines = [f"📊 审计 {today}: {count} 次调用"]
    if tools:
        lines.append("  工具分布: " + ", ".join(
            f"{k}×{v}" for k, v in sorted(tools.items(), key=lambda x: -x[1])[:8]
        ))
    if l2_count:
        lines.append(f"  ⚠ 危险操作(L2+): {l2_count} 次")
    return "\n".join(lines)


def _load_doc_cache():
    global _DOC_CACHE
    try:
        if os.path.exists(_DOC_CACHE_FILE):
            with open(_DOC_CACHE_FILE, "r", encoding="utf-8") as f:
                _DOC_CACHE = json.load(f)
    except Exception:
        pass


def _save_doc_cache():
    try:
        tmp = _DOC_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_DOC_CACHE, f, ensure_ascii=False)
        os.replace(tmp, _DOC_CACHE_FILE)
    except Exception as e:
        log(f"[agent] 文档缓存保存失败: {e}")


def _search_doc(library: str, func: str = "") -> str:
    """搜索官方文档"""
    key = f"{library}:{func}" if func else library
    if key in _DOC_CACHE:
        return f"📚 缓存: {_DOC_CACHE[key][:1500]}"

    query = f"{library} documentation"
    if func:
        query += f" {func} function signature example"

    # 用 web_search 工具的能力
    try:
        from .tools import _tool_search_online
        result = _tool_search_online(query)
    except Exception as e:
        return f"[错误] 搜索失败: {e}"

    if result and len(result) > 20:
        _DOC_CACHE[key] = result[:2000]
        _save_doc_cache()
        return f"📚 {library}" + (f".{func}" if func else "") + f":\n{result[:1500]}"
    return f"未找到 {library}" + (f".{func}" if func else "") + " 的文档。"


def _run_suggest_scan() -> str:
    """主动扫描项目代码问题"""
    py_files = _project_py_files()
    findings: List[Dict[str, Any]] = []
    for rel in py_files[:30]:  # 限制扫描30个文件
        try:
            result = _execute_review_code(rel, "suggest")
            if "[错误]" not in result and "未发现明确问题" not in result:
                findings.append({"file": rel, "result": result[:300]})
        except Exception:
            pass
    if not findings:
        return "✅ 扫描完成：未发现新问题。"
    lines = [f"🔍 主动扫描 ({len(findings)}个文件有问题):"]
    for f in findings[:10]:
        lines.append(f"\n📁 {f['file']}\n{f['result']}")
    return "\n".join(lines)


def _format_daily_report() -> str:
    """生成日报草稿"""
    auto_fixes = len(_SUGGEST_FIXES_TODAY)
    pending = len(_SUGGEST_PENDING)
    lines = [
        f"📋 今日报告草稿 ({datetime.now().strftime('%H:%M')})",
        f"  自动修复: {auto_fixes} 项",
        f"  待确认: {pending} 项",
    ]
    if _SUGGEST_FIXES_TODAY:
        lines.append("  已修复:")
        for f in _SUGGEST_FIXES_TODAY[-5:]:
            lines.append(f"    - {f}")
    if _SUGGEST_PENDING:
        lines.append("  待确认:")
        for p in _SUGGEST_PENDING[-5:]:
            lines.append(f"    - {p}")
    return "\n".join(lines)


def _format_weekly_report() -> str:
    """生成周报草稿"""
    return (
        f"📊 周报草稿 ({datetime.now().strftime('%m-%d')})\n"
        f"  本周自主修复: {len(_SUGGEST_FIXES_TODAY)} 项\n"
        f"  待确认: {len(_SUGGEST_PENDING)} 项\n"
        f"  Agent升级: {_PATCH_COUNTER} 次patch"
    )


def _push_wechat_daily_report():
    """推送日报到微信 — 后台线程，不阻塞"""
    report = _format_daily_report()
    wechat_msg = f"冬日报 | 自动修复{len(_SUGGEST_FIXES_TODAY)}项 | 待确认{len(_SUGGEST_PENDING)}项 | 详情 /d report"
    try:
        from .agent_loop import _do_launch, _do_type
        import time as _t
        _do_launch("微信")
        _t.sleep(2)
        _do_type(wechat_msg, "微信")
        _t.sleep(0.3)
        # 发送
        import pyautogui as _pg
        _pg.press("enter")
        log(f"[agent] 微信日报已推送: {wechat_msg[:50]}")
        # 清空今日计数
        _SUGGEST_FIXES_TODAY.clear()
        _SUGGEST_PENDING.clear()
    except Exception as e:
        log(f"[agent] 微信日报推送失败: {e}")


def _push_wechat_alert(message: str):
    """推送高优先级告警到微信"""
    try:
        from .agent_loop import _do_launch, _do_type
        import time as _t, pyautogui as _pg
        _do_launch("微信")
        _t.sleep(2)
        _do_type(f"⚠ 冬告警: {message[:200]}", "微信")
        _t.sleep(0.3)
        _pg.press("enter")
        log(f"[agent] 微信告警已推送: {message[:50]}")
    except Exception as e:
        log(f"[agent] 微信告警推送失败: {e}")


# ════════════════════════════════════════════
# 安全权限层
# ════════════════════════════════════════════
_SECURITY_LEVELS = {
    # L0 只读 — 始终允许
    "read_file": 0, "search_code": 0, "list_directory": 0,
    "run_bash": 0,  # run_bash已有白名单限制
    "search_memory": 0, "locate_symbol": 0,
    "file_dependencies": 0, "review_code": 0,
    "search_online": 0, "browser_control": 0,  # browser只读操作
    # L1 写文件 — 需 /d ok 确认（当前默认允许）
    "write_file": 1, "edit_file": 1,
    # L2 执行 — 需 /d permit L2（60分钟窗口）
    "modify_self": 2, "computer_control": 2,
    "record_demo": 2, "learn_from_demo": 2,
    "learn_from_video": 2, "install_skill": 2,
    "wechat_send": 2,
    # L3 系统 — 拒绝，除非主人说"允许L3"
    "_pip_install": 3, "_git_push": 3, "_git_reset": 3, "_delete_file": 3,
}
_SECURITY_LEVEL_OVERRIDE: Dict[str, int] = {}  # 工具名→临时放宽的等级
_SECURITY_BLOCKED_READS = frozenset({".env", "*.key", "*_secret*"})  # L0黑名单文件
_L2_PERMIT_UNTIL: float = 0.0  # L2临时开放截止时间戳
_L3_PERMIT: bool = False  # L3全局允许标志

# ════════════════════════════════════════════
# 任务状态机
# ════════════════════════════════════════════
_TASK_STATE: Dict[str, Any] = {}  # 当前任务状态，持久化到 TASK_STATE_FILE
_TASK_LIST: List[Dict[str, Any]] = []  # 所有任务列表（内存），归档到 TASK_HISTORY_FILE

# ════════════════════════════════════════════
# 项目知识库
# ════════════════════════════════════════════
_KNOWLEDGE_CACHE: Optional[str] = None  # PROJECT_KNOWLEDGE.md 内容缓存
_KNOWLEDGE_FILE = os.path.join(PROJECT_DIR, "PROJECT_KNOWLEDGE.md")

# ════════════════════════════════════════════
# Patch 引擎
# ════════════════════════════════════════════
_PATCH_LOG: List[Dict[str, Any]] = []  # 本轮所有patch记录
_PATCH_COUNTER: int = 0
PATCH_HISTORY_FILE = os.path.join(PROJECT_DIR, "patch_history.jsonl")
AGENT_SESSIONS_FILE = os.path.join(PROJECT_DIR, "agent_sessions.json")
AGENT_MEMORY_FILE = os.path.join(PROJECT_DIR, "agent_project_memory.json")
AGENT_PREFS_FILE = os.path.join(PROJECT_DIR, "agent_user_prefs.json")

# ════════════════════════════════════════════
# 持久会话 (第0项)
# ════════════════════════════════════════════
_SESSION_MESSAGES: Dict[int, List[Dict[str, Any]]] = {}  # uid → messages数组
_SESSION_MAX_TOKENS = 1_000_000  # 1M token 软上限

def _load_sessions():
    """从JSON恢复所有会话"""
    global _SESSION_MESSAGES
    try:
        if os.path.exists(AGENT_SESSIONS_FILE):
            with open(AGENT_SESSIONS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            _SESSION_MESSAGES = {int(k): v for k, v in raw.items()}
    except Exception as e:
        log(f"[agent] 会话加载失败: {e}")


def _save_sessions():
    """持久化所有会话到JSON"""
    try:
        tmp = AGENT_SESSIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in _SESSION_MESSAGES.items()}, f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, AGENT_SESSIONS_FILE)
    except Exception as e:
        log(f"[agent] 会话保存失败: {e}")


def _get_session(uid: int, system_prompt: str, user_message: str) -> List[Dict[str, Any]]:
    """获取或创建持久会话的messages数组"""
    msgs = _SESSION_MESSAGES.get(uid, [])
    if not msgs:
        msgs = [{"role": "system", "content": system_prompt}]
        _SESSION_MESSAGES[uid] = msgs
    # 更新 system prompt（可能因知识库/记忆变化而更新）
    msgs[0]["content"] = system_prompt
    # 追加用户消息
    msgs.append({"role": "user", "content": user_message})
    # 压缩检查
    _session_compact(uid, msgs)
    return msgs


def _session_compact(uid: int, msgs: List[Dict[str, Any]]):
    """超1M token时压缩：保留system + 最近10轮 + 摘要"""
    total_text = "\n".join(str(m.get("content") or "") for m in msgs)
    if _estimate_tokens(total_text) < _SESSION_MAX_TOKENS:
        return
    system = [m for m in msgs if m.get("role") == "system"][:1]
    others = [m for m in msgs if m.get("role") != "system"]
    if len(others) <= 20:
        return  # 太少不压
    old = others[:-20]  # 保留最近10对
    recent = others[-20:]
    digest = "\n".join(
        f"[{m.get('role')}]: {str(m.get('content') or '')[:200]}"
        for m in old[-40:]
    )
    summary = {"role": "system", "content": f"【压缩历史摘要】较早期对话摘要：\n{digest}"}
    compacted = system + [summary] + recent
    _SESSION_MESSAGES[uid] = compacted
    log(f"[agent] 会话压缩 uid={uid}: {len(msgs)}→{len(compacted)}条消息")


def _clear_session(uid: int):
    """清空会话"""
    _SESSION_MESSAGES.pop(uid, None)
    _save_sessions()


def _session_info(uid: int) -> str:
    """返回会话统计信息"""
    msgs = _SESSION_MESSAGES.get(uid, [])
    if not msgs:
        return f"uid={uid}: 无活跃会话。"
    non_sys = [m for m in msgs if m.get("role") != "system"]
    text = "\n".join(str(m.get("content") or "") for m in msgs)
    # 找最近主题
    recent_topics = []
    for m in non_sys[-4:]:
        c = str(m.get("content") or "")[:60]
        if c.strip():
            recent_topics.append(f"[{m['role']}] {c}")
    return (
        f"📊 会话 {uid}:\n"
        f"- 消息数: {len(msgs)} (系统1 + 对话{len(non_sys)})\n"
        f"- 估算token: {_estimate_tokens(text):,}\n"
        f"- 最近主题:\n  " + "\n  ".join(recent_topics or ["(无)"])
    )


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 3)

def _message_digest(message: Dict[str, Any]) -> str:
    role = message.get("role", "?")
    content = message.get("content") or ""
    if not content and message.get("tool_calls"):
        names = []
        for tc in message.get("tool_calls", []):
            func = tc.get("function", {}) if isinstance(tc, dict) else {}
            names.append(func.get("name", "tool"))
        content = "tool_calls=" + ",".join(names)
    content = re.sub(r"\s+", " ", str(content)).strip()
    if len(content) > 220:
        content = content[:220] + "..."
    return f"{role}: {content}"

def _remember_read_file(path: str):
    norm = path.replace("\\", "/")
    if norm in _RECENT_READ_FILES:
        _RECENT_READ_FILES.remove(norm)
    _RECENT_READ_FILES.append(norm)
    del _RECENT_READ_FILES[:-20]

def _context_snapshot(uid: int, messages: Optional[List[Dict[str, Any]]] = None) -> str:
    state = _CONTEXT_STATE.get(uid, {})
    if messages is not None:
        text = "\n".join(str(m.get("content") or "") for m in messages)
        state = {
            **state,
            "rounds": max(0, len([m for m in messages if m.get("role") != "system"]) // 2),
            "messages": len(messages),
            "tokens": _estimate_tokens(text),
        }
        _CONTEXT_STATE[uid] = state
    files = state.get("files") or list(_RECENT_READ_FILES[-8:])
    return (
        "上下文窗口:\n"
        f"- 轮数: {state.get('rounds', 0)}\n"
        f"- 消息数: {state.get('messages', 0)}\n"
        f"- 已压缩早期消息: {state.get('compressed_messages', 0)}\n"
        f"- 估算token: {state.get('tokens', 0)}\n"
        f"- 最近读取文件: {', '.join(files) if files else '无'}"
    )

def _maybe_compact_messages(uid: int, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    non_system = [m for m in messages if m.get("role") != "system"]
    if len(non_system) <= CONTEXT_COMPRESS_AFTER:
        _context_snapshot(uid, messages)
        return messages

    system_messages = [m for m in messages if m.get("role") == "system"]
    keep_count = 10  # 最近5轮原文，约等于 user/assistant 或 assistant/tool 成对消息。
    old = non_system[:-keep_count]
    recent = non_system[-keep_count:]
    digest = "\n".join(_message_digest(m) for m in old[-40:])
    summary = {
        "role": "system",
        "content": (
            "【压缩上下文摘要】以下是较早对话和工具结果的确定性摘要；最近5轮仍保留原文。\n"
            f"已压缩消息数: {len(old)}\n{digest}"
        )
    }
    compacted = system_messages[:1] + [summary] + recent
    text = "\n".join(str(m.get("content") or "") for m in compacted)
    _CONTEXT_STATE[uid] = {
        "rounds": max(0, len(non_system) // 2),
        "messages": len(compacted),
        "compressed_messages": len(old),
        "tokens": _estimate_tokens(text),
        "files": list(_RECENT_READ_FILES[-8:]),
    }
    return compacted

def _get_agent_config() -> Tuple[str, str, str]:
    if AGENT_API_KEY:
        api_key, api_base, model = AGENT_API_KEY, AGENT_API_BASE, AGENT_MODEL
    else:
        try:
            from .config import _get_cfg as _cfg
            c = _cfg("chat")  # 用主力聊天模型，比analysis模型强
            api_key, api_base, model = c.api_key, c.api_base, c.model
        except Exception:
            return "", "", ""

    # ★ 休眠切API：入睡时用便宜模型
    try:
        from .status import _status
        if isinstance(_status, dict) and _status.get("sleeping"):
            # 用 analysis 模型（更便宜），保留 api_key/api_base
            try:
                from .config import _get_cfg as _cfg2
                cheap = _cfg2("analysis")
                model = cheap.model
            except Exception:
                model = os.environ.get("DONG_SLEEP_MODEL", model)
    except Exception:
        pass

    return api_key, api_base, model

# ════════════════════════════════════════════
# 安全：路径沙箱
# ════════════════════════════════════════════

def _safe_path(user_path: str) -> str:
    if user_path.replace("\\", "/").startswith("dong/") and os.path.basename(PROJECT_DIR).lower() == "dong":
        user_path = user_path.replace("\\", "/", 1)[5:]
    path = os.path.normpath(user_path)
    if not os.path.isabs(path):
        path = os.path.join(PROJECT_DIR, path)
    path = os.path.abspath(os.path.realpath(path))
    path = os.path.normpath(path)
    root = os.path.normpath(os.path.abspath(os.path.realpath(PROJECT_DIR)))
    # Windows 大小写不敏感比较
    if os.name == 'nt':
        if not path.lower().startswith(root.lower() + os.sep.lower()) and path.lower() != root.lower():
            raise ValueError(f"路径越界: {user_path}")
    else:
        if not path.startswith(root + os.sep) and path != root:
            raise ValueError(f"路径越界: {user_path}")
    return path

def _project_rel(path: str) -> str:
    return os.path.relpath(path, PROJECT_DIR).replace("\\", "/")

def _module_name_from_rel(rel: str) -> str:
    mod = rel[:-3] if rel.endswith(".py") else rel
    mod = mod.replace("/", ".").replace("\\", ".")
    return mod[:-9] if mod.endswith(".__init__") else mod

def _normalize_import(module: str, level: int, rel: str) -> str:
    if level <= 0:
        return module or ""
    package = _module_name_from_rel(rel).split(".")[:-1]
    base = package[:max(0, len(package) - level + 1)]
    if module:
        base.append(module)
    return ".".join(p for p in base if p)

def _ast_call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _ast_call_name(func.value)
        return f"{base}.{func.attr}" if base else func.attr
    if isinstance(func, ast.Call):
        return _ast_call_name(func.func)
    return ""


def _ast_names(node: ast.AST) -> List[str]:
    return sorted({n.id for n in ast.walk(node) if isinstance(n, ast.Name)})


def _detect_function_side_effects(node: ast.AST) -> List[Dict[str, Any]]:
    effects: List[Dict[str, Any]] = []
    io_calls = {"open", "Path.write_text", "Path.write_bytes", "write", "writelines",
                "os.remove", "os.unlink", "os.rename", "os.replace", "os.makedirs",
                "shutil.rmtree", "shutil.copy", "shutil.move"}
    net_calls = {"requests.get", "requests.post", "requests.request", "aiohttp.ClientSession",
                 "socket.socket", "urllib.request.urlopen"}
    proc_calls = {"subprocess.run", "subprocess.Popen", "os.system", "asyncio.create_subprocess_exec"}
    db_calls = {"sqlite3.connect", "pymysql.connect", "psycopg2.connect", "mysql.connector.connect"}
    for sub in ast.walk(node):
        line = getattr(sub, "lineno", getattr(node, "lineno", 0))
        if isinstance(sub, (ast.Global, ast.Nonlocal)):
            effects.append({"type": "global", "line": line, "detail": ", ".join(sub.names)})
        elif isinstance(sub, ast.Assign):
            for target in sub.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    effects.append({"type": "global", "line": line, "detail": f"assign {target.id}"})
                elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id not in {"self", "cls"}:
                    effects.append({"type": "global", "line": line, "detail": ast.unparse(target)[:80]})
        elif isinstance(sub, ast.Call):
            name = _ast_call_name(sub.func)
            low = name.lower()
            if name in io_calls or low.endswith(".write") or low.endswith(".read") or low.endswith(".save"):
                effects.append({"type": "disk_io", "line": line, "detail": name})
            elif name in net_calls or low.startswith(("requests.", "aiohttp.", "urllib.", "socket.")):
                effects.append({"type": "network", "line": line, "detail": name})
            elif name in proc_calls or low.startswith("subprocess."):
                effects.append({"type": "process", "line": line, "detail": name})
            elif name in db_calls or low.endswith((".execute", ".executemany", ".commit", ".rollback")):
                effects.append({"type": "database", "line": line, "detail": name})
    dedup: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    for eff in effects:
        dedup[(eff["type"], eff["line"], eff["detail"])] = eff
    return list(dedup.values())

def _index_python_file(path: str) -> Dict[str, Any]:
    rel = _project_rel(path)
    item = {"module": _module_name_from_rel(rel), "defs": [], "imports": [],
            "import_aliases": {}, "mtime": os.path.getmtime(path), "calls": {},
            "call_sites": {}, "data_flow": {}, "side_effects": {}, "inherits": []}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=path)
    except Exception as e:
        item["error"] = f"{type(e).__name__}: {e}"
        return item
    imports = set()
    # 第一遍：收集所有顶层定义名称
    top_names: set = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_names.add(node.name)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = []
            for a in node.args.args:
                ann = ast.unparse(a.annotation) if a.annotation else ""
                params.append({"name": a.arg, "type": ann})
            returns = ast.unparse(node.returns) if node.returns else ""
            def_info = {"name": node.name, "kind": "function", "line": node.lineno,
                        "params": params, "returns": returns}
            if hasattr(node, 'end_lineno') and node.end_lineno:
                def_info["end_line"] = node.end_lineno
            side_effects = _detect_function_side_effects(node)
            if side_effects:
                def_info["side_effects"] = side_effects
                item["side_effects"][node.name] = side_effects
            item["defs"].append(def_info)
            # 收集函数内调用的顶层函数
            call_targets = set()
            call_sites = []
            flow_edges = []
            param_names = {p["name"] for p in params}
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    call_name = _ast_call_name(sub.func)
                    if not call_name:
                        continue
                    short_name = call_name.rsplit(".", 1)[-1]
                    call_targets.add(short_name if short_name in top_names else call_name)
                    arg_names = sorted(set().union(*[_ast_names(a) for a in sub.args]) & param_names) if sub.args else []
                    call_sites.append({"name": call_name, "short": short_name,
                                       "line": getattr(sub, "lineno", 0),
                                       "args_from_params": arg_names})
                    for arg in arg_names:
                        flow_edges.append({"from": arg, "to": call_name,
                                           "line": getattr(sub, "lineno", 0)})
            if call_targets:
                item["calls"][node.name] = sorted(call_targets)
            if call_sites:
                item["call_sites"][node.name] = call_sites
            if flow_edges:
                item["data_flow"][node.name] = flow_edges
        elif isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases] if node.bases else []
            methods = []
            for body_node in node.body:
                if isinstance(body_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(body_node.name)
            item["defs"].append({"name": node.name, "kind": "class", "line": node.lineno,
                                "bases": bases, "methods": methods})
            if bases:
                item["inherits"].append({"class": node.name, "bases": bases})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
                item["import_aliases"][alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _normalize_import(node.module or "", node.level, rel)
            if base:
                imports.add(base)
            for alias in node.names:
                if alias.name != "*" and base:
                    imports.add(f"{base}.{alias.name}")
                    item["import_aliases"][alias.asname or alias.name] = f"{base}.{alias.name}"
    item["imports"] = sorted(imports)
    return item

def _build_project_index() -> Dict[str, Any]:
    files: Dict[str, Dict[str, Any]] = {}
    symbols: Dict[str, List[Dict[str, Any]]] = {}
    skip_dirs = {"__pycache__", ".git", ".venv", "venv", "node_modules", "data", "archive"}
    for root, dirs, names in os.walk(PROJECT_DIR):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            rel = _project_rel(path)
            item = _index_python_file(path)
            files[rel] = item
            for d in item.get("defs", []):
                symbols.setdefault(d["name"], []).append({
                    "file": rel,
                    "line": d.get("line", 0),
                    "kind": d.get("kind", "symbol"),
                    "module": item.get("module", ""),
                })
    call_graph: Dict[str, List[str]] = {}
    all_inherits: List[Dict[str, Any]] = []
    for item in files.values():
        for caller, callees in item.get("calls", {}).items():
            call_graph.setdefault(caller, []).extend(callees)
        all_inherits.extend(item.get("inherits", []))
    index = {"root": PROJECT_DIR, "built_at": time.time(), "files": files,
             "symbols": symbols, "call_graph": call_graph, "inherits": all_inherits}
    _rebuild_symbols_from_files(index)
    return index

def _save_project_index(index: Dict[str, Any]):
    with _PROJECT_INDEX_LOCK:
        tmp = PROJECT_INDEX_FILE + f".{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PROJECT_INDEX_FILE)

def _ensure_project_index(force: bool = False) -> Dict[str, Any]:
    global _PROJECT_INDEX
    if _PROJECT_INDEX is not None and not force:
        return _PROJECT_INDEX
    if not force and os.path.exists(PROJECT_INDEX_FILE):
        try:
            with open(PROJECT_INDEX_FILE, "r", encoding="utf-8") as f:
                _PROJECT_INDEX = json.load(f)
            # 增量更新：只重扫修改过的文件
            _incremental_refresh_index()
            return _PROJECT_INDEX
        except Exception as e:
            log(f"[agent] 项目索引读取失败，重新扫描: {e}")
    _PROJECT_INDEX = _build_project_index()
    try:
        _save_project_index(_PROJECT_INDEX)
    except Exception as e:
        log(f"[agent] 项目索引保存失败: {e}")
    return _PROJECT_INDEX


def _incremental_refresh_index():
    global _PROJECT_INDEX
    """增量更新：只重扫 mtime 有变化的文件"""
    index = _PROJECT_INDEX
    if not index:
        return
    files_index = index.get("files", {})
    changed = 0
    for rel, item in list(files_index.items()):
        path = _safe_path(rel)
        if not os.path.isfile(path):
            del files_index[rel]
            changed += 1
            continue
        try:
            current_mtime = os.path.getmtime(path)
        except OSError:
            continue
        if abs(current_mtime - item.get("mtime", 0)) > 1:
            new_item = _index_python_file(path)
            files_index[rel] = new_item
            changed += 1
    if changed:
        _rebuild_symbols_from_files(index)
        _save_project_index(index)
    _PROJECT_INDEX = index


def _rebuild_symbols_from_files(index: Dict[str, Any]):
    symbols: Dict[str, List[Dict[str, Any]]] = {}
    all_calls: Dict[str, List[str]] = {}
    reverse_calls: Dict[str, List[Dict[str, Any]]] = {}
    import_graph: Dict[str, List[str]] = {}
    reverse_imports: Dict[str, List[str]] = {}
    all_inherits: List[Dict[str, Any]] = []
    for rel, item in index.get("files", {}).items():
        module = item.get("module", "")
        import_graph[rel] = list(item.get("imports", []))
        for imp in item.get("imports", []):
            reverse_imports.setdefault(imp, []).append(rel)
        for d in item.get("defs", []):
            entry = {"file": rel, "line": d.get("line", 0), "kind": d.get("kind", "symbol"),
                     "module": item.get("module", ""),
                     "params": d.get("params", []), "returns": d.get("returns", ""),
                     "bases": d.get("bases", []), "methods": d.get("methods", []),
                     "side_effects": d.get("side_effects", [])}
            symbols.setdefault(d["name"], []).append(entry)
        for caller, callees in item.get("calls", {}).items():
            all_calls.setdefault(caller, []).extend(callees)
            for callee in callees:
                reverse_calls.setdefault(callee.rsplit(".", 1)[-1], []).append({
                    "function": caller, "file": rel, "module": module
                })
        all_inherits.extend(item.get("inherits", []))
    index["symbols"] = symbols
    index["call_graph"] = all_calls
    index["reverse_call_graph"] = reverse_calls
    index["import_graph"] = import_graph
    index["reverse_import_graph"] = reverse_imports
    index["inherits"] = all_inherits

def _update_project_index_for(path: str):
    try:
        index = _ensure_project_index()
        rel = _project_rel(path)
        if path.endswith(".py") and os.path.exists(path):
            item = _index_python_file(path)
            index.setdefault("files", {})[rel] = item
            _rebuild_symbols_from_files(index)
            _save_project_index(index)
    except Exception as e:
        log(f"[agent] 项目索引更新失败: {e}")

def _execute_locate(symbol: str) -> str:
    symbol = symbol.strip()
    if not symbol:
        return "用法: /d locate 函数名或类名"
    index = _ensure_project_index()
    matches = list(index.get("symbols", {}).get(symbol, []))
    if not matches:
        low = symbol.lower()
        for name, items in index.get("symbols", {}).items():
            if low in name.lower():
                matches.extend(items)
    if not matches:
        return f"未找到定义: {symbol}"
    lines = [f"定义位置: {symbol}"]
    for m in matches[:30]:
        lines.append(f"- {m.get('file')}:{m.get('line')} {m.get('kind')} {m.get('module')}")
    return "\n".join(lines)

def _execute_depend(path: str) -> str:
    try:
        p = _safe_path(path)
    except ValueError as e:
        return f"[错误] {e}"
    rel = _project_rel(p)
    index = _ensure_project_index()
    files = index.get("files", {})
    item = files.get(rel)
    if not item:
        return f"索引中没有文件: {path}"
    module = item.get("module", _module_name_from_rel(rel))
    imports = item.get("imports", [])
    imported_by = []
    for file_rel, file_item in files.items():
        if file_rel == rel:
            continue
        for imp in file_item.get("imports", []):
            if imp == module or imp.startswith(module + "."):
                imported_by.append(file_rel)
                break
    return (
        f"依赖关系: {rel}\n"
        f"- 模块名: {module}\n"
        f"- 它导入: {', '.join(imports) if imports else '无'}\n"
        f"- 导入它的文件: {', '.join(sorted(imported_by)) if imported_by else '无'}"
    )

# ════════════════════════════════════════════
# Bash 安全：白名单 + 黑名单 + 管道校验 (GLM)
# ════════════════════════════════════════════

def _iter_callers_of(index: Dict[str, Any], symbol: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    short = symbol.rsplit(".", 1)[-1]
    for rel, item in index.get("files", {}).items():
        for func, sites in item.get("call_sites", {}).items():
            for site in sites:
                if site.get("name") == symbol or site.get("short") == short:
                    rows.append({"file": rel, "function": func, "line": site.get("line", 0),
                                 "call": site.get("name", ""), "args_from_params": site.get("args_from_params", [])})
    return rows


def _execute_trace(symbol: str) -> str:
    symbol = symbol.strip()
    if not symbol:
        return "用法: /d trace 函数名"
    index = _ensure_project_index()
    matches = index.get("symbols", {}).get(symbol, [])
    callers = _iter_callers_of(index, symbol)
    lines = [f"调用链 trace: {symbol}"]
    if matches:
        lines.append("定义:")
        for m in matches[:8]:
            lines.append(f"- {m.get('file')}:{m.get('line')} {m.get('kind')} {m.get('module')}")
    else:
        lines.append("定义: 未在索引中精确找到，以下按调用名搜索。")
    lines.append("谁调用它:")
    if callers:
        for c in callers[:20]:
            flow = f" | 数据流参数: {', '.join(c['args_from_params'])}" if c.get("args_from_params") else ""
            lines.append(f"- {c['file']}:{c['line']} {c['function']} -> {c['call']}{flow}")
    else:
        lines.append("- 暂无记录")
    lines.append("它调用谁:")
    for m in matches[:5]:
        item = index.get("files", {}).get(m.get("file", ""), {})
        callees = item.get("calls", {}).get(symbol, [])
        flows = item.get("data_flow", {}).get(symbol, [])
        lines.append(f"- {m.get('file')}:{m.get('line')} -> {', '.join(callees[:30]) if callees else '(无)'}")
        for edge in flows[:12]:
            lines.append(f"  data {edge.get('from')} -> {edge.get('to')} @L{edge.get('line')}")
    return "\n".join(lines[:80])


def _execute_impact(path: str) -> str:
    try:
        p = _safe_path(path)
    except ValueError as e:
        return f"[错误] {e}"
    rel = _project_rel(p)
    index = _ensure_project_index()
    item = index.get("files", {}).get(rel)
    if not item:
        return f"索引中没有文件: {path}"
    module = item.get("module", _module_name_from_rel(rel))
    imported_by = []
    for file_rel, file_item in index.get("files", {}).items():
        if file_rel == rel:
            continue
        if any(imp == module or imp.startswith(module + ".") for imp in file_item.get("imports", [])):
            imported_by.append(file_rel)
    defs = [d.get("name") for d in item.get("defs", []) if d.get("kind") == "function"]
    callers: List[str] = []
    for name in defs:
        for c in _iter_callers_of(index, name):
            callers.append(f"{c['file']}:{c['line']} {c['function']} -> {name}")
    lines = [f"影响分析: {rel}", f"模块: {module}"]
    lines.append(f"导入它的文件({len(imported_by)}): {', '.join(sorted(imported_by)[:30]) if imported_by else '(无)'}")
    lines.append(f"本文件函数({len(defs)}): {', '.join(defs[:30]) if defs else '(无)'}")
    if callers:
        lines.append(f"调用方({len(callers)}):")
        lines.extend(f"- {row}" for row in callers[:40])
    else:
        lines.append("调用方: 暂无索引记录")
    return "\n".join(lines)


def _execute_sidefx(symbol: str) -> str:
    symbol = symbol.strip()
    if not symbol:
        return "用法: /d sidefx 函数名"
    index = _ensure_project_index()
    matches = index.get("symbols", {}).get(symbol, [])
    if not matches:
        return f"未找到函数: {symbol}"
    lines = [f"副作用分析: {symbol}"]
    for m in matches[:8]:
        item = index.get("files", {}).get(m.get("file", ""), {})
        effects = item.get("side_effects", {}).get(symbol, []) or m.get("side_effects", [])
        lines.append(f"- {m.get('file')}:{m.get('line')}")
        if not effects:
            lines.append("  未发现明显副作用")
            continue
        for eff in effects[:20]:
            lines.append(f"  {eff.get('type')} @L{eff.get('line')}: {eff.get('detail')}")
    return "\n".join(lines)


def _function_body_lines(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=path)
        lines = source.splitlines()
    except Exception:
        return []
    result: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body: List[str] = []
        for stmt in node.body:
            lo = getattr(stmt, "lineno", 0) - 1
            hi = getattr(stmt, "end_lineno", getattr(stmt, "lineno", 0)) - 1
            for i in range(max(lo, 0), min(hi + 1, len(lines))):
                stripped = re.sub(r"(['\"]).*?\1", "STR", lines[i].strip())
                if stripped and not stripped.startswith("#"):
                    body.append(stripped)
        if len(body) >= 5:
            result.append({"name": node.name, "line": node.lineno, "lines": body, "set": set(body)})
    return result


def _find_related_files(path: str) -> List[Dict[str, Any]]:
    try:
        p = _safe_path(path)
    except ValueError:
        return []
    rel = _project_rel(p)
    index = _ensure_project_index()
    item = index.get("files", {}).get(rel, {})
    module = item.get("module", _module_name_from_rel(rel))
    related: Dict[str, List[str]] = {}
    for file_rel, file_item in index.get("files", {}).items():
        if file_rel == rel:
            continue
        if any(imp == module or imp.startswith(module + ".") for imp in file_item.get("imports", [])):
            related.setdefault(file_rel, []).append(f"导入了 {module}")
    for d in item.get("defs", []):
        if d.get("kind") != "function":
            continue
        for caller in _iter_callers_of(index, d.get("name", "")):
            if caller["file"] != rel:
                related.setdefault(caller["file"], []).append(
                    f"L{caller['line']} 调用了 {d.get('name')}"
                )
    base = os.path.splitext(os.path.basename(rel))[0].lower()
    for file_rel in index.get("files", {}):
        if file_rel == rel:
            continue
        name = os.path.basename(file_rel).lower()
        if base in name and (file_rel.startswith("tests/") or name.startswith("test_")):
            related.setdefault(file_rel, []).append("疑似对应测试文件")
    funcs = _function_body_lines(p)
    if funcs:
        for other_rel in list(index.get("files", {}).keys())[:300]:
            if other_rel == rel:
                continue
            other_path = _safe_path(other_rel)
            for f1 in funcs[:20]:
                for f2 in _function_body_lines(other_path)[:20]:
                    common = len(f1["set"] & f2["set"])
                    ratio = common / max(len(f1["set"]), len(f2["set"]), 1)
                    if ratio >= 0.75:
                        related.setdefault(other_rel, []).append(
                            f"{f2['name']} 与 {f1['name']} AST/行结构相似 {int(ratio * 100)}%"
                        )
                        break
                if other_rel in related and any("相似" in r for r in related[other_rel]):
                    break
    rows = []
    for file_rel, reasons in related.items():
        rows.append({"file": file_rel, "reasons": sorted(set(reasons))[:5]})
    rows.sort(key=lambda r: (0 if r["file"].startswith("tests/") else 1, r["file"]))
    return rows


def _execute_related(path: str) -> str:
    if not path.strip():
        return "用法: /d related 文件"
    rows = _find_related_files(path)
    if not rows:
        return f"未找到明显关联文件: {path}"
    lines = [f"关联修改建议: {path}"]
    for row in rows[:40]:
        lines.append(f"- {row['file']}: {'; '.join(row['reasons'])}")
    return "\n".join(lines)


def _format_related_hint(changed_files: List[str]) -> str:
    hints: List[str] = []
    seen = set()
    for rel in changed_files[:5]:
        for row in _find_related_files(rel)[:8]:
            key = (row["file"], tuple(row["reasons"]))
            if key in seen:
                continue
            seen.add(key)
            hints.append(f"{row['file']}: {'; '.join(row['reasons'])}")
            if len(hints) >= 12:
                break
    if not hints:
        return ""
    return "💡 这个改动可能影响这些文件：\n" + "\n".join(f"- {h}" for h in hints)


_BASH_BLACKLIST = [
    "rm -rf /", "rm -rf *", "rm -r *", "rm -rf ~", "rm -rf .",
    "del /s /q C:", "format ", "mkfs.",
    "shutdown", "reboot", "halt", "poweroff",
    "curl | sh", "wget | sh", "| sh", "| bash",
    "> /dev/sd", "dd if=", ":(){ :|:& };:",
    "chmod -R 777 /", "chown -R",
    "net user", "net localgroup", "reg add", "reg delete",
]

_BASH_ALLOWED_PREFIXES = [
    # 只保留读操作命令 — pip/pip3/git push/git reset等破坏性命令已移除
    "ls", "dir", "cat", "head", "tail", "wc",
    "grep", "rg", "find", "fd",
    "python", "python3",
    "git status", "git log", "git diff", "git branch", "git show",
    "echo", "type", "cd", "pwd", "env",
    "tree", "du", "df", "sort", "uniq", "cut", "awk", "sed",
    "pytest", "python -m pytest", "ruff", "flake8", "mypy",
    "node", "which", "where", "stat", "file",
    "md5sum", "sha256sum", "date", "whoami",
    "cargo", "make",
]

def _is_bash_allowed(command: str) -> Tuple[bool, str]:
    cmd_stripped = command.strip()
    cmd_lower = cmd_stripped.lower()

    for black in _BASH_BLACKLIST:
        if black.lower() in cmd_lower:
            return False, f"禁止: 包含 '{black}'"

    if ">" in cmd_lower and any(d in cmd_lower for d in ["/dev/", "c:\\", "c:/"]):
        return False, "禁止重定向到系统设备"

    first_word = cmd_lower.split()[0] if cmd_lower.split() else ""

    allowed = any(cmd_lower.startswith(p.lower()) for p in _BASH_ALLOWED_PREFIXES)

    if not allowed and "|" in cmd_lower:
        parts = cmd_lower.split("|")
        if all(any(p.strip().startswith(ap.lower()) for ap in _BASH_ALLOWED_PREFIXES) for p in parts):
            allowed = True

    if not allowed:
        return False, f"命令 '{first_word}' 不在白名单"
    return True, ""

# ════════════════════════════════════════════
# 7个工具定义 (OpenAI function calling)
# ════════════════════════════════════════════

TOOL_DEFINITIONS = [
    {
        "type": "function", "function": {
            "name": "read_file",
            "description": "读取项目目录下的文件内容。支持行号范围和分页。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 'dong/config.py'"},
                    "offset": {"type": "integer", "description": "起始行号(1开始)，默认1"},
                    "limit": {"type": "integer", "description": "最多读取行数，默认500"},
                    "page": {"type": "integer", "description": "页码，每页500行；大文件建议用 page"},
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "write_file",
            "description": "创建或覆盖项目目录下的文件。自动创建父目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                    "content": {"type": "string", "description": "文件内容"},
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "edit_file",
            "description": "编辑文件：old_text → new_text 精确替换。替换所有出现处，未找到则报错。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                    "old_text": {"type": "string", "description": "要替换的原文本(需精确匹配)"},
                    "new_text": {"type": "string", "description": "替换后的新文本"},
                },
                "required": ["path", "old_text", "new_text"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "review_code",
            "description": "审查项目内Python文件，检查语法错误、导入问题、明显逻辑bug、安全风险和重复代码，返回带行号的问题列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要审查的项目内文件路径，如 'dong/agent.py'"},
                    "focus": {"type": "string", "description": "可选关注点，如 syntax/security/imports/logic"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "modify_self",
            "description": "按问题描述修改项目内Python文件。提供 old_text/new_text 时精确替换；否则会先审查并给出需要人工确认的修改建议。写入后自动 py_compile 验证。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要修改的项目内Python文件路径；跨文件自动定位时可填 auto"},
                    "issue": {"type": "string", "description": "要修复的问题描述"},
                    "old_text": {"type": "string", "description": "可选：要替换的原文本"},
                    "new_text": {"type": "string", "description": "可选：替换后的新文本"}
                },
                "required": ["issue"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "locate_symbol",
            "description": "从项目结构缓存中快速定位函数或类定义，返回文件和行号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "函数名或类名"}
                },
                "required": ["symbol"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "file_dependencies",
            "description": "列出一个Python文件导入了谁，以及谁导入了它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "项目内Python文件路径"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "search_code",
            "description": "在项目中搜索代码(类似grep)。支持正则、文件类型过滤。优先用rg，fallback grep。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "搜索模式(支持正则)，如 'def send_msg'"},
                    "path": {"type": "string", "description": "子目录，默认整个项目"},
                    "file_type": {"type": "string", "description": "文件扩展名过滤，如 'py'"},
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "run_bash",
            "description": "在项目目录执行bash命令。仅白名单命令，禁止危险操作，30秒超时。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认30"},
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "search_memory",
            "description": "搜索冬的记忆库，查找历史对话和约定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {"type": "string", "description": "搜索关键词，空格分隔，如 '生日 约定'"},
                },
                "required": ["keywords"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "list_directory",
            "description": "列出项目目录下的文件和子目录。显示文件大小。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "子目录，默认项目根目录"},
                },
                "required": []
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "computer_control",
            "description": "智能桌面操作。screenshot=截图+自动分析界面元素(SoM标注)返回可点击元素列表；click_element=按名称/文本智能点击(不需坐标)；type_in_field=定位输入框+输入文本；analyze_screen=分析屏幕返回文本描述。也支持原始操作：click/move/type/press/scroll/launch。当你无法通过纯代码完成任务时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "screenshot(截图+元素分析)/analyze_screen(文本描述)/click_element(按名称点击)/type_in_field(字段输入)/click(坐标点击,x,y)/move(移动)/type/scroll/launch/press/double_click"},
                    "name": {"type": "string", "description": "元素名称(click_element/type_in_field时用，如'登录''搜索框')"},
                    "target": {"type": "string", "description": "目标元素名(name的别名)"},
                    "field": {"type": "string", "description": "输入框名称(type_in_field时用)"},
                    "x": {"type": "integer", "description": "X坐标(click/move时)"},
                    "y": {"type": "integer", "description": "Y坐标(click/move时)"},
                    "text": {"type": "string", "description": "输入文本(type/type_in_field时)"},
                    "window": {"type": "string", "description": "目标窗口名(launch/type时可选)"},
                    "key": {"type": "string", "description": "按键名(press时，enter/escape/tab)"},
                    "amount": {"type": "integer", "description": "滚动量(scroll时)"},
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "search_online",
            "description": "在互联网上搜索信息、教程、解决方案。当你需要外部知识时使用。返回搜索结果摘要和链接。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，如 'python playwright 教程'"},
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "install_skill",
            "description": "从网上下载并安装一个新技能到冬的技能库。当你发现当前无法完成的任务可以通过某个外部工具/库/技能来解决时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名称，如 'browser-automation'"},
                    "description": {"type": "string", "description": "技能的一句话描述"},
                    "steps": {"type": "string", "description": "技能的步骤说明（Markdown格式），用\\n换行"},
                    "triggers": {"type": "string", "description": "触发词，逗号分隔，如 '浏览器,打开网页,截图网页'"},
                    "requires": {"type": "string", "description": "需要的Python包，逗号分隔，如 'playwright,selenium'"},
                },
                "required": ["name", "description", "steps"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "wechat_send",
            "description": "通过微信桌面端发送消息。自动打开微信、搜索联系人/群聊、发送文本。使用UIA键盘快捷键方案，稳定可靠。",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "接收者：联系人备注/群名/文件传输助手"},
                    "message": {"type": "string", "description": "要发送的消息内容"},
                },
                "required": ["to", "message"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "record_demo",
            "description": "开始录制操作演示。调用后截图记录当前屏幕，之后每隔0.8秒自动截图。配合learn_from_demo使用——让主人演示一次操作，然后VLM分析截图序列自动生成技能。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "演示名称/技能名，如 'wechat-send-message'"},
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "learn_from_demo",
            "description": "停止录制，用VLM分析录制的截图序列，自动提取操作步骤并生成SKILL.md技能。与record_demo配对使用。",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function", "function": {
            "name": "learn_from_video",
            "description": "从录屏视频学习操作。用ffmpeg提取关键帧→VLM逐帧分析→提取操作步骤→生成SKILL.md技能。主人录一段操作视频丢给agent就能学会。",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_path": {"type": "string", "description": "视频文件的绝对路径，如 D:/videos/demo.mp4"},
                    "skill_name": {"type": "string", "description": "要创建的技能名称，如 'login-to-website'"},
                },
                "required": ["video_path", "skill_name"]
            }
        }
    },
    {
        "type": "function", "function": {
            "name": "browser_control",
            "description": "控制Chromium浏览器：打开网页、点击元素(CSS选择器)、输入文本、截图、读取页面内容。用于需要浏览器操作的任务(填表单、查网页、比价等)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "open(打开URL)/click(点击元素)/type(输入文本)/screenshot(截图)/read(读页面文本)/close(关闭浏览器)"},
                    "url": {"type": "string", "description": "网页URL(open时必填，如 https://github.com)"},
                    "selector": {"type": "string", "description": "CSS选择器(click/type时必填，如 '#search-input' 或 '.btn-primary')"},
                    "text": {"type": "string", "description": "输入文本(type时必填)"},
                },
                "required": ["action"]
            }
        }
    },
]

# ── 浏览器实例（模块级，复用）──
_BROWSER = None
_PAGE = None

def _execute_browser_control(action: str, url: str = "", selector: str = "",
                              text: str = "") -> str:
    """Playwright浏览器控制"""
    global _BROWSER, _PAGE
    try:
        from playwright.sync_api import sync_playwright
        import time as _t

        # 启动浏览器（首次或重新打开）
        if _BROWSER is None:
            pw = sync_playwright().start()
            _BROWSER = pw.chromium.launch(headless=False)
            _PAGE = _BROWSER.new_page()
            log("[agent] 浏览器已启动")

        if action == "open":
            if not url:
                return "[错误] 请提供URL，如 action=open,url=https://github.com"
            if not url.startswith("http"):
                url = "https://" + url
            _PAGE.goto(url, wait_until="domcontentloaded", timeout=30)
            _t.sleep(1)
            return f"[成功] 已打开 {url}\n页面标题: {_PAGE.title()}\nURL: {_PAGE.url}"

        elif action == "click":
            if not selector:
                return "[错误] 请提供CSS选择器"
            _PAGE.wait_for_selector(selector, timeout=10)
            _PAGE.click(selector)
            _t.sleep(0.5)
            return f"[成功] 已点击 {selector}"

        elif action == "type":
            if not selector or not text:
                return "[错误] 请提供selector和text"
            _PAGE.wait_for_selector(selector, timeout=10)
            _PAGE.fill(selector, text)
            return f"[成功] 已在 {selector} 输入: {text[:30]}"

        elif action == "screenshot":
            path = os.path.join(PROJECT_DIR, "dong_screenshots",
                                f"browser_{int(_t.time())}.png")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _PAGE.screenshot(path=path, full_page=False)
            # VLM描述
            try:
                from .gui_agent import analyze_screen
                desc = analyze_screen()
            except Exception:
                desc = ""
            result = f"[成功] 截图已保存: {path}"
            if desc:
                result += f"\n页面描述: {desc[:800]}"
            return result

        elif action == "read":
            content = _PAGE.inner_text("body")[:4000]
            title = _PAGE.title()
            return f"页面标题: {title}\nURL: {_PAGE.url}\n内容:\n{content}"

        elif action == "close":
            if _BROWSER:
                _BROWSER.close()
                _BROWSER = None
                _PAGE = None
            return "[成功] 浏览器已关闭"

        else:
            return f"[错误] 未知操作: {action}。可用: open, click, type, screenshot, read, close"
    except ImportError:
        return "[错误] playwright未安装。请 pip install playwright && playwright install chromium"
    except Exception as e:
        return f"[错误] 浏览器操作失败: {e}"

# ════════════════════════════════════════════
# 工具执行器
# ════════════════════════════════════════════

def _execute_read_file(path: str, offset: int = 1, limit: int = None, page: int = None) -> str:
    try:
        p = _safe_path(path)
        if not os.path.isfile(p):
            return f"[错误] 文件不存在: {path}"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        _remember_read_file(path)
        page_size = max(1, READ_PAGE_LINES)
        if page is not None:
            page = max(1, int(page))
            start = (page - 1) * page_size
            end = min(start + page_size, total)
        else:
            limit = page_size if limit is None else max(1, int(limit))
            start = max(0, int(offset or 1) - 1)
            end = min(start + limit, total)
        selected = lines[start:end]
        result = []
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = page if page is not None else (start // page_size) + 1
        result.append(
            f"[read_file] {path} 第 {current_page}/{total_pages} 页，"
            f"行 {start + 1}-{end}/{total}。继续读: /d read {path}:{min(current_page + 1, total_pages)}"
        )
        for i, line in enumerate(selected, start=start + 1):
            result.append(f"{i:6d}→{line.rstrip()}")
        out = "\n".join(result)
        if len(out) > 15000:
            out = out[:15000] + f"\n... (截断，共{len(selected)}行)"
        return out if out else "(空文件)"
    except ValueError as e:
        return f"[错误] {e}"
    except Exception as e:
        return f"[错误] 读取失败: {e}"

def _execute_write_file(path: str, content: str) -> str:
    try:
        p = _safe_path(path)
        # 备份原文件
        backup = None
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                backup = f.read()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        _record_patch(path, backup or "", content,
                      line_start=1, line_end=content.count("\n") + 1)
        lines = content.count("\n") + 1
        # .py 文件写完验证编译
        if p.endswith(".py"):
            ok, detail = _verify_python_file(p)
            if not ok:
                if backup is not None:
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(backup)
                    return f"[回滚] py_compile 失败，原文件已恢复：{detail}"
                else:
                    os.remove(p)
                    return f"[回滚] 新文件 py_compile 失败，已删除：{detail}"
        _update_project_index_for(p)
        log(f"[agent] 写入: {path} ({lines}行)")
        return f"已写入 {path} ({lines} 行)" + ("；py_compile通过" if p.endswith(".py") else "")
    except ValueError as e:
        return f"[错误] {e}"
    except Exception as e:
        return f"[错误] 写入失败: {e}"

def _execute_edit_file(path: str, old_text: str, new_text: str) -> str:
    try:
        p = _safe_path(path)
        if not os.path.isfile(p):
            return f"[错误] 文件不存在: {path}"
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()
        if old_text not in content:
            return f"[错误] 未找到要替换的文本。请用 read_file 确认内容后重试"
        count = content.count(old_text)
        new_content = content.replace(old_text, new_text)
        line_start = content[:content.find(old_text)].count("\n") + 1 if old_text in content else 0
        line_end = line_start + old_text.count("\n")
        _record_patch(path, old_text, new_text, line_start=line_start, line_end=line_end)
        with open(p, "w", encoding="utf-8") as f:
            f.write(new_content)
        if p.endswith(".py"):
            ok, detail = _verify_python_file(p)
            if not ok:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)
                return f"[回滚] 已替换但 py_compile 失败，原文件已恢复：{detail}"
        _update_project_index_for(p)
        log(f"[agent] 编辑: {path} ({count}处替换)")
        return f"已替换 {path} 中的 {count} 处匹配" + ("；py_compile通过" if p.endswith(".py") else "")
    except ValueError as e:
        return f"[错误] {e}"
    except Exception as e:
        return f"[错误] 编辑失败: {e}"

def _verify_python_file(path: str) -> Tuple[bool, str]:
    try:
        py_compile.compile(path, doraise=True)
        return True, "py_compile通过"
    except py_compile.PyCompileError as e:
        return False, str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1

def _format_findings(path: str, findings: List[Dict[str, Any]]) -> str:
    if not findings:
        return f"审查完成：{path}\n维度：语法错误、逻辑bug、安全面、重复代码。\n未发现明确问题。"
    order = {"致命": 0, "高": 1, "中": 2, "低": 3}
    findings = sorted(findings, key=lambda x: (order.get(x.get("severity", "低"), 9), x.get("line", 0)))
    lines = [f"审查报告：{path}", "维度：语法错误、逻辑bug、安全面、重复代码。"]
    for i, f in enumerate(findings, 1):
        lines.append(
            f"{i}. L{f.get('line', '需进一步确认')} [{f.get('severity', '中')}] "
            f"{f.get('title', '问题')}: {f.get('detail', '')}\n   修复建议：{f.get('fix', '需进一步确认')}"
        )
        if f.get("code"):
            lines.append(f"   修复代码：\n```python\n{f['code']}\n```")
    return "\n".join(lines)

def _add_finding(findings: List[Dict[str, Any]], line: int, severity: str,
                 title: str, detail: str, fix: str, code: str = ""):
    findings.append({
        "line": line,
        "severity": severity,
        "title": title,
        "detail": detail,
        "fix": fix,
        "code": code,
    })

class _ReviewVisitor(ast.NodeVisitor):
    def __init__(self, source_lines: List[str], findings: List[Dict[str, Any]]):
        self.lines = source_lines
        self.findings = findings
        self.imports: Dict[str, int] = {}
        self.used_names: set = set()
        self.func_defs: Dict[str, List[int]] = {}

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imports[alias.asname or alias.name.split(".")[0]] = node.lineno
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        mod = node.module or ""
        if node.level == 0 and mod:
            root = mod.split(".")[0]
            if importlib.util.find_spec(root) is None and root not in {"typing"}:
                _add_finding(
                    self.findings, node.lineno, "中", "疑似缺失导入依赖",
                    f"模块 {mod!r} 在当前解释器中未找到。",
                    "确认依赖是否写入 requirements.txt，或改为可选导入并给出降级路径。",
                    f"try:\n    from {mod} import ...\nexcept ImportError as e:\n    log(f\"可选依赖缺失: {{e}}\")",
                )
        for alias in node.names:
            self.imports[alias.asname or alias.name] = node.lineno
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load):
            self.used_names.add(node.id)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self.func_defs.setdefault(node.name, []).append(node.lineno)
        for default in list(node.args.defaults) + list(node.args.kw_defaults):
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                _add_finding(
                    self.findings, node.lineno, "中", "可变默认参数",
                    f"函数 {node.name} 使用 list/dict/set 作为默认值，会在多次调用间共享状态。",
                    "将默认值改为 None，函数内部再创建新对象。",
                    f"def {node.name}(..., value=None):\n    if value is None:\n        value = []",
                )
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        line = self.lines[node.lineno - 1].strip() if 0 < node.lineno <= len(self.lines) else "except"
        only_pass = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
        if node.type is None:
            _add_finding(
                self.findings, node.lineno, "中", "裸 except",
                "裸 except 会吞掉 KeyboardInterrupt/SystemExit，也会隐藏真实错误。",
                "改成 except Exception as e，并记录日志或返回错误。",
                "except Exception as e:\n    log(f\"操作失败: {e}\")",
            )
        if only_pass:
            _add_finding(
                self.findings, node.lineno, "中", "异常静默吞掉",
                f"{line} 后直接 pass，线上失败没有诊断信号。",
                "至少记录异常上下文；关键路径应向调用方返回失败。",
                "except Exception as e:\n    log(f\"这里失败了: {e}\")\n    return None",
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
            owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
            if owner:
                func_name = f"{owner}.{func_name}"

        if func_name in {"eval", "exec"}:
            _add_finding(
                self.findings, node.lineno, "高", "动态执行代码",
                f"调用 {func_name}，若输入来自 LLM/用户会导致任意代码执行。",
                "移除动态执行；必须保留时放入隔离子进程并限制 builtins/imports。",
                "# 避免 exec(user_code)\nresult = safe_dispatch[action](validated_args)",
            )
        if func_name in {"requests.get", "requests.post"}:
            if not any(kw.arg == "timeout" for kw in node.keywords):
                _add_finding(
                    self.findings, node.lineno, "中", "HTTP 调用缺少 timeout",
                    "网络调用没有超时，可能卡死消息循环或后台线程。",
                    "补充 timeout，并优先复用统一 API gateway/session。",
                    "requests.post(url, json=payload, timeout=15)",
                )
        if func_name in {"subprocess.run", "subprocess.Popen", "asyncio.create_subprocess_shell"}:
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    _add_finding(
                        self.findings, node.lineno, "高", "shell=True 命令注入面",
                        "shell=True 会让字符串经过 shell 解释，用户/LLM 参数可能变成命令。",
                        "改为参数列表；协议启动也用白名单映射。",
                        "subprocess.run([\"cmd\", \"/c\", \"type\", tmp.name], timeout=5)",
                    )
        self.generic_visit(node)

    def finish(self):
        for name, locs in self.func_defs.items():
            if len(locs) > 1:
                _add_finding(
                    self.findings, locs[1], "低", f"重复函数名 {name}",
                    f"同名函数定义出现在行 {locs}，后者会覆盖前者。",
                    "确认是否保留两个实现；通常应合并或重命名。",
                    f"# 将其中一个函数重命名\n# def {name}_legacy(...): ...",
                )
        for name, line in self.imports.items():
            if name.startswith("_") or name in {"typing"}:
                continue
            if name not in self.used_names:
                _add_finding(
                    self.findings, line, "低", f"疑似未使用导入 {name}",
                    "导入后未在当前文件中读取，增加启动成本并掩盖依赖边界。",
                    "删除未使用导入，或移动到真正需要的函数内部。",
                    f"# 删除: import {name}",
                )

def _execute_review_code(path: str, focus: str = "") -> str:
    try:
        p = _safe_path(path)
        if not os.path.isfile(p):
            return f"[错误] 文件不存在: {path}"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        lines = text.splitlines()
        findings: List[Dict[str, Any]] = []

        if p.endswith(".py"):
            try:
                tree = ast.parse(text, filename=p)
            except SyntaxError as e:
                _add_finding(
                    findings, e.lineno or 1, "致命", "语法错误", e.msg,
                    "先修正该语法错误，再运行 py_compile 验证。",
                    "# 修复该行附近的括号、引号或缩进后运行：\n# python -m py_compile <file>",
                )
                tree = None
            if tree is not None:
                visitor = _ReviewVisitor(lines, findings)
                visitor.visit(tree)
                visitor.finish()
                _detect_similar_functions(tree, lines, findings)

        for idx, line in enumerate(lines, 1):
            stripped = line.strip()
            if "open(" in line and ("\"w\"" in line or "'w'" in line) and "os.replace" not in text:
                _add_finding(
                    findings, idx, "低", "非原子写文件",
                    "直接写目标文件，崩溃或并发写入可能留下半截 JSON/源码。",
                    "写临时文件后 os.replace 原子替换。",
                    "tmp = path + '.tmp'\nwith open(tmp, 'w', encoding='utf-8') as f:\n    f.write(content)\nos.replace(tmp, path)",
                )
            if "TODO" in stripped or "FIXME" in stripped:
                _add_finding(
                    findings, idx, "低", "遗留 TODO/FIXME",
                    stripped[:160],
                    "确认是否仍有效；有效则拆成明确任务，无效则删除。",
                    "# TODO: 改成具体缺陷编号或删除",
                )

        return _format_findings(path, findings[:30])
    except ValueError as e:
        return f"[错误] {e}"
    except Exception as e:
        return f"[错误] 审查失败: {e}"

def _execute_modify_self(path: str, issue: str, old_text: str = "", new_text: str = "") -> str:
    if (not path or path in {"*", "auto", "AUTO"}) and issue:
        return _execute_fix_issue(issue)
    snippet_hint = ""
    try:
        if path and not os.path.exists(_safe_path(path)):
            matches = _search_code_snippets(issue)
            if matches:
                snippet_hint = f"\n\n可用片段模板: {matches[0].get('name')}\n{matches[0].get('code', '')[:1200]}"
    except Exception:
        snippet_hint = ""
    if old_text and new_text:
        result = _execute_edit_file(path, old_text, new_text)
        review = _execute_review_code(path, "post-fix")
        return f"{result}\n\n复查：\n{review[:2000]}"
    review = _execute_review_code(path, issue)
    return (
        f"已定位 {path}，但未提供 old_text/new_text，暂不自动改写。\n"
        f"问题描述：{issue}\n\n{review}{snippet_hint}\n\n"
        "请用 modify_self(path, issue, old_text, new_text) 精确替换，或先 read_file 获取上下文。"
    )

def _project_py_files(limit: int = 120) -> List[str]:
    files = []
    for root, dirs, names in os.walk(PROJECT_DIR):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", "dong_backups", "data", "skills", ".git"}]
        for name in names:
            if name.endswith(".py"):
                full = os.path.join(root, name)
                files.append(os.path.relpath(full, PROJECT_DIR).replace("\\", "/"))
                if len(files) >= limit:
                    return files
    return files

def _builtin_snippets() -> List[Dict[str, Any]]:
    return [
        {
            "name": "register_check_health",
            "kind": "decorator",
            "keywords": ["health", "健康检查", "register_check", "CheckLevel"],
            "source": "builtin",
            "code": (
                "from .core.health_registry import CheckLevel, register_check\n\n"
                "@register_check(\"{check_name}\", interval={interval}, level=CheckLevel.{level})\n"
                "def check_{safe_name}():\n"
                "    try:\n"
                "        # TODO: read-only health probe\n"
                "        return True, \"OK\"\n"
                "    except Exception as e:\n"
                "        return False, str(e)\n"
            ),
        },
        {
            "name": "atomic_json_write",
            "kind": "json_io",
            "keywords": ["json", "atomic", "os.replace", "原子写"],
            "source": "builtin",
            "code": (
                "tmp = path + '.tmp'\n"
                "with open(tmp, 'w', encoding='utf-8') as f:\n"
                "    json.dump(data, f, ensure_ascii=False, indent=2)\n"
                "os.replace(tmp, path)\n"
            ),
        },
    ]


def _snippet_source_window(lines: List[str], start: int, end: int, pad: int = 1) -> str:
    lo = max(0, start - 1 - pad)
    hi = min(len(lines), end + pad)
    return "\n".join(lines[lo:hi]).strip()


def _decorator_name(dec: ast.AST) -> str:
    if isinstance(dec, ast.Call):
        return _ast_call_name(dec.func)
    return _ast_call_name(dec)


def _scan_code_snippets() -> List[Dict[str, Any]]:
    snippets: List[Dict[str, Any]] = _builtin_snippets()
    seen = {s["name"] for s in snippets}
    for rel in _project_py_files(limit=500):
        try:
            with open(_safe_path(rel), "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            lines = source.splitlines()
            tree = ast.parse(source)
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
                decorators = [_decorator_name(d) for d in node.decorator_list]
                if any(decorators):
                    code = _snippet_source_window(lines, node.lineno, getattr(node, "end_lineno", node.lineno), 0)
                    kind = "decorator"
                    name = f"decorator_{decorators[0].split('.')[-1]}_{node.name}"
                    if "register_check" in " ".join(decorators):
                        name = f"register_check_{node.name}"
                    if name not in seen:
                        snippets.append({"name": name, "kind": kind, "keywords": decorators + [node.name],
                                         "source": f"{rel}:{node.lineno}", "code": code[:1200], "uses": 0})
                        seen.add(name)
            if isinstance(node, ast.ExceptHandler):
                code = _snippet_source_window(lines, node.lineno, getattr(node, "end_lineno", node.lineno), 1)
                key = re.sub(r"\W+", "_", code[:60])[:50]
                name = f"exception_{key}"
                if name not in seen:
                    snippets.append({"name": name, "kind": "exception", "keywords": ["try", "except", "异常处理"],
                                     "source": f"{rel}:{node.lineno}", "code": code[:800], "uses": 0})
                    seen.add(name)
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if any(k in stripped for k in ("json.load", "json.dump", "json.loads", "json.dumps")):
                name = f"json_{rel.replace('/', '_')}_{i}"
                if name not in seen:
                    snippets.append({"name": name, "kind": "json_io", "keywords": ["json", "读写", "load", "dump"],
                                     "source": f"{rel}:{i}", "code": _snippet_source_window(lines, i, i, 2)[:800], "uses": 0})
                    seen.add(name)
            if any(k in stripped for k in ("requests.get", "requests.post", "gateway.call", "ClientSession")):
                name = f"api_{rel.replace('/', '_')}_{i}"
                if name not in seen:
                    snippets.append({"name": name, "kind": "api_call", "keywords": ["api", "requests", "gateway", "timeout"],
                                     "source": f"{rel}:{i}", "code": _snippet_source_window(lines, i, i, 3)[:1000], "uses": 0})
                    seen.add(name)
        if len(snippets) >= 120:
            break
    return snippets[:120]


def _save_code_snippets(snippets: List[Dict[str, Any]]):
    tmp = CODE_SNIPPETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"built_at": datetime.now().isoformat(), "snippets": snippets},
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp, CODE_SNIPPETS_FILE)


def _load_code_snippets(force: bool = False) -> List[Dict[str, Any]]:
    if not force and os.path.exists(CODE_SNIPPETS_FILE):
        try:
            with open(CODE_SNIPPETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("snippets"), list):
                return data["snippets"]
        except Exception as e:
            log(f"[agent] snippet load failed: {e}")
    snippets = _scan_code_snippets()
    try:
        _save_code_snippets(snippets)
    except Exception as e:
        log(f"[agent] snippet save failed: {e}")
    return snippets


def _search_code_snippets(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    q = query.lower().strip()
    snippets = _load_code_snippets()
    scored: List[Tuple[int, Dict[str, Any]]] = []
    for s in snippets:
        hay = " ".join([s.get("name", ""), s.get("kind", ""),
                        " ".join(s.get("keywords", [])), s.get("code", "")]).lower()
        score = sum(3 for w in _issue_keywords(q) if w in hay)
        if q and q in hay:
            score += 5
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:limit]]


def _execute_snippet_command(args: str) -> str:
    args = args.strip()
    if not args or args == "scan":
        snippets = _load_code_snippets(force=args == "scan")
        return f"片段库已加载: {len(snippets)} 条，文件: {os.path.basename(CODE_SNIPPETS_FILE)}"
    if args.startswith("add "):
        body = args[4:].strip()
        name, _, code = body.partition(" ")
        if not name or not code:
            return "用法: /d snippet add 名称 代码"
        snippets = _load_code_snippets()
        snippets.append({"name": name, "kind": "manual", "keywords": _issue_keywords(name + " " + code),
                         "source": "manual", "code": code, "uses": 0, "created": datetime.now().isoformat()})
        _save_code_snippets(snippets)
        return f"已添加片段: {name}"
    matches = _search_code_snippets(args)
    if not matches:
        return f"未找到片段: {args}"
    lines = [f"片段搜索: {args}"]
    for s in matches:
        lines.append(f"\n## {s.get('name')} [{s.get('kind')}] {s.get('source')}\n{s.get('code', '')[:1200]}")
    return "\n".join(lines)


def _render_health_check_snippet(text: str) -> str:
    matches = _search_code_snippets("register_check health 健康检查")
    tpl = next((s for s in matches if "register_check" in s.get("code", "")), None)
    if not tpl:
        tpl = _builtin_snippets()[0]
    raw_name = "_".join(_issue_keywords(text)[:3]) or "custom_health"
    safe_name = re.sub(r"\W+", "_", raw_name).strip("_") or "custom_health"
    code = tpl["code"].format(check_name=safe_name, safe_name=safe_name,
                              interval=3600, level="WARN")
    return f"套用片段: {tpl.get('name')}\n```python\n{code}\n```"


def _locate_fix_targets(issue: str, limit: int = 12) -> List[str]:
    mentioned = []
    for m in re.finditer(r"([A-Za-z0-9_./\\-]+\.py)", issue):
        path = m.group(1).replace("\\", "/")
        try:
            _safe_path(path)
            if path not in mentioned:
                mentioned.append(path)
        except ValueError:
            continue
    if mentioned:
        return mentioned[:limit]

    keywords = [w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", issue) if w not in {"修复", "全部", "所有", "问题", "文件"}]
    scored: List[Tuple[int, str]] = []
    for rel in _project_py_files():
        try:
            with open(_safe_path(rel), "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        score = sum(text.count(k) for k in keywords)
        if "threading" in issue.lower() and "threading.Lock" in text:
            score += 20
        if ("except" in issue.lower() or "吞" in issue) and re.search(r"except\s+(Exception)?\s*:\s*\n\s*pass", text):
            score += 10
        if "shell" in issue.lower() and "shell=True" in text:
            score += 10
        if score:
            scored.append((score, rel))
    scored.sort(reverse=True)
    return [rel for _, rel in scored[:limit]]

def _apply_safe_fixes_to_text(path: str, content: str, issue: str) -> Tuple[str, List[str]]:
    changed = content
    notes: List[str] = []

    if "threading.Lock" in changed and not re.search(r"(^|\n)\s*import\s+threading\b", changed):
        lines = changed.splitlines()
        insert_at = 0
        while insert_at < len(lines) and (lines[insert_at].startswith("#") or lines[insert_at].strip() == "" or lines[insert_at].startswith('"""')):
            insert_at += 1
            if insert_at > 20:
                break
        while insert_at < len(lines) and (lines[insert_at].startswith("import ") or lines[insert_at].startswith("from ")):
            insert_at += 1
        lines.insert(insert_at, "import threading")
        changed = "\n".join(lines) + ("\n" if content.endswith("\n") else "")
        notes.append("补充 import threading")

    pattern = re.compile(r"except Exception:\n(?P<indent>\s+)pass")
    def repl_exception(m):
        notes.append("将 except Exception: pass 改为记录日志")
        indent = m.group("indent")
        return f"except Exception as e:\n{indent}log(f\"自动修复记录异常: {{e}}\")"
    changed = pattern.sub(repl_exception, changed)

    pattern_bare = re.compile(r"except:\n(?P<indent>\s+)pass")
    def repl_bare(m):
        notes.append("将裸 except: pass 改为 except Exception as e 并记录")
        indent = m.group("indent")
        return f"except Exception as e:\n{indent}log(f\"自动修复记录异常: {{e}}\")"
    changed = pattern_bare.sub(repl_bare, changed)

    if "shell=True" in changed and "shell" in issue.lower():
        notes.append("检测到 shell=True：需要人工确认命令参数结构，未自动替换")

    return changed, notes

def _compile_many(paths: List[str]) -> Tuple[bool, str]:
    for rel in paths:
        p = _safe_path(rel)
        if p.endswith(".py"):
            ok, detail = _verify_python_file(p)
            if not ok:
                return False, f"{rel}: {detail}"
    return True, "全部 py_compile 通过"

# ════════════════════════════════════════════
# 验证闭环
# ════════════════════════════════════════════

def _load_learned_fixes() -> List[Dict[str, Any]]:
    try:
        if os.path.exists(LEARNED_FIXES_FILE):
            with open(LEARNED_FIXES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:
        log(f"[agent] learned fixes load failed: {e}")
    return []


def _save_learned_fixes(patterns: List[Dict[str, Any]]):
    try:
        tmp = LEARNED_FIXES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(patterns, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LEARNED_FIXES_FILE)
    except Exception as e:
        log(f"[agent] learned fixes save failed: {e}")


def _issue_keywords(issue: str) -> List[str]:
    stop = {"fix", "修复", "问题", "文件", "全部", "所有", "the", "and", "with"}
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", issue.lower())
    return sorted({w for w in words if w not in stop})[:20]


def _infer_fix_template(old: str, new: str) -> Dict[str, Any]:
    import difflib
    actions: List[str] = []
    replacements: List[Dict[str, str]] = []
    if "import threading" not in old and "import threading" in new:
        actions.append("add_import_threading")
    if re.search(r"except\s+(Exception)?\s*:\s*\n\s*pass", old) and "log(f" in new:
        actions.append("replace_silent_except")
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in {"replace", "delete"}:
            continue
        old_chunk = "\n".join(old_lines[i1:i2]).strip()
        new_chunk = "\n".join(new_lines[j1:j2]).strip()
        if old_chunk and new_chunk and len(old_chunk) <= 400 and len(new_chunk) <= 400:
            replacements.append({"old": old_chunk, "new": new_chunk})
        if len(replacements) >= 5:
            break
    return {"actions": sorted(set(actions)), "replacements": replacements}


def _learn_from_last_change(issue: str) -> Optional[Dict[str, Any]]:
    changed = _CHANGE_LOG.get("changed", [])
    snapshots = _CHANGE_LOG.get("snapshots", {})
    if not changed or not snapshots:
        return None
    actions: List[str] = []
    replacements: List[Dict[str, str]] = []
    file_types = sorted({os.path.splitext(rel)[1] or "(none)" for rel in changed})
    for rel in changed:
        old = snapshots.get(rel, "")
        try:
            with open(_safe_path(rel), "r", encoding="utf-8", errors="replace") as f:
                new = f.read()
        except Exception:
            continue
        template = _infer_fix_template(old, new)
        actions.extend(template.get("actions", []))
        replacements.extend(template.get("replacements", []))
    if not actions and not replacements:
        return None
    patterns = _load_learned_fixes()
    next_id = max([int(p.get("id", 0)) for p in patterns] or [0]) + 1
    pattern = {
        "id": next_id,
        "keywords": _issue_keywords(issue),
        "file_types": file_types,
        "actions": sorted(set(actions)),
        "replacements": replacements[:8],
        "source_issue": issue[:300],
        "created": datetime.now().isoformat(),
        "uses": 0,
    }
    patterns.append(pattern)
    _save_learned_fixes(patterns)
    return pattern


def _score_learned_pattern(pattern: Dict[str, Any], issue: str, targets: List[str]) -> int:
    issue_keys = set(_issue_keywords(issue))
    pat_keys = set(pattern.get("keywords", []))
    score = len(issue_keys & pat_keys) * 3
    target_types = {os.path.splitext(t)[1] or "(none)" for t in targets}
    score += len(target_types & set(pattern.get("file_types", []))) * 2
    return score


def _apply_learned_template(content: str, pattern: Dict[str, Any]) -> Tuple[str, List[str]]:
    changed = content
    notes: List[str] = []
    for repl in pattern.get("replacements", []):
        old = repl.get("old", "")
        new = repl.get("new", "")
        if old and new and old in changed:
            changed = changed.replace(old, new, 1)
            notes.append("历史替换模板")
    actions = set(pattern.get("actions", []))
    if "add_import_threading" in actions and "threading.Lock" in changed and "import threading" not in changed:
        changed, n = _apply_safe_fixes_to_text("", changed, "threading.Lock")
        notes.extend(n)
    if "replace_silent_except" in actions:
        changed, n = _apply_safe_fixes_to_text("", changed, "except pass")
        notes.extend(n)
    return changed, notes


def _try_learned_fix(issue: str, targets: List[str]) -> Optional[str]:
    patterns = _load_learned_fixes()
    if not patterns or not targets:
        return None
    ranked = sorted(((_score_learned_pattern(p, issue, targets), p) for p in patterns),
                    key=lambda x: x[0], reverse=True)
    score, pattern = ranked[0]
    if score < 3:
        return None
    snapshots: Dict[str, str] = {}
    changed_files: List[str] = []
    notes: List[str] = []
    for rel in targets:
        try:
            p = _safe_path(rel)
            if not os.path.isfile(p):
                continue
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                old = f.read()
            new, file_notes = _apply_learned_template(old, pattern)
            if new == old:
                continue
            snapshots[rel] = old
            with open(p, "w", encoding="utf-8") as f:
                f.write(new)
            changed_files.append(rel)
            notes.extend(f"{rel}: {n}" for n in file_notes)
        except Exception as e:
            notes.append(f"{rel}: learned template failed {e}")
    if not changed_files:
        return None
    val = run_validation(build_validation_plan(changed_files))
    if val.get("ok"):
        pattern["uses"] = int(pattern.get("uses", 0)) + 1
        pattern["last_used"] = datetime.now().isoformat()
        _save_learned_fixes(patterns)
        _record_change(issue, snapshots, changed_files, [], {}, val, None)
        return (f"[成功] 命中历史修复模式 #{pattern.get('id')}，已修改 {len(changed_files)} 个文件: "
                f"{', '.join(changed_files)}\n验证通过\n" + "\n".join(notes[:10]))
    for rel, old in snapshots.items():
        try:
            with open(_safe_path(rel), "w", encoding="utf-8") as f:
                f.write(old)
        except Exception:
            pass
    return None


def _format_learned_patterns() -> str:
    patterns = _load_learned_fixes()
    if not patterns:
        return "暂无已学习的错误模式。"
    lines = [f"已学习错误模式({len(patterns)}):"]
    for p in patterns:
        lines.append(f"{p.get('id')}. {', '.join(p.get('keywords', [])[:8]) or '(无关键词)'} "
                     f"| types={','.join(p.get('file_types', []))} "
                     f"| actions={','.join(p.get('actions', [])) or 'replace'} "
                     f"| uses={p.get('uses', 0)}")
    return "\n".join(lines)


def _forget_learned_pattern(raw_id: str) -> str:
    if not raw_id.isdigit():
        return "用法: /d patterns forget <编号>"
    target_id = int(raw_id)
    patterns = _load_learned_fixes()
    kept = [p for p in patterns if int(p.get("id", 0)) != target_id]
    if len(kept) == len(patterns):
        return f"未找到模式 #{target_id}"
    _save_learned_fixes(kept)
    return f"已删除模式 #{target_id}"


def build_validation_plan(files: List[str]) -> List[Dict[str, Any]]:
    """根据修改的文件列表构建验证计划"""
    plan: List[Dict[str, Any]] = []
    py_files = [f for f in files if f.endswith(".py")]
    if py_files:
        plan.append({"type": "py_compile", "files": py_files})
    tests_dir = os.path.join(PROJECT_DIR, "tests")
    if os.path.isdir(tests_dir) and any(
        f.startswith("tests") or f.startswith("dong") for f in files
    ):
        plan.append({"type": "pytest", "scope": "tests/"})
    return plan


def run_validation(plan: List[Dict[str, Any]]) -> Dict[str, Any]:
    """执行验证计划并返回结果"""
    results: List[Dict[str, Any]] = []
    overall_ok = True
    failed_step = None

    for step in plan:
        step_type = step.get("type", "")
        step_result = {"type": step_type, "ok": True, "detail": ""}

        if step_type == "py_compile":
            for f in step.get("files", []):
                try:
                    p = _safe_path(f) if not os.path.isabs(f) else f
                    ok, detail = _verify_python_file(p)
                    if not ok:
                        step_result["ok"] = False
                        step_result["detail"] = f"{f}: {detail}"
                        overall_ok = False
                        failed_step = "py_compile"
                        break
                except Exception as e:
                    step_result["ok"] = False
                    step_result["detail"] = f"{f}: {e}"
                    overall_ok = False
                    failed_step = "py_compile"
                    break
            if step_result["ok"]:
                step_result["detail"] = "全部 py_compile 通过"

        elif step_type == "pytest":
            try:
                scope = step.get("scope", "tests/")
                r = subprocess.run(
                    ["python", "-m", "pytest", scope, "-x", "--tb=short"],
                    capture_output=True, text=True, timeout=60,
                    cwd=PROJECT_DIR, encoding="utf-8", errors="replace"
                )
                if r.returncode != 0:
                    step_result["ok"] = False
                    step_result["detail"] = r.stdout[-2000:] + r.stderr[-1000:]
                    overall_ok = False
                    failed_step = "pytest"
                else:
                    step_result["detail"] = "pytest 全部通过"
            except subprocess.TimeoutExpired:
                step_result["ok"] = False
                step_result["detail"] = "pytest 超时(60s)"
                overall_ok = False
                failed_step = "pytest"
            except Exception as e:
                step_result["ok"] = False
                step_result["detail"] = f"pytest 异常: {e}"
                overall_ok = False
                failed_step = "pytest"

        results.append(step_result)
        if not step_result["ok"]:
            break

    return {"ok": overall_ok, "results": results, "failed_step": failed_step}


def validation_repair_loop(issue: str, changed_files: List[str]) -> Dict[str, Any]:
    """验证失败时自动修正循环（最多3轮）"""
    MAX_REPAIR_ROUNDS = 3
    repair_log: List[Dict[str, Any]] = []
    current_files = list(changed_files)

    for round_n in range(1, MAX_REPAIR_ROUNDS + 1):
        # 先验证当前状态
        plan = build_validation_plan(current_files)
        result = run_validation(plan)
        if result["ok"]:
            return {"ok": True, "rounds": round_n, "log": repair_log,
                    "detail": f"第{round_n}轮验证通过"}

        # 收集失败详情
        failed_detail = ""
        for r in result.get("results", []):
            if not r.get("ok"):
                failed_detail += r.get("detail", "") + "\n"

        repair_log.append({
            "round": round_n, "ok": False,
            "error": failed_detail[:500],
            "files": list(current_files)
        })

        # 调LLM生成修正方案
        patch = _call_repair_llm(issue, failed_detail, current_files)
        if not patch:
            repair_log[-1]["action"] = "LLM返回空，无法修正"
            break

        # 应用修正
        applied = False
        for rel in current_files:
            try:
                p = _safe_path(rel)
                with open(p, "r", encoding="utf-8") as f:
                    old = f.read()
                new, notes = _apply_safe_fixes_to_text(rel, old,
                    issue + "\n[验证失败]\n" + failed_detail[:500])
                if new != old:
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(new)
                    applied = True
                    repair_log[-1]["action"] = f"自动修正 {rel}: {', '.join(notes)}"
            except Exception as e:
                repair_log[-1]["action"] = f"修正 {rel} 异常: {e}"

        if not applied:
            repair_log[-1]["action"] = "_apply_safe_fixes_to_text 未生成修改"

    # 最后一轮验证
    plan = build_validation_plan(current_files)
    result = run_validation(plan)
    if result["ok"]:
        return {"ok": True, "rounds": MAX_REPAIR_ROUNDS, "log": repair_log,
                "detail": f"{MAX_REPAIR_ROUNDS}轮修正后验证通过"}

    return {"ok": False, "rounds": MAX_REPAIR_ROUNDS, "log": repair_log,
            "detail": f"{MAX_REPAIR_ROUNDS}轮修正后仍失败: " +
            str(result.get("results", [{}])[-1].get("detail", "")[:200])}


def _call_repair_llm(issue: str, error_detail: str, files: List[str]) -> Optional[str]:
    """调用LLM生成修正建议（同步）"""
    try:
        from .core.api_gateway import gateway
        prompt = f"""以下是自动修复中 py_compile 失败的详情。请分析原因并给出修正方案（不超过3条具体修改建议）。

原始问题：{issue[:300]}
验证失败：{error_detail[:500]}
涉及文件：{', '.join(files[:5])}

请用简短中文回答，每条建议一行。"""
        result = gateway.call_simple(
            "你是Python代码审查专家。分析编译错误并给出修正方案。",
            prompt, task="analysis", temperature=0.1, max_tokens=400, timeout=20
        )
        return result
    except Exception as e:
        log(f"[agent] 修正LLM调用失败: {e}")
        return None


# ════════════════════════════════════════════
# Patch 引擎
# ════════════════════════════════════════════

def _record_patch(file: str, original: str, replacement: str,
                  line_start: int = 0, line_end: int = 0):
    """记录一个patch操作"""
    global _PATCH_COUNTER
    _PATCH_COUNTER += 1
    patch = {
        "id": _PATCH_COUNTER,
        "file": file,
        "original": original[:500],
        "replacement": replacement[:500],
        "line_start": line_start,
        "line_end": line_end,
        "time": datetime.now().isoformat(),
    }
    _PATCH_LOG.append(patch)
    # 持久化追加
    try:
        with open(PATCH_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(patch, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"[agent] patch记录失败: {e}")


def _undo_patch(patch_id: int) -> str:
    """反向应用一个patch"""
    for p in _PATCH_LOG:
        if p["id"] == patch_id:
            try:
                path = _safe_path(p["file"])
                if not os.path.isfile(path):
                    return f"[错误] 文件不存在: {p['file']}"
                with open(path, "r", encoding="utf-8") as f:
                    current = f.read()
                # 检查当前内容是否包含 replacement（即patch已应用）
                if p["replacement"] in current:
                    restored = current.replace(p["replacement"], p["original"], 1)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(restored)
                    _PATCH_LOG.remove(p)
                    return f"[成功] 已撤销 patch #{patch_id} → {p['file']}"
                else:
                    return f"[错误] patch #{patch_id} 可能已被覆盖或撤销，当前内容不匹配"
            except Exception as e:
                return f"[错误] 撤销失败: {e}"
    return f"[错误] 未找到 patch #{patch_id}"


def _format_patch_log() -> str:
    """格式化patch日志"""
    if not _PATCH_LOG:
        return "暂无patch记录。"
    lines = [f"共 {len(_PATCH_LOG)} 个patch:"]
    for p in _PATCH_LOG:
        lines.append(
            f"  #{p['id']} {p['file']} L{p['line_start']}-{p['line_end']} "
            f"@{p['time'][:19]}\n"
            f"    -{p['original'][:60]}\n"
            f"    +{p['replacement'][:60]}"
        )
    return "\n".join(lines)


# ════════════════════════════════════════════
# 自动测试选择器
# ════════════════════════════════════════════

def _select_tests_for_files(changed_files: List[str]) -> List[str]:
    """基于调用图和文件关联选择相关测试"""
    tests_dir = os.path.join(PROJECT_DIR, "tests")
    if not os.path.isdir(tests_dir):
        return []
    # 收集所有测试文件
    test_files: List[str] = []
    for f in os.listdir(tests_dir):
        if f.endswith(".py") and f != "__init__.py" and not f.startswith("_"):
            test_files.append(f"tests/{f}")

    if not test_files:
        return []

    # 如果改了测试目录本身 → 跑全量
    if any(f.startswith("tests/") for f in changed_files):
        return test_files

    # 基于调用图匹配
    index = _ensure_project_index()
    call_graph = index.get("call_graph", {})
    symbols = index.get("symbols", {})

    # 从 changed_files 中提取被修改的函数名
    affected_functions: set = set()
    for rel in changed_files:
        if rel in index.get("files", {}):
            item = index["files"][rel]
            for d in item.get("defs", []):
                affected_functions.add(d["name"])

    if not affected_functions:
        # 无法确定受影响函数 → 保守策略：如果有匹配关键词的测试，跑那些
        relevant = []
        module_keywords = set()
        for f in changed_files:
            base = os.path.splitext(os.path.basename(f))[0]
            module_keywords.add(base)
        for tf in test_files:
            tname = os.path.splitext(os.path.basename(tf))[0]
            if any(kw in tname for kw in module_keywords):
                relevant.append(tf)
        return relevant if relevant else test_files[:2]  # 最多2个通用测试

    # 用调用图找调用了受影响函数的测试
    relevant = []
    for tf in test_files:
        tf_rel = tf
        if tf_rel in index.get("files", {}):
            tf_calls = index["files"][tf_rel].get("calls", {})
            # 检查测试文件是否调用了任何受影响函数
            for caller, callees in tf_calls.items():
                if any(af in callees for af in affected_functions):
                    if tf not in relevant:
                        relevant.append(tf)
                    break

    if not relevant:
        # 无直接关联 → 跑烟雾测试
        if "tests/smoke_test.py" in test_files:
            return ["tests/smoke_test.py"]
        return test_files[:1]

    return relevant[:5]


def _run_tests(test_files: List[str]) -> Dict[str, Any]:
    """运行指定测试文件"""
    results: List[Dict[str, Any]] = []
    overall_ok = True
    for tf in test_files:
        path = os.path.join(PROJECT_DIR, tf)
        if not os.path.isfile(path):
            results.append({"file": tf, "ok": False, "detail": "文件不存在"})
            overall_ok = False
            continue
        try:
            r = subprocess.run(
                ["python", path],
                capture_output=True, text=True, timeout=30,
                cwd=PROJECT_DIR, encoding="utf-8", errors="replace"
            )
            ok = r.returncode == 0
            detail = (r.stdout[-500:] + r.stderr[-200:]).strip()
            results.append({"file": tf, "ok": ok, "detail": detail or "(无输出)"})
            if not ok:
                overall_ok = False
        except subprocess.TimeoutExpired:
            results.append({"file": tf, "ok": False, "detail": "超时(30s)"})
            overall_ok = False
        except Exception as e:
            results.append({"file": tf, "ok": False, "detail": str(e)})
            overall_ok = False
    return {"ok": overall_ok, "results": results}


def _execute_fix_issue(issue: str) -> str:
    targets = _locate_fix_targets(issue)
    if not targets:
        return f"[错误] 没有定位到相关 Python 文件。问题描述：{issue}"

    report: List[str] = [f"定位文件：{', '.join(targets)}"]

    # ══════ 第0步：所有目标文件保存快照 ══════
    snapshots: Dict[str, str] = {}
    for rel in targets:
        p = _safe_path(rel)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    snapshots[rel] = f.read()
            except Exception as e:
                report.append(f"[{rel}] 快照失败: {e}")
                continue

    if not snapshots:
        return "[错误] 无法读取任何目标文件\n" + "\n".join(report)

    # ══════ 第1步：逐文件修改 + 即时 py_compile ══════
    changed_files: List[str] = []
    rolled_back: List[str] = []
    rollback_reasons: Dict[str, str] = {}

    for rel in targets:
        p = _safe_path(rel)
        if not os.path.isfile(p) or rel not in snapshots:
            continue
        try:
            old = snapshots[rel]
            new, notes = _apply_safe_fixes_to_text(rel, old, issue)
            review = _execute_review_code(rel, issue)
            report.append(f"\n[{rel}] 预审：\n{review[:1200]}")
            if new == old:
                report.append(f"[{rel}] 未发现可安全自动应用的修改。")
                continue

            # 写入
            with open(p, "w", encoding="utf-8") as f:
                f.write(new)
            report.append(f"[{rel}] 已应用：{'; '.join(notes)}")

            # 即时 py_compile
            if p.endswith(".py"):
                ok, detail = _verify_python_file(p)
                if not ok:
                    # 该文件回滚
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(snapshots[rel])
                    rolled_back.append(rel)
                    rollback_reasons[rel] = detail
                    report.append(f"[{rel}] ⚠ py_compile失败已回滚：{detail}")
                    continue

            changed_files.append(rel)
        except Exception as e:
            # 异常 → 该文件回滚
            try:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(snapshots[rel])
            except Exception:
                pass
            rolled_back.append(rel)
            rollback_reasons[rel] = str(e)
            report.append(f"[{rel}] ⚠ 修改异常已回滚：{e}")

    if not changed_files:
        detail = ""
        if rolled_back:
            detail = f"\n回滚文件({len(rolled_back)})：{', '.join(rolled_back)}\n原因：{'; '.join(f'{k}: {v}' for k, v in rollback_reasons.items())}"
        return "未自动修改文件。" + detail + "\n" + "\n".join(report)

    # ══════ 第2步：统一验证，任一失败 → 全部回滚 ══════
    plan = build_validation_plan(changed_files)
    val_result = run_validation(plan)
    if val_result["ok"]:
        _record_change(issue, snapshots, changed_files, rolled_back, rollback_reasons, val_result, None)
        post = "\n".join(_execute_review_code(rel, "post-fix")[:800] for rel in changed_files)
        rollback_note = ""
        if rolled_back:
            rollback_note = f"\n⚠ 部分文件因即时py_compile失败已回滚({len(rolled_back)})：{', '.join(rolled_back)}"
        return f"[成功] 已修改 {len(changed_files)} 个文件：{', '.join(changed_files)}{rollback_note}\n验证通过\n\n复查摘要：\n{post}"

    # 验证失败 → 自动修正循环
    repair = validation_repair_loop(issue, changed_files)
    if repair["ok"]:
        _record_change(issue, snapshots, changed_files, rolled_back, rollback_reasons, val_result, repair.get("log", []))
        post = "\n".join(_execute_review_code(rel, "post-fix")[:800] for rel in changed_files)
        return f"[成功] 已修改 {len(changed_files)} 个文件：{', '.join(changed_files)}\n{repair['detail']}\n\n复查摘要：\n{post}"

    # ══════ 第3步：修正无效 → 全部回滚到快照 ══════
    restored: List[str] = []
    restore_fails: List[str] = []
    for rel in snapshots:
        try:
            with open(_safe_path(rel), "w", encoding="utf-8") as f:
                f.write(snapshots[rel])
            restored.append(rel)
        except Exception as e:
            restore_fails.append(f"{rel}: {e}")

    _record_change(issue, snapshots, [], rolled_back, rollback_reasons, val_result, repair.get("log", []))
    rollback_detail = f"验证失败且{repair['rounds']}轮自动修正无效：{repair['detail']}"
    rollback_detail += f"\n全部回滚：已恢复 {len(restored)} 个文件 ({', '.join(restored)})"
    if rolled_back:
        rollback_detail += f"\n即时回滚({len(rolled_back)})：{', '.join(rolled_back)}"
        rollback_detail += f"\n即时回滚原因：{'; '.join(f'{k}: {v[:60]}' for k, v in rollback_reasons.items())}"
    if restore_fails:
        rollback_detail += f"\n回滚失败：{'; '.join(restore_fails)}"
    return f"[回滚] {rollback_detail}"


# ════════════════════════════════════════════
# 变更追踪 /d diff /d changed
# ════════════════════════════════════════════

def _record_change(issue: str, snapshots: Dict[str, str],
                   changed_files: List[str], rolled_back: List[str],
                   rollback_reasons: Dict[str, str],
                   val_result: Optional[Dict[str, Any]],
                   repair_log: Optional[List[Dict[str, Any]]]):
    """记录最近一次 /d fix 的完整变更"""
    _CHANGE_LOG.clear()
    _CHANGE_LOG.update({
        "issue": issue,
        "targets": list(snapshots.keys()),
        "snapshots": dict(snapshots),
        "changed": list(changed_files),
        "rolled_back": list(rolled_back),
        "rollback_reasons": dict(rollback_reasons),
        "validation": val_result,
        "repair_log": repair_log or [],
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


def _generate_unified_diff(old_text: str, new_text: str, filename: str) -> str:
    """生成简易 unified diff"""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    result = [f"--- a/{filename}", f"+++ b/{filename}"]
    # 简单行级diff
    import difflib
    diff = list(difflib.unified_diff(
        [l.rstrip('\n') for l in old_lines],
        [l.rstrip('\n') for l in new_lines],
        fromfile=f"a/{filename}", tofile=f"b/{filename}",
        lineterm=""
    ))
    if diff:
        result.extend(diff)
    else:
        result.append(" (无变化)")
    return "\n".join(result[:80])


# ════════════════════════════════════════════
# 任务状态机 /d status /d tasks
# ════════════════════════════════════════════

def _load_task_state():
    """从JSON文件恢复任务状态"""
    global _TASK_STATE, _TASK_LIST
    try:
        if os.path.exists(TASK_STATE_FILE):
            with open(TASK_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                _TASK_STATE = data.get("current", {})
                _TASK_LIST = data.get("list", [])
    except Exception as e:
        log(f"[agent] 加载任务状态失败: {e}")


def _save_task_state():
    """持久化任务状态到JSON"""
    try:
        tmp = TASK_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"current": _TASK_STATE, "list": _TASK_LIST}, f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, TASK_STATE_FILE)
    except Exception as e:
        log(f"[agent] 保存任务状态失败: {e}")


def _archive_task(task: Dict[str, Any]):
    """任务完成后归档到 task_history.jsonl"""
    try:
        entry = dict(task)
        entry["archived_at"] = datetime.now().isoformat()
        with open(TASK_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"[agent] 归档任务失败: {e}")


def _estimate_task_priority(description: str, files: List[str]) -> str:
    text = description.lower()
    if any(k in text for k in ("crash", "fatal", "security", "leak", "钥", "崩", "致命", "安全")):
        return "P0"
    if len(files) >= 5 or any(k in text for k in ("全部", "批量", "回归", "阻塞")):
        return "P1"
    if files:
        return "P2"
    return "P3"


def _build_autonomous_plan(description: str, files: List[str] = None) -> List[Dict[str, Any]]:
    files = files or []
    steps: List[Dict[str, Any]] = [
        {"id": "scope", "title": "定位范围", "status": "pending",
         "detail": f"确认问题描述和候选文件: {', '.join(files) if files else '待索引定位'}"},
    ]
    for rel in files[:12]:
        steps.append({"id": f"edit:{rel}", "title": f"处理 {rel}",
                      "status": "pending", "detail": "审查、应用安全修改、即时 py_compile"})
    steps.append({"id": "validate", "title": "验证", "status": "pending",
                  "detail": "按变更文件生成验证计划并执行"})
    steps.append({"id": "related", "title": "关联影响", "status": "pending",
                  "detail": "基于索引提示调用方、导入方和重复代码"})
    steps.append({"id": "report", "title": "汇报", "status": "pending",
                  "detail": "输出变更、验证、后续建议"})
    return steps


def _format_autonomous_plan(description: str, files: List[str] = None) -> str:
    files = files or _locate_fix_targets(description)
    priority = _estimate_task_priority(description, files)
    steps = _build_autonomous_plan(description, files)
    lines = [f"自主任务编排: {description}", f"优先级: {priority}",
             f"候选文件: {', '.join(files) if files else '(待定位)'}"]
    for i, step in enumerate(steps, 1):
        lines.append(f"{i}. [{step['status']}] {step['title']} - {step['detail']}")
    lines.append("确认执行可用 /d fix <问题>；当前命令只生成计划。")
    return "\n".join(lines)


TEAM_ROLE_PROMPTS = {
    "Planner": "你是 Planner，只负责理解任务、拆解步骤、判断优先级和风险，不直接改代码。",
    "Executor": "你是 Executor，按计划调用工具、修改代码、运行验证，输出执行证据。",
    "Reviewer": "你是 Reviewer，审查 Executor 的改动、验证结果和残余风险；发现问题就打回。",
}


def _load_team_state() -> Dict[str, Any]:
    global _TEAM_STATE
    if _TEAM_STATE:
        return _TEAM_STATE
    try:
        if os.path.exists(TEAM_STATE_FILE):
            with open(TEAM_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _TEAM_STATE = data
                _TEAM_STATE.setdefault("mode", "solo")
                _TEAM_STATE.setdefault("session", [])
                _TEAM_STATE.setdefault("roles", TEAM_ROLE_PROMPTS)
                return _TEAM_STATE
    except Exception as e:
        log(f"[agent] team state load failed: {e}")
    _TEAM_STATE = {"mode": "solo", "roles": TEAM_ROLE_PROMPTS, "session": []}
    return _TEAM_STATE


def _save_team_state():
    try:
        state = _load_team_state()
        tmp = TEAM_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TEAM_STATE_FILE)
    except Exception as e:
        log(f"[agent] team state save failed: {e}")


def _team_mode() -> str:
    return _load_team_state().get("mode", "solo")


def _set_team_mode(mode: str) -> str:
    state = _load_team_state()
    state["mode"] = mode
    state["updated"] = datetime.now().isoformat()
    _save_team_state()
    return "多Agent协作模式已开启。" if mode == "team" else "单Agent兼容模式已开启。"


def _append_team_session(role: str, content: str):
    state = _load_team_state()
    state.setdefault("session", []).append({
        "time": datetime.now().isoformat(),
        "role": role,
        "system": state.get("roles", {}).get(role, ""),
        "content": content[:4000],
    })
    state["session"] = state["session"][-60:]
    state["updated"] = datetime.now().isoformat()
    _save_team_state()


def _is_team_exempt(low: str) -> bool:
    prefixes = (
        "solo", "team", "status", "tasks", "done", "diff", "changed", "patterns",
        "snippet", "progress", "cost", "git", "audit", "sandbox", "log", "report",
        "permit", "forbid", "context", "new", "pref", "read", "search_code",
    )
    return any(low == p or low.startswith(p + " ") for p in prefixes)


def _team_plan_for_command(text: str) -> Dict[str, Any]:
    targets = _locate_fix_targets(text)
    steps = _build_autonomous_plan(text, targets)
    priority = _estimate_task_priority(text, targets)
    risk = "high" if priority in {"P0", "P1"} or len(targets) >= 5 else "normal"
    return {"task": text, "priority": priority, "risk": risk,
            "targets": targets, "steps": steps}


def _format_team_plan(plan: Dict[str, Any]) -> str:
    lines = [f"Planner计划: {plan.get('task')}",
             f"优先级: {plan.get('priority')} | 风险: {plan.get('risk')}",
             f"目标: {', '.join(plan.get('targets', [])) or '(待执行器定位)'}"]
    for i, step in enumerate(plan.get("steps", [])[:12], 1):
        lines.append(f"{i}. {step.get('title')} - {step.get('detail')}")
    return "\n".join(lines)


def _review_team_result(task: str, result: str) -> Dict[str, Any]:
    changed = list(_CHANGE_LOG.get("changed", []))
    ok = "[错误]" not in result and "[回滚]" not in result and "[閿欒]" not in result and "[鍥炴粴]" not in result
    detail = "结果文本未显示错误或回滚。"
    if changed:
        val = run_validation(build_validation_plan(changed))
        ok = ok and bool(val.get("ok"))
        detail = "验证通过。" if val.get("ok") else f"验证失败: {val.get('failed_step')}"
    elif task.lower().startswith(("fix ", "修复 ")):
        ok = ok and "[成功]" in result
        detail = "fix 未产生变更记录。" if not changed else detail
    return {"ok": ok, "changed": changed, "detail": detail}


def _execute_team_command(text: str, uid: int) -> str:
    plan = _team_plan_for_command(text)
    plan_text = _format_team_plan(plan)
    _append_team_session("Planner", plan_text)
    executor_result = _handle_direct_agent_command(text, uid, _team_internal=True) or "Executor未匹配到可执行命令。"
    _append_team_session("Executor", executor_result)
    review = _review_team_result(text, executor_result)
    review_text = f"Reviewer审查: {'通过' if review['ok'] else '打回'}；{review['detail']}"
    _append_team_session("Reviewer", review_text)
    if not review["ok"] and review.get("changed"):
        repair = validation_repair_loop(text, review["changed"])
        repair_text = f"Executor返修: {repair.get('detail', repair)}"
        _append_team_session("Executor", repair_text)
        review = _review_team_result(text, executor_result + "\n" + repair_text)
        review_text = f"Reviewer复审: {'通过' if review['ok'] else '仍需人工确认'}；{review['detail']}"
        _append_team_session("Reviewer", review_text)
    return "\n\n".join([plan_text, "Executor执行:\n" + executor_result, review_text])


def _new_task(description: str, files: List[str] = None) -> Dict[str, Any]:
    """创建一个结构化任务"""
    plan_steps = []
    for i, f in enumerate(files or []):
        plan_steps.append(f"修改 {f}")
    orchestration = _build_autonomous_plan(description, files)
    task = {
        "description": description,
        "plan": plan_steps,
        "orchestration": orchestration,
        "priority": _estimate_task_priority(description, files or []),
        "current_step": 0,
        "files_touched": files or [],
        "validation": None,
        "blockers": [],
        "next_action": "执行第1步" if plan_steps else "分析问题",
        "status": "pending",
        "created": datetime.now().isoformat(),
        "updated": datetime.now().isoformat(),
    }
    _TASK_LIST.append(task)
    _TASK_STATE.update(task)
    _save_task_state()
    return task


def _update_task(**kwargs):
    """更新当前任务状态"""
    _TASK_STATE.update(kwargs)
    _TASK_STATE["updated"] = datetime.now().isoformat()
    # 同步回 _TASK_LIST
    for i, t in enumerate(_TASK_LIST):
        if t.get("created") == _TASK_STATE.get("created"):
            _TASK_LIST[i] = dict(_TASK_STATE)
            break
    _save_task_state()


def _finish_task(status: str = "done"):
    """结束当前任务并归档"""
    _TASK_STATE["status"] = status
    _TASK_STATE["updated"] = datetime.now().isoformat()
    _archive_task(dict(_TASK_STATE))
    # 从列表中移除
    created = _TASK_STATE.get("created")
    _TASK_LIST[:] = [t for t in _TASK_LIST if t.get("created") != created]
    _TASK_STATE.clear()
    _save_task_state()


def _format_task_status() -> str:
    """格式化当前任务状态为可读文本"""
    if not _TASK_STATE:
        return "暂无活跃任务。用 /d fix <问题> 开始新任务。"
    t = _TASK_STATE
    lines = [
        f"📋 任务: {t.get('description', '(无描述)')}",
        f"📌 状态: {t.get('status', 'unknown')}",
        f"📅 创建: {t.get('created', '?')}",
        f"🔄 更新: {t.get('updated', '?')}",
    ]
    plan = t.get("plan", [])
    if plan:
        cur = t.get("current_step", 0)
        lines.append(f"📝 步骤 ({cur}/{len(plan)}):")
        for i, s in enumerate(plan):
            mark = "→" if i == cur else ("✓" if i < cur else "·")
            lines.append(f"   {mark} {s}")
    orchestration = t.get("orchestration", [])
    if orchestration:
        lines.append(f"编排优先级: {t.get('priority', 'P?')}")
        for i, step in enumerate(orchestration[:8], 1):
            lines.append(f"   {i}. [{step.get('status', 'pending')}] {step.get('title', '?')}")
    lines.append(f"📁 涉及文件: {', '.join(t.get('files_touched', [])) or '(无)'}")
    if t.get("blockers"):
        lines.append(f"🚫 阻塞: {', '.join(t['blockers'])}")
    lines.append(f"▶ 下一步: {t.get('next_action', '?')}")
    if t.get("validation"):
        v = t["validation"]
        lines.append(f"✅ 验证: {'通过' if v.get('ok') else '失败 — ' + str(v.get('failed_step', '?'))}")
    return "\n".join(lines)


def _format_task_list() -> str:
    """列出所有任务"""
    if not _TASK_LIST:
        return "任务列表为空。"
    lines = [f"共 {len(_TASK_LIST)} 个任务:"]
    for i, t in enumerate(_TASK_LIST):
        status_icon = {"pending": "⏳", "in_progress": "🔄", "paused": "⏸",
                       "done": "✅", "failed": "❌"}.get(t.get("status", ""), "·")
        lines.append(f"  {i+1}. {status_icon} {t.get('description', '?')[:60]} "
                     f"[{t.get('status', '?')}] {t.get('created', '')[:10]}")
    return "\n".join(lines)


# ════════════════════════════════════════════
# 强化 /d review — 重复代码检测
# ════════════════════════════════════════════

def _detect_similar_functions(tree: ast.AST, source_lines: List[str],
                               findings: List[Dict[str, Any]]):
    """检测同文件内相似函数体（>70%行重合）"""
    import hashlib
    funcs: List[Tuple[str, int, List[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_lines = []
            for stmt in node.body:
                if hasattr(stmt, 'lineno'):
                    lo = stmt.lineno - 1
                    hi = (stmt.end_lineno or stmt.lineno) - 1
                    for li in range(lo, min(hi + 1, len(source_lines))):
                        stripped = source_lines[li].strip()
                        if stripped and not stripped.startswith('#'):
                            body_lines.append(stripped)
            funcs.append((node.name, node.lineno, body_lines))

    for i in range(len(funcs)):
        for j in range(i + 1, len(funcs)):
            lines_i = funcs[i][2]
            lines_j = funcs[j][2]
            if len(lines_i) < 5 or len(lines_j) < 5:
                continue
            common = sum(1 for l in lines_i if l in lines_j)
            ratio = common / max(len(lines_i), len(lines_j))
            if ratio > 0.7:
                _add_finding(
                    findings, funcs[j][1], "低", "疑似重复代码",
                    f"函数 '{funcs[i][0]}' (L{funcs[i][1]}) 和 '{funcs[j][0]}' (L{funcs[j][1]}) "
                    f"体相似度 {int(ratio*100)}%，建议抽公共逻辑。",
                    f"提取公共函数，两个函数调用它。",
                )


def _execute_search_code(pattern: str, path: str = "", file_type: str = "") -> str:
    try:
        search_root = _safe_path(path) if path else PROJECT_DIR
    except ValueError as e:
        return f"[错误] {e}"

    # 优先 rg
    for tool in ["rg", "grep"]:
        try:
            cmd = [tool, "--no-heading", "--line-number", "--color", "never",
                   "--max-count", "50", "--max-filesize", "1M"]
            if tool == "grep":
                cmd = ["grep", "-rn", "--color=never"]
            if file_type and tool == "rg":
                cmd.insert(-5, "--type")
                cmd.insert(-5, file_type)
            if file_type and tool == "grep":
                cmd.insert(2, f"--include=*.{file_type}")
            cmd.extend(["--", pattern, search_root] if tool == "rg" else [pattern, search_root])

            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=15, encoding="utf-8", errors="replace")
            out = r.stdout.strip()
            if not out:
                return f"未找到匹配 '{pattern}'"
            if len(out) > 10000:
                out = out[:10000] + "\n... (截断)"
            return out
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return "[错误] 搜索超时"
        except Exception as e:
            return f"[错误] 搜索失败: {e}"
    return "[错误] 未安装 rg 或 grep"

_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

def _execute_run_bash(command: str, timeout: int = 30) -> str:
    allowed, reason = _is_bash_allowed(command)
    if not allowed:
        return f"[拒绝] {reason}"

    try:
        args = shlex.split(command)
    except ValueError as e:
        return f"[错误] 命令解析失败: {e}"

    exit_code = 0
    try:
        if "|" in command:
            segments = [s.strip() for s in command.split("|")]
            prev_out = None
            last_result = None
            for seg in segments:
                ok, why = _is_bash_allowed(seg)
                if not ok:
                    return f"[拒绝] 管道段 '{seg}': {why}"
                try:
                    seg_args = shlex.split(seg)
                except ValueError:
                    seg_args = seg.split()
                last_result = subprocess.run(
                    seg_args, input=prev_out, capture_output=True, text=True,
                    timeout=timeout, cwd=PROJECT_DIR, encoding="utf-8", errors="replace",
                    creationflags=_SUBPROCESS_FLAGS)
                prev_out = last_result.stdout
            if last_result is None:
                return "(无输出)"
            out = last_result.stdout.strip()
            if last_result.stderr.strip():
                out += f"\n[stderr] {last_result.stderr.strip()[:1000]}"
            exit_code = last_result.returncode
        else:
            r = subprocess.run(args, capture_output=True, text=True,
                               timeout=timeout, cwd=PROJECT_DIR, encoding="utf-8", errors="replace",
                               creationflags=_SUBPROCESS_FLAGS)
            out = r.stdout.strip()
            if r.stderr.strip():
                out += f"\n[stderr] {r.stderr.strip()[:1000]}"
            exit_code = r.returncode

        if exit_code != 0:
            out += f"\n[退出码: {exit_code}]"
    except subprocess.TimeoutExpired:
        return f"[错误] 命令执行超时({timeout}秒)，进程已终止"

    if len(out) > 10000:
        out = out[:10000] + "\n... (截断)"
    log(f"[agent] bash: {command[:80]} (exit={exit_code})")
    return out if out else "(无输出)"

def _execute_search_memory(keywords: str) -> str:
    try:
        from .memory import retrieve_relevant_memories
        result = retrieve_relevant_memories(MASTER_UID, keywords, top_k=10)
        return result if result and result.strip() else f"未找到与 '{keywords}' 相关的记忆"
    except Exception as e:
        return f"[错误] 记忆搜索失败: {e}"

def _execute_list_directory(path: str = "") -> str:
    try:
        target = _safe_path(path) if path else PROJECT_DIR
    except ValueError as e:
        return f"[错误] {e}"
    if not os.path.isdir(target):
        return f"[错误] 不是目录: {path}"
    try:
        entries = sorted(os.listdir(target))
        dirs, files = [], []
        for e in entries:
            if e.startswith(".") or e == "__pycache__":
                continue
            full = os.path.join(target, e)
            if os.path.isdir(full):
                dirs.append(f"  {e}/")
            else:
                sz = os.path.getsize(full)
                sz_s = f"{sz/1024/1024:.1f}MB" if sz > 1024*1024 else f"{sz/1024:.1f}KB" if sz > 1024 else f"{sz}B"
                files.append(f"  {e} ({sz_s})")
        r = f"目录: {path or '/'}\n"
        if dirs:
            r += f"子目录({len(dirs)}):\n" + "\n".join(dirs) + "\n"
        if files:
            r += f"文件({len(files)}):\n" + "\n".join(files)
        return r if dirs or files else r + "(空目录)"
    except Exception as e:
        return f"[错误] 列出目录失败: {e}"

# ════════════════════════════════════════════
# P0: 可靠桌面操作基元 (GLM方案)
# ════════════════════════════════════════════

def _safe_type(text: str) -> bool:
    """剪贴板输入——绕过中文输入法问题"""
    try:
        import pyperclip, pyautogui
        pyperclip.copy(text)
        import time
        time.sleep(0.05)
        pyautogui.hotkey('ctrl', 'v')
        return True
    except Exception:
        return False

def _focus_window(title: str) -> bool:
    """复用tools.py的可靠窗口激活(AttachThreadInput)"""
    try:
        from .tools import _find_hwnd_by_title, _force_foreground
        hwnd = _find_hwnd_by_title(title)
        if hwnd:
            return _force_foreground(hwnd)
        return False
    except Exception:
        return False

def _verify_change(before_path: str, after_path: str, threshold: float = 0.02) -> bool:
    """快速检查操作是否产生了屏幕变化"""
    try:
        from PIL import Image
        import numpy as np
        before = np.array(Image.open(before_path), dtype=float)
        after = np.array(Image.open(after_path), dtype=float)
        diff = np.abs(before - after)
        return np.mean(diff > 30) > threshold
    except Exception:
        return True  # 无法验证时假定成功

def _execute_computer_control(action: str, **kwargs) -> str:
    """封装冬的桌面控制 + GUI Agent智能定位"""
    uid = MASTER_UID

    # ── 智能截图 + 屏幕分析 ──
    if action == "screenshot":
        try:
            # 先截图
            from .tools import _tool_computer_control
            result = _tool_computer_control({"action": "screenshot"}, uid)

            # 尝试 SoM 标注（GUI Agent）
            try:
                from .gui_agent import generate_som_screenshot, generate_ui_tree_and_screenshot
                som_path, elements = generate_som_screenshot()
                if som_path and elements:
                    # 构建元素清单
                    elems_text = []
                    for el in elements[:20]:
                        name = el.get('name', '?')[:30]
                        etype = el.get('type', '?')
                        center = el.get('center', (0, 0))
                        eid = el.get('id', '?')
                        elems_text.append(f"#{eid} {etype} \"{name}\" @({center[0]},{center[1]})")

                    result += "\n\n═══ 屏幕可交互元素 ═══\n"
                    result += "使用 computer_control(action=click_element, target=元素名称) 点击\n"
                    result += "使用 computer_control(action=analyze_screen) 重新分析\n\n"
                    result += "\n".join(elems_text[:15])
                    result += f"\n\n(共{len(elements)}个元素，SoM标注图: {som_path})"
            except Exception:
                pass
            # 追加VLM视觉描述（对微信等非UIA应用至关重要）
            try:
                from .gui_agent import analyze_screen
                vlm = analyze_screen()
                if vlm:
                    result += "\n\n═══ VLM视觉描述 ═══\n" + vlm[:1200]
            except Exception:
                pass
            return result
        except Exception as e:
            return f"[错误] 截图失败: {e}"

    # ─� 智能点击：按名称/类型定位 ──
    elif action == "click_element":
        name = kwargs.get("name", kwargs.get("target", ""))
        if not name:
            return "[错误] 请提供元素名称，如 action=click_element,name=登录"
        try:
            from .gui_agent import find_element, click_element
            el = find_element(name=name)
            if el:
                # 用UIA精确点击
                result = click_element(name=name)
                return f"[成功] 已点击元素 \"{name}\" — {result}"
            else:
                # 回退到OCR文字定位
                from .gui_agent import find_text_on_screen
                found = find_text_on_screen(name)
                if found:
                    cx, cy = int(found['center'][0]), int(found['center'][1])
                    from .tools import _tool_computer_control
                    return _tool_computer_control({"action": "click", "x": str(cx), "y": str(cy)}, uid)
                return f"[失败] 屏幕上未找到 \"{name}\"，请先 screenshot 查看当前界面"
        except Exception as e:
            return f"[错误] 智能点击失败: {e}"

    # ── 屏幕分析（文本描述）──
    elif action == "analyze_screen":
        try:
            from .gui_agent import analyze_screen
            return analyze_screen()
        except Exception as e:
            return f"[错误] 屏幕分析失败: {e}"

    # ── 智能输入：先找输入框再键入 ──
    elif action == "type_in_field":
        text = kwargs.get("text", "")
        field_name = kwargs.get("field", kwargs.get("name", ""))
        if not text:
            return "[错误] 请提供输入文本"
        if field_name:
            try:
                from .gui_agent import find_element, click_element
                el = find_element(name=field_name, elem_type="edit")
                if el:
                    click_element(name=field_name)
                    import time
                    time.sleep(0.3)
            except Exception:
                pass
        if _safe_type(text):
            return f"[成功] 已粘贴输入: {text[:30]}"
        try:
            from .tools import _tool_computer_control
            return _tool_computer_control({"action": "type", "text": text, "window": field_name}, uid)
        except Exception as e:
            return f"[错误] 输入失败: {e}"

    # ── 其他操作走冬的原始工具 ──
    elif action in ("click", "move", "press", "scroll", "launch",
                     "double_click", "drag"):
        try:
            from .tools import _tool_computer_control
            params = {"action": action}
            for k, v in kwargs.items():
                if v is not None:
                    if action == "launch" and k == "window":
                        params["app"] = str(v)
                    elif action == "launch" and k in ("app", "name"):
                        params["app"] = str(v)
                    else:
                        params[k] = str(v)
            return _tool_computer_control(params, uid)
        except Exception as e:
            return f"[错误] 桌面操作失败: {e}"

    # ── type: 优先剪贴板 ──
    elif action == "type":
        text = kwargs.get("text", "")
        if not text:
            return "[错误] 请提供输入文本"
        if _safe_type(text):
            return f"[成功] 已粘贴: {text[:30]}"
        try:
            from .tools import _tool_computer_control
            return _tool_computer_control({"action": "type", "text": text}, uid)
        except Exception as e:
            return f"[错误] 输入失败: {e}"

    # ── activate_window: Win32优先 ──
    elif action == "activate_window":
        name = kwargs.get("name", kwargs.get("window", kwargs.get("title", "")))
        if name and _focus_window(name):
            return f"[成功] 已激活窗口: {name}"
        try:
            from .tools import _tool_computer_control
            return _tool_computer_control({"action": "activate_window", "name": name}, uid)
        except Exception as e:
            return f"[错误] 激活窗口失败: {e}"

    else:
        return f"[错误] 未知操作: {action}。可用: screenshot, analyze_screen, click_element, type_in_field, click, move, type, press, scroll, launch"

def _execute_search_online(query: str) -> str:
    """三路搜索：DuckDuckGo + GitHub仓库 + PyPI包"""
    import requests as _req
    lines = []

    # 1. DuckDuckGo（快速摘要）
    try:
        from .tools import _tool_web_search
        ddg = _tool_web_search({"query": query}, MASTER_UID)
        if ddg and "失败" not in ddg and "错误" not in ddg:
            lines.append(f"【网页搜索】{ddg[:600]}")
    except Exception:
        pass

    # 2. GitHub仓库搜索（按star排序）
    try:
        r = _req.get("https://api.github.com/search/repositories",
                     params={"q": f"{query} language:python", "sort": "stars", "per_page": 3},
                     headers={"Accept": "application/vnd.github+json", "User-Agent": "dong-agent"},
                     timeout=10)
        if r.status_code == 200:
            repos = r.json().get("items", [])
            if repos:
                lines.append(f"\n【GitHub仓库({len(repos)}个)】")
                for repo in repos:
                    lines.append(f"  ★{repo['stargazers_count']} {repo['full_name']}")
                    lines.append(f"    {repo.get('description','')[:100]}")
                    lines.append(f"    {repo['html_url']}")
    except Exception:
        pass

    # 3. PyPI搜索（包名查询）
    try:
        pkg_name = query.strip().replace(" ", "-").lower().split()[0]
        r = _req.get(f"https://pypi.org/pypi/{pkg_name}/json", timeout=8,
                     headers={"User-Agent": "dong-agent"})
        if r.status_code == 200:
            pkg = r.json()
            info = pkg.get("info", {})
            lines.append(f"\n【PyPI包】{pkg_name} v{info.get('version','?')}")
            lines.append(f"  {info.get('summary','')[:150]}")
            lines.append(f"  pip install {pkg_name}")
    except Exception:
        pass

    return "\n".join(lines) if lines else f"未找到 '{query}' 的相关结果"

# ════════════════════════════════════════════════════
# 动态工具沙箱 —— 白名单 + subprocess隔离 (Windows版)
# ════════════════════════════════════════════════════

_DYNAMIC_TOOLS_FILE = os.path.join(PROJECT_DIR, "dynamic_tools.json")

# ── 白名单模块 ──
_ALLOWED_MODULES = frozenset({
    "json", "math", "re", "time", "datetime", "collections",
    "itertools", "functools", "operator", "hashlib", "base64",
    "urllib.parse", "typing", "enum", "dataclasses",
    "decimal", "fractions", "statistics", "copy",
    "requests", "httpx", "urllib.request",
    "csv", "xml.etree.ElementTree", "html.parser",
})
_ALLOWED_BUILTINS = frozenset({
    "print", "len", "str", "int", "float", "bool", "list", "dict",
    "tuple", "set", "range", "enumerate", "zip", "map", "filter",
    "sorted", "reversed", "min", "max", "sum", "abs", "round",
    "isinstance", "type", "hasattr", "getattr", "setattr",
    "repr", "chr", "ord", "hex", "oct", "bin", "format",
    "ValueError", "TypeError", "KeyError", "IndexError",
    "AttributeError", "RuntimeError", "StopIteration", "Exception",
    "exec",  # runner需要，用户代码静态检查已拦截
})
_FORBIDDEN_PATTERNS = [
    "__import__", "eval(", "exec(", "compile(",
    "getattr(__builtins__", "globals()", "locals()",
    "os.system", "os.exec", "os.spawn", "os.popen",
    "os.remove", "os.unlink", "os.rmdir", "os.mkdir",
    "subprocess", "shutil", "ctypes", "winreg",
    "socket.socket", "open(",
    "__class__", "__subclasses__", "__bases__",
    "__code__", "__globals__", "__dict__",
    "signal.signal", "threading.Thread", "multiprocessing",
]

def _static_check(code: str, extra_modules: set = None) -> tuple:
    allowed = _ALLOWED_MODULES | (extra_modules or set())
    for pattern in _FORBIDDEN_PATTERNS:
        if pattern in code:
            return False, f"包含禁止模式: {pattern}"
    import re
    for mod in re.findall(r'(?:import|from)\s+([a-zA-Z_]\w*)', code):
        if mod not in allowed:
            return False, f"导入未授权模块: {mod}"
    if "chr(" in code and "+" in code and code.count("chr(") >= 3:
        return False, "疑似字符串拼接绕过"
    if "\\" in code and any(x in code for x in ["\\x", "\\u", "\\o"]):
        return False, "疑似编码绕过"
    return True, ""

_SANDBOX_RUNNER = r'''
import json, sys
try:
    _input = json.loads(sys.stdin.read())
    kwargs = _input["kwargs"]
    _allowed_builtins = _input["builtins"]
except Exception as e:
    print(json.dumps({"__error__": f"参数读取失败: {e}"}))
    sys.exit(1)

# 保存原始的 __import__（exec需要它来import代码中的模块）
_real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

# 替换 builtins 为白名单
_real_builtins = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
_safe = {k: _real_builtins[k] for k in _allowed_builtins if k in _real_builtins}
_safe["__name__"] = "__main__"
_safe["__doc__"] = None
_safe["True"] = True
_safe["False"] = False
_safe["None"] = None
_safe["__import__"] = _real_import  # 保留import能力（静态检查已过滤）
_safe["ImportError"] = ImportError
_safe["ModuleNotFoundError"] = ModuleNotFoundError
if isinstance(__builtins__, dict):
    __builtins__.clear()
    __builtins__.update(_safe)
else:
    import builtins as _bi
    _bi.__dict__.clear()
    _bi.__dict__.update(_safe)

# 执行用户代码
_user_code = _input["code"]
_result_namespace = {}
try:
    exec(_user_code, _result_namespace)
except Exception as e:
    print(json.dumps({"__error__": f"执行出错: {e}"}, ensure_ascii=False))
    sys.exit(0)

# 输出结果
if "__result__" in _result_namespace:
    _r = _result_namespace["__result__"]
    try:
        json.dumps(_r)
        print(json.dumps({"__result__": _r}, ensure_ascii=False))
    except (TypeError, ValueError):
        print(json.dumps({"__result__": str(_r)}, ensure_ascii=False))
else:
    print(json.dumps({"__result__": "(执行完成，无返回值)"}))
'''

def _sandbox_exec(code: str, kwargs: dict, extra_modules: set = None,
                  timeout: int = 30) -> str:
    tmp_dir = tempfile.gettempdir()
    runner_path = os.path.join(tmp_dir, f"dong_sandbox_{int(time.time()*1000)}.py")
    all_allowed_imports = set(_ALLOWED_MODULES) | (extra_modules or set())
    stdin_data = json.dumps({
        "code": code,
        "kwargs": kwargs,
        "builtins": list(_ALLOWED_BUILTINS),
        "allowed_imports": list(all_allowed_imports),
    }, ensure_ascii=False)
    try:
        with open(runner_path, "w", encoding="utf-8") as f:
            f.write(_SANDBOX_RUNNER)
        result = subprocess.run(
            ["python", "-u", runner_path],
            input=stdin_data, capture_output=True, text=True,
            timeout=timeout, cwd=PROJECT_DIR,
            encoding="utf-8", errors="replace",
            creationflags=_SUBPROCESS_FLAGS,
            env={
                "PATH": os.environ.get("PATH", ""), "TEMP": tmp_dir,
                "PYTHONIOENCODING": "utf-8", "PYTHONPATH": PROJECT_DIR,
                "PYTHONLEGACYWINDOWSSTDIO": "1",
            },
        )
        stdout = (result.stdout or "").strip()
        if stdout:
            last_line = stdout.split('\n')[-1]
            try:
                data = json.loads(last_line)
                if "__error__" in data:
                    return f"[沙箱] {data['__error__']}"
                return data.get("__result__", "(无返回值)")
            except json.JSONDecodeError:
                return stdout[:2000]
        if result.returncode != 0:
            return f"[沙箱] 执行出错: {(result.stderr or '').strip()[-300:]}"
        return "(执行完成，无输出)"
    except subprocess.TimeoutExpired:
        return f"[沙箱] 执行超时({timeout}秒)，进程已终止"
    except Exception as e:
        return f"[沙箱] 执行异常: {e}"
    finally:
        try:
            os.remove(runner_path)
        except:
            pass

def _make_executor(code_template: str, pkg_name: str):
    extra_modules = set()
    if pkg_name:
        extra_modules.add(pkg_name.replace("-", "_"))
    def executor(**kwargs):
        ok, reason = _static_check(code_template, extra_modules)
        if not ok:
            return f"[沙箱拒绝] {reason}"
        return _sandbox_exec(code_template, kwargs, extra_modules, timeout=30)
    return executor

def _load_dynamic_tools() -> dict:
    if os.path.exists(_DYNAMIC_TOOLS_FILE):
        try:
            with open(_DYNAMIC_TOOLS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_dynamic_tools(data: dict):
    try:
        with open(_DYNAMIC_TOOLS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"[agent] 保存动态工具失败: {e}")

def _register_dynamic_tool(name, description, params, executor_code, package=""):
    extra = {package.replace("-", "_")} if package else set()
    ok, reason = _static_check(executor_code, extra)
    if not ok:
        log(f"[agent] 动态工具注册被拒: {name} — {reason}")
        return False
    tool_def = {
        "type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": params,
                           "required": [k for k in params.keys()]},
        },
    }
    existing = [t["function"]["name"] for t in TOOL_DEFINITIONS]
    if name not in existing:
        TOOL_DEFINITIONS.append(tool_def)
    TOOL_EXECUTORS[name] = _make_executor(executor_code, package)
    dynamic = _load_dynamic_tools()
    dynamic[name] = {
        "description": description, "params": params,
        "executor_code": executor_code, "package": package,
        "registered_at": time.time(),
    }
    _save_dynamic_tools(dynamic)
    log(f"[agent] 动态工具已注册(沙箱版): {name}")
    return True

def _restore_dynamic_tools():
    """启动时只恢复verified的技能"""
    dynamic = _load_dynamic_tools()
    restored = 0
    for name, dt in dynamic.items():
        if not dt.get("verified", False):
            log(f"[agent] 跳过未验证技能: {name}")
            continue
        try:
            if name not in TOOL_EXECUTORS:
                TOOL_EXECUTORS[name] = _make_executor(
                    dt.get("executor_code", ""), dt.get("package", ""))
                TOOL_DEFINITIONS.append({
                    "type": "function", "function": {
                        "name": name, "description": dt.get("description", ""),
                        "parameters": {"type": "object",
                                       "properties": dt.get("params", {}),
                                       "required": [k for k in dt.get("params", {}).keys()]},
                    },
                })
                restored += 1
        except Exception as e:
            log(f"[agent] 恢复动态工具失败 {name}: {e}")
            dt["needs_reverify"] = True
            _save_dynamic_tools(dynamic)
    if restored:
        log(f"[agent] 恢复{restored}个动态工具")

# ════════════════════════════════════════════
# 技能自测试 v3（固定模板 + 字符串判断 + 全自动）
# ════════════════════════════════════════════

_SIDE_EFFECT_KEYWORDS = frozenset({
    "send", "write", "delete", "submit", "post", "push",
    "发", "删", "写", "提交", "wechat", "微信", "通知", "邮件",
})

def _has_side_effects(name: str, description: str) -> bool:
    text = f"{name} {description}".lower()
    return any(kw in text for kw in _SIDE_EFFECT_KEYWORDS)

def _judge_test_result(result: str) -> str:
    if any(x in result for x in ("[fail]", "[错误]", "[沙箱]", "超时", "ImportError")):
        return "fail"
    if "[pass]" in result or (len(result) > 0 and "[fail]" not in result):
        return "pass"
    return "fail"

def _generate_test_code(tool_name: str, params: dict,
                        executor_code: str) -> str:
    test_args = {}
    for k, v in params.items():
        ptype = v.get("type", "string")
        if ptype == "string": test_args[k] = "test"
        elif ptype == "integer": test_args[k] = 1
        elif ptype == "number": test_args[k] = 1.0
        elif ptype == "boolean": test_args[k] = True
        else: test_args[k] = "test"
    return f"""
kwargs = {repr(test_args)}
{executor_code}
if '__result__' not in dir():
    __result__ = '[fail] 工具未设置__result__'
"""

def _auto_test_and_verify(name: str, description: str, params: dict,
                          executor_code: str, package: str) -> bool:
    if _has_side_effects(name, description):
        mod = package.replace("-", "_")
        test_code = f"""
try:
    import {mod}
    __result__ = '[pass] 包可导入（有副作用，跳过功能测试）'
except ImportError as e:
    __result__ = f'[fail] 无法导入: {{e}}'
"""
    else:
        test_code = _generate_test_code(name, params, executor_code)

    extra_modules = {package.replace("-", "_")} if package else set()
    result = _sandbox_exec(test_code, {}, extra_modules, timeout=15)
    verdict = _judge_test_result(result)
    log(f"[agent] 技能测试 {name}: {verdict} — {str(result)[:100]}")

    if verdict == "pass":
        return True

    log(f"[agent] 技能测试 {name} 第1次失败，重试...")
    time.sleep(1)
    result2 = _sandbox_exec(test_code, {}, extra_modules, timeout=15)
    verdict2 = _judge_test_result(result2)
    log(f"[agent] 技能测试 {name} 重试: {verdict2} — {str(result2)[:100]}")
    return verdict2 == "pass"

def _rollback_skill(name: str):
    TOOL_EXECUTORS.pop(name, None)
    TOOL_DEFINITIONS[:] = [t for t in TOOL_DEFINITIONS
                           if t["function"]["name"] != name]
    dynamic = _load_dynamic_tools()
    dynamic.pop(name, None)
    _save_dynamic_tools(dynamic)
    skill_dir = os.path.join(PROJECT_DIR, "skills", name)
    import shutil as _shutil
    if os.path.exists(skill_dir):
        _shutil.rmtree(skill_dir, ignore_errors=True)
    log(f"[agent] 技能回滚: {name}")

def _execute_install_skill(name: str, description: str, steps: str,
                           triggers: str = "", requires: str = "") -> str:
    """安装技能：生成SKILL.md+注册动态工具（pip安装已移除）"""
    # 安全校验：技能名只允许 [a-z0-9_-]
    import re as _re
    if not _re.fullmatch(r'[a-z0-9_-]+', name):
        return f"[错误] 技能名非法: {name} (只允许a-z 0-9 _ -)"

    skills_dir = os.path.join(PROJECT_DIR, "skills", name)
    # 路径必须在skills_dir下
    try:
        resolved = Path(skills_dir).resolve()
        base = Path(os.path.join(PROJECT_DIR, "skills")).resolve()
        if not str(resolved).startswith(str(base) + os.sep) and resolved != base:
            return f"[错误] 技能路径越界: {skills_dir}"
    except Exception as e:
        return f"[错误] 技能路径校验失败: {e}"

    try:
        os.makedirs(skills_dir, exist_ok=True)
    except Exception as e:
        return f"[错误] 创建技能目录失败: {e}"

    # 1. 生成SKILL.md (pip安装已移除)
    md = f"""---
name: {name}
description: >-
  {description}
metadata:
  dong:
    requires:
      tools: [{', '.join(f'"{r}"' for r in requires.split(',') if r.strip()) or 'computer_control'}]
      os: ["win32"]
---

# {name}

## 触发条件
"""
    if triggers:
        for t in triggers.split(","):
            t = t.strip()
            if t:
                md += f'- "{t}"\n'
    else:
        md += '- （用户明确请求时触发）\n'

    md += f"""
## 步骤
{steps}

## 规则
- 执行前先截图确认当前桌面状态
- 每步操作之间等待0.5-1秒
- 如果操作失败，截图确认后重试
"""
    try:
        with open(os.path.join(skills_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(md)
    except Exception as e:
        return f"[错误] 写入SKILL.md失败: {e}"

    # 3. 注册动态工具
    msg = f"技能 '{name}' 已安装到 skills/{name}/"

    log(f"[agent] 技能已安装: {name}")
    msg += f"\n触发词: {triggers or '用户请求时'}"
    return msg

def _execute_wechat_send(to: str, message: str) -> str:
    """通过微信桌面端发送消息（UIA键盘+多ClassName+每步验证）"""
    try:
        import uiautomation as auto
        import time as _t, subprocess as _sp

        # 多ClassName尝试（兼容不同微信版本）
        wechat = None
        for cls in ['Qt51514QWindowIcon', 'Qt51410QWindowIcon', 'WeChatMainWndForPC',
                     'Qt51514QWindowOwnDCIcon', 'Qt51510QWindowIcon']:
            w = auto.WindowControl(ClassName=cls)
            if w.Exists(maxSearchSeconds=1):
                wechat = w
                break

        # 按标题找
        if not wechat:
            wechat = auto.WindowControl(Name='微信')
            if not wechat.Exists(maxSearchSeconds=1):
                wechat = None

        # 启动微信
        if not wechat:
            _sp.Popen(['start', 'weixin://'], shell=True)
            _t.sleep(4)
            for cls in ['Qt51514QWindowIcon', 'Qt51410QWindowIcon', 'WeChatMainWndForPC']:
                w = auto.WindowControl(ClassName=cls)
                if w.Exists(maxSearchSeconds=2):
                    wechat = w
                    break

        if not wechat:
            return "[停止] 微信未登录。请立即通知主人：需要手机扫码登录微信。不要再尝试其他操作。"

        # 激活+聚焦
        wechat.SetActive()
        _t.sleep(0.3)
        _focus_window('微信')
        _t.sleep(0.3)

        # Ctrl+F 搜索
        wechat.SendKeys('{Ctrl}f')
        _t.sleep(0.6)
        import pyperclip
        pyperclip.copy(to)
        wechat.SendKeys('{Ctrl}v')
        _t.sleep(0.8)

        wechat.SendKeys('{Enter}')
        _t.sleep(0.6)

        # 输入消息
        pyperclip.copy(message)
        wechat.SendKeys('{Ctrl}v')
        _t.sleep(0.3)

        # 发送
        wechat.SendKeys('{Enter}')

        log(f"[agent] 微信已发送: to={to[:20]} msg={message[:30]}")
        return f"[成功] 微信消息已发送给 \"{to}\": {message[:50]}"
    except ImportError:
        return "[错误] uiautomation 未安装"
    except Exception as e:
        return f"[错误] 微信发送失败: {e}"

# ── 演示学习：录制 → VLM分析 → 生成技能 ──
_demo_recording = None  # {"name": str, "screenshots": [path, ...], "start": timestamp}

def _execute_record_demo(name: str = "demo") -> str:
    """开始录制演示：每0.8秒截图一张，直到调用 learn_from_demo"""
    global _demo_recording
    import time as _t, threading as _th

    shots = []
    try:
        from .tools import _tool_computer_control
        r = _tool_computer_control({"action": "screenshot"}, MASTER_UID)
        import re
        m = re.search(r'[A-Z]:[^\s]+\.png', r)
        if m:
            shots.append(m.group())
    except Exception:
        pass

    _demo_recording = {"name": name, "screenshots": shots, "start": _t.time()}

    def _bg_capture():
        while _demo_recording and _demo_recording.get("name") == name:
            _t.sleep(0.8)
            if _demo_recording and _demo_recording.get("name") == name:
                try:
                    r2 = _tool_computer_control({"action": "screenshot"}, MASTER_UID)
                    import re as _re
                    m2 = _re.search(r'[A-Z]:[^\s]+\.png', r2)
                    if m2:
                        path = m2.group()
                        if path not in _demo_recording.get("screenshots", []):
                            _demo_recording["screenshots"].append(path)
                except Exception:
                    pass
    _th.Thread(target=_bg_capture, daemon=True).start()
    return f"开始录制 \"{name}\"。现在操作，完成后调 learn_from_demo。"

def _execute_learn_from_demo() -> str:
    """停止录制，VLM分析截图序列，生成可复用的SKILL.md"""
    global _demo_recording
    if not _demo_recording or not _demo_recording["screenshots"]:
        _demo_recording = None
        return "[错误] 没有在录制的演示。请先调用 record_demo"

    demo = _demo_recording
    _demo_recording = None  # 停止录制

    name = demo["name"]
    shots = demo["screenshots"]

    # 用VLM分析截图序列
    try:
        from .tools import _tool_computer_control

        # 分析每张截图的变化
        descriptions = []
        for i, shot in enumerate(shots):
            try:
                from .config import _get_cfg
                import base64, requests as _req
                cfg = _get_cfg("vision")
                with open(shot, "rb") as img:
                    b64 = base64.b64encode(img.read()).decode()
                resp = _req.post(
                    f"{cfg.api_base.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
                    json={
                        "model": cfg.model,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": f"这张截图{i+1}/{len(shots)}里用户做了什么操作？（点击了什么/输入了什么/打开了什么）。用一句话回答，不要解释。"},
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                            ]
                        }],
                        "max_tokens": 150,
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    d = resp.json()
                    desc = d["choices"][0]["message"].get("content", "")
                    descriptions.append(f"步骤{i+1}: {desc.strip()}")
            except Exception as e:
                descriptions.append(f"步骤{i+1}: (分析失败: {e})")

        # 合并描述，生成技能
        steps_analysis = "\n".join(descriptions)

        # 用LLM总结成技能步骤
        summary_prompt = f"""用户演示了"{name}"操作。VLM对截图的分析如下：

{steps_analysis}

请总结这个操作为Markdown格式的技能步骤。格式：
1. 第1步做什么
2. 第2步做什么
...
N. 最后一步做什么

只输出步骤，不要其他内容。"""

        api_key, api_base, model = _get_agent_config()
        import requests as _req2
        resp2 = _req2.post(
            f"{api_base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": summary_prompt}], "max_tokens": 500},
            timeout=30,
        )
        if resp2.status_code == 200:
            skill_steps = resp2.json()["choices"][0]["message"]["content"]
        else:
            skill_steps = steps_analysis

        # 生成并安装技能
        triggers = name
        return _execute_install_skill(
            name=name, description=f"演示学习: {name}", steps=skill_steps,
            triggers=triggers, requires=""
        )
    except Exception as e:
        return f"[错误] 学习失败: {e}"

def _execute_learn_from_video(video_path: str, skill_name: str) -> str:
    """从录屏视频学习：ffmpeg提取关键帧→VLM分析→生成技能"""
    import subprocess, tempfile, os, base64, requests as _req, time as _t

    if not os.path.exists(video_path):
        return f"[错误] 视频文件不存在: {video_path}"

    # 创建临时目录提取帧
    tmpdir = tempfile.mkdtemp(prefix="agent_video_")
    try:
        # ffmpeg: 每秒1帧
        result = subprocess.run([
            "ffmpeg", "-i", video_path, "-vf", "fps=1",
            f"{tmpdir}/frame_%03d.png", "-y"
        ], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return f"[错误] ffmpeg提取帧失败: {result.stderr[:200]}"

        frames = sorted([f for f in os.listdir(tmpdir) if f.endswith('.png')])
        if not frames:
            return "[错误] 视频没有提取到帧"

        # 采样：最多15帧
        if len(frames) > 15:
            step = len(frames) // 15
            frames = frames[::step][:15]

        log(f"[agent] 视频学习: {len(frames)}帧, 技能={skill_name}")

        # VLM分析每帧
        from .config import _get_cfg as _cfg
        cfg = _cfg("vision")
        descriptions = []

        for i, fname in enumerate(frames):
            fpath = os.path.join(tmpdir, fname)
            with open(fpath, "rb") as img:
                b64 = base64.b64encode(img.read()).decode()
            resp = _req.post(
                f"{cfg.api_base.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
                json={
                    "model": cfg.model,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": f"截图{i+1}/{len(frames)}。描述当前界面状态和用户可能的操作。20字以内。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                    ]}],
                    "max_tokens": 100,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                desc = resp.json()["choices"][0]["message"]["content"]
                descriptions.append(f"步骤{i+1}: {desc.strip()}")
            _t.sleep(0.3)

        # 用LLM总结
        analysis = "\n".join(descriptions)
        api_key, api_base, model = _get_agent_config()
        resp2 = _req.post(
            f"{api_base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{
                "role": "user",
                "content": f"根据VLM对录屏的分析，总结为操作步骤（Markdown）：\n\n{analysis}\n\n格式：\n1. 步骤1\n2. 步骤2\n..."
            }], "max_tokens": 500},
            timeout=30,
        )
        steps = resp2.json()["choices"][0]["message"]["content"] if resp2.status_code == 200 else analysis

        return _execute_install_skill(
            name=skill_name, description=f"从视频学习: {skill_name}",
            steps=steps, triggers=skill_name, requires=""
        )
    finally:
        # 清理临时文件
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

TOOL_EXECUTORS = {
    "read_file": _execute_read_file,
    "write_file": _execute_write_file,
    "edit_file": _execute_edit_file,
    "review_code": _execute_review_code,
    "modify_self": _execute_modify_self,
    "locate_symbol": _execute_locate,
    "file_dependencies": _execute_depend,
    "search_code": _execute_search_code,
    "run_bash": _execute_run_bash,
    "search_memory": _execute_search_memory,
    "list_directory": _execute_list_directory,
    "computer_control": _execute_computer_control,
    "search_online": _execute_search_online,
    "install_skill": _execute_install_skill,
    "wechat_send": _execute_wechat_send,
    "record_demo": _execute_record_demo,
    "learn_from_demo": _execute_learn_from_demo,
    "learn_from_video": _execute_learn_from_video,
    "browser_control": _execute_browser_control,
}

# ════════════════════════════════════════════
# 技能系统对接
# ════════════════════════════════════════════

def _list_skills() -> List[str]:
    skills_dir = os.path.join(PROJECT_DIR, "skills")
    if not os.path.exists(skills_dir):
        return []
    return [d for d in os.listdir(skills_dir)
            if os.path.isdir(os.path.join(skills_dir, d)) and not d.startswith(".")]

def _load_skill_md(skill_name: str) -> Optional[str]:
    p = os.path.join(PROJECT_DIR, "dong", "skills", skill_name, "SKILL.md")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        log(f"[agent] 加载技能失败 {skill_name}: {e}")
        return None

def _build_skills_context() -> str:
    skills = _list_skills()
    if not skills:
        return "（暂无可用技能）"
    lines = []
    for name in skills:
        md = _load_skill_md(name)
        if md:
            lines.append(f"### {name}\n{md[:300]}...")
    return "\n\n".join(lines) if lines else "（暂无可用技能）"

def detect_skill_trigger(user_message: str) -> Optional[str]:
    for name in _list_skills():
        md = _load_skill_md(name)
        if md:
            m = re.search(r'##\s*触发条件\s*\n(.*?)(?:\n##|\Z)', md, re.DOTALL)
            if m:
                keywords = re.findall(r'[""]([^"]+)[""]', m.group(1))
                if any(kw in user_message for kw in keywords):
                    return name
    return None

def _guess_file_from_text(text: str) -> str:
    m = re.search(r"([A-Za-z0-9_./\\-]+\.py)", text)
    if m:
        return m.group(1).replace("\\", "/")
    return ""

def _handle_direct_agent_command(user_message: str, uid: int, _team_internal: bool = False) -> Optional[str]:
    global _L3_PERMIT, _L2_PERMIT_UNTIL
    text = user_message.strip()
    if text.startswith("/d "):
        text = text[3:].strip()
    low = text.lower()
    if low == "team" or low.startswith("team "):
        return _set_team_mode("team")
    if low == "solo" or low.startswith("solo "):
        return _set_team_mode("solo")
    if not _team_internal and _team_mode() == "team" and not _is_team_exempt(low):
        return _execute_team_command(text, uid)
    if low == "context":
        return _session_info(uid)
    if low == "new" or low.startswith("new"):
        _clear_session(uid)
        return "✅ 会话已清空。下一个 /d 命令将开启新窗口。"
    if low.startswith("pref") or text.startswith("偏好"):
        sub = text.split(None, 1)[1].strip() if " " in text else ""
        if sub.startswith("add ") or sub.startswith("加 "):
            parts = sub.split(None, 1)
            body = parts[1].strip() if len(parts) > 1 else ""
            if "=" in body:
                k, v = body.split("=", 1)
                _USER_PREFS.setdefault(uid, {})[k.strip()] = v.strip()
                _save_user_prefs()
                return f"✅ 偏好已记录: {k.strip()} = {v.strip()}"
            return "格式: /d pref add key=value"
        if sub == "list" or sub == "列表":
            return _load_user_prefs(uid) or "(暂无偏好)"
        if sub.startswith("remove") or sub.startswith("删"):
            key = sub.split(None, 1)[1].strip() if " " in sub else ""
            if key and uid in _USER_PREFS and key in _USER_PREFS[uid]:
                del _USER_PREFS[uid][key]
                _save_user_prefs()
                return f"✅ 已删除偏好: {key}"
            return f"未找到偏好: {key}"
        return "用法: /d pref add key=value | list | remove key"
    if low.startswith("read "):
        spec = text.split(None, 1)[1].strip()
        m = re.match(r"^(.*?)(?::(\d+))?$", spec)
        if not m:
            return "用法: /d read 文件路径:页码"
        path = m.group(1).strip()
        page = int(m.group(2) or "1")
        return _execute_read_file(path, page=page)
    if low.startswith("locate "):
        return _execute_locate(text.split(None, 1)[1].strip())
    if low.startswith("depend "):
        return _execute_depend(text.split(None, 1)[1].strip())
    if low.startswith("review "):
        path = text.split(None, 1)[1].strip()
        return _execute_review_code(path, "review")
    if text.startswith(("审查 ", "代码审查 ")):
        return _execute_review_code(text.split(None, 1)[1].strip())
    if low.startswith("plan ") or text.startswith("计划 "):
        body = text.split(None, 1)[1].strip() if " " in text else ""
        if not body:
            return "用法: /d plan <任务描述>"
        return _format_autonomous_plan(body)
    if low.startswith("fix ") or text.startswith("修复 "):
        body = text.split(None, 1)[1].strip() if " " in text else text
        # ★ 创建/更新任务状态
        targets = _locate_fix_targets(body)
        if not _TASK_STATE:
            _new_task(body, targets)
        else:
            _update_task(status="in_progress", files_touched=targets, next_action="执行修改")
        path = _guess_file_from_text(body)
        if path:
            result = _execute_modify_self(path, body)
        else:
            learned = _try_learned_fix(body, targets)
            result = learned if learned else _execute_fix_issue(body)
            if not learned and isinstance(result, str) and "[成功]" in result:
                learned_pattern = _learn_from_last_change(body)
                if learned_pattern:
                    result += f"\n\n已学习修复模式 #{learned_pattern.get('id')}，下次相似问题会先尝试复用。"
        # ★ 修复完成后自动验证
        if isinstance(result, str) and "[错误]" not in result and "[回滚]" not in result:
            changed = _locate_fix_targets(body)
            if changed:
                plan = build_validation_plan(changed)
                val = run_validation(plan)
                _update_task(validation={"ok": val["ok"], "failed_step": val.get("failed_step")},
                             next_action="完成", current_step=len(changed),
                             status="done" if val["ok"] else "failed")
                if val["ok"]:
                    result += "\n\n✅ 验证通过：py_compile OK"
                    # ★ 自动测试建议
                    suggested = _select_tests_for_files(changed)
                    if suggested:
                        result += f"\n💡 建议跑测试: /d test ({', '.join(suggested)})"
                    related_hint = _format_related_hint(_CHANGE_LOG.get("changed") or changed)
                    if related_hint:
                        result += "\n" + related_hint
                    result += "\n💡 提交? /d git commit \"描述\""
                else:
                    detail = val.get("results", [{}])[-1].get("detail", "未知")
                    result += f"\n\n❌ 验证失败：{detail}"
            _finish_task("done" if "[成功]" in result else "failed")
        else:
            _update_task(status="failed", next_action="查看错误详情")
            _finish_task("failed")
        return result
    if low == "patterns" or low.startswith("patterns "):
        parts = text.split()
        if len(parts) >= 3 and parts[1].lower() == "forget":
            return _forget_learned_pattern(parts[2])
        return _format_learned_patterns()
    if low == "diff" or low.startswith("diff "):
        if not _CHANGE_LOG:
            return "暂无变更记录。用 /d fix 执行一次修复后再试。"
        log_data = _CHANGE_LOG
        lines = [f"📋 最近修复: {log_data.get('issue', '?')[:100]}",
                 f"🕐 时间: {log_data.get('time', '?')}",
                 f"📁 目标文件: {', '.join(log_data.get('targets', []))}"]
        for rel in log_data.get("changed", []):
            old = log_data.get("snapshots", {}).get(rel, "")
            try:
                with open(_safe_path(rel), "r", encoding="utf-8") as f:
                    new = f.read()
            except Exception:
                new = ""
            lines.append(f"\n── {rel} ──")
            lines.append(_generate_unified_diff(old, new, rel))
        for rel in log_data.get("rolled_back", []):
            reason = log_data.get("rollback_reasons", {}).get(rel, "未知")
            lines.append(f"\n⚠ {rel}: 已回滚 — {reason}")
        return "\n".join(lines)
    if low == "changed" or low.startswith("changed"):
        if not _CHANGE_LOG:
            return "暂无变更记录。用 /d fix 执行一次修复后再试。"
        log_data = _CHANGE_LOG
        lines = [f"📋 修复: {log_data.get('issue', '?')[:100]}",
                 f"🕐 {log_data.get('time', '?')}",
                 f"📁 涉及 {len(log_data.get('targets', []))} 个文件: {', '.join(log_data.get('targets', []))}"]
        if log_data.get("changed"):
            lines.append(f"✅ 已修改({len(log_data['changed'])}): {', '.join(log_data['changed'])}")
        if log_data.get("rolled_back"):
            lines.append(f"⚠ 已回滚({len(log_data['rolled_back'])}): {', '.join(log_data['rolled_back'])}")
            for rel, reason in log_data.get("rollback_reasons", {}).items():
                lines.append(f"   - {rel}: {reason[:80]}")
        val = log_data.get("validation")
        if val:
            lines.append(f"✅ 验证: {'通过' if val.get('ok') else '失败 — ' + str(val.get('failed_step', '?'))}")
        return "\n".join(lines)
    if low == "status" or low.startswith("status"):
        return _format_task_status()
    if low == "tasks" or low.startswith("tasks"):
        return _format_task_list()
    if low == "done" or low.startswith("done"):
        if _TASK_STATE:
            _finish_task("done")
            return "✅ 任务已标记为完成并归档。"
        return "没有进行中的任务。"
    if low.startswith("trace "):
        return _execute_trace(text.split(None, 1)[1].strip() if " " in text else "")
    if low.startswith("impact "):
        return _execute_impact(text.split(None, 1)[1].strip() if " " in text else "")
    if low.startswith("sidefx "):
        return _execute_sidefx(text.split(None, 1)[1].strip() if " " in text else "")
    if low.startswith("related "):
        return _execute_related(text.split(None, 1)[1].strip() if " " in text else "")
    if low.startswith("index ") or text.startswith("索引 "):
        symbol = text.split(None, 1)[1].strip() if " " in text else ""
        if not symbol:
            return "用法: /d index <函数名或类名>"
        index = _ensure_project_index()
        matches = index.get("symbols", {}).get(symbol, [])
        if not matches:
            # 模糊搜索
            low = symbol.lower()
            for name, items in index.get("symbols", {}).items():
                if low in name.lower():
                    matches.extend(items)
        if not matches:
            return f"未找到: {symbol}"
        callers = []
        call_graph = index.get("call_graph", {})
        for caller, callees in call_graph.items():
            if symbol in callees:
                callers.append(caller)
        inherits = [i for i in index.get("inherits", []) if i.get("class") == symbol]
        lines = [f"📎 {symbol}"]
        for m in matches[:5]:
            lines.append(f"   📍 {m.get('file')}:{m.get('line')} ({m.get('kind')})")
            params = m.get("params", [])
            if params:
                param_str = ", ".join(f"{p['name']}:{p['type'] or '?'}" for p in params)
                lines.append(f"   📥 参数: ({param_str}) → {m.get('returns') or '?'}")
            bases = m.get("bases", [])
            if bases:
                lines.append(f"   🧬 继承: {', '.join(bases)}")
            methods = m.get("methods", [])
            if methods:
                lines.append(f"   📋 方法: {', '.join(methods[:10])}")
        if callers:
            lines.append(f"   📞 被调用者: {', '.join(sorted(set(callers))[:10])}")
        callees = call_graph.get(symbol, [])
        if callees:
            lines.append(f"   📤 它调用: {', '.join(sorted(set(callees))[:10])}")
        if inherits:
            for inh in inherits[:3]:
                lines.append(f"   🧬 {inh['class']} 继承自: {', '.join(inh['bases'])}")
        return "\n".join(lines)
    if low.startswith("refs ") or text.startswith("引用 "):
        symbol = text.split(None, 1)[1].strip() if " " in text else ""
        if not symbol:
            return "用法: /d refs <函数名>"
        index = _ensure_project_index()
        call_graph = index.get("call_graph", {})
        callers = []
        for caller, callees in call_graph.items():
            if symbol in callees:
                callers.append(caller)
        symbols = index.get("symbols", {})
        if not callers and symbol not in symbols:
            return f"未找到 '{symbol}' 的定义或引用。"
        lines = [f"📎 {symbol} 引用关系"]
        if symbol in symbols:
            for m in symbols[symbol][:3]:
                lines.append(f"   定义: {m.get('file')}:{m.get('line')}")
        if callers:
            lines.append(f"   调用者({len(callers)}): {', '.join(sorted(set(callers))[:20])}")
        else:
            lines.append("   无调用者（未被其他函数调用）")
        callees = call_graph.get(symbol, [])
        if callees:
            lines.append(f"   它调用({len(callees)}): {', '.join(sorted(set(callees))[:20])}")
        return "\n".join(lines)
    if low.startswith("permit ") or text.startswith("允许 "):
        level = text.split(None, 1)[1].strip().upper() if " " in text else ""
        if level in ("L2", "2"):
            _L2_PERMIT_UNTIL = time.time() + 3600
            log(f"[agent] L2临时开放60分钟")
            return "✅ L2 权限已临时开放60分钟。到期自动降回L3禁止。"
        elif level in ("L3", "3"):
            _L3_PERMIT = True
            log(f"[agent] L3全局开放")
            return "⚠ L3 权限已开放（系统级操作允许）。用 /d forbid L3 关闭。"
        return "用法: /d permit L2 (60分钟) 或 /d permit L3 (直到关闭)"
    if low.startswith("forbid ") or text.startswith("禁止 "):
        target = text.split(None, 1)[1].strip() if " " in text else ""
        if target in ("L1", "1"):
            _SECURITY_LEVEL_OVERRIDE["write_file"] = 99
            _SECURITY_LEVEL_OVERRIDE["edit_file"] = 99
            log(f"[agent] L1写文件已禁止")
            return "✅ L1 写文件已禁止。用 /d permit L1 恢复。"
        elif target in ("L2", "2"):
            _L2_PERMIT_UNTIL = 0.0
            log(f"[agent] L2权限已关闭")
            return "✅ L2 权限已关闭。"
        elif target in ("L3", "3"):
            _L3_PERMIT = False
            log(f"[agent] L3权限已关闭")
            return "✅ L3 权限已关闭。"
        elif target == "write":
            _SECURITY_LEVEL_OVERRIDE["write_file"] = 99
            _SECURITY_LEVEL_OVERRIDE["edit_file"] = 99
            return "✅ 写文件已禁止。"
        return "用法: /d forbid L1 | L2 | L3"
    if low == "audit" or low.startswith("audit "):
        date = text.split(None, 1)[1].strip() if " " in text else ""
        return _get_audit_summary(date)
    if low == "sandbox" or low.startswith("sandbox"):
        return _sandbox_status()
    if low.startswith("log") or text.startswith("日志"):
        sub = text.split(None, 1)[1].strip() if " " in text else ""
        log_dir = os.path.join(PROJECT_DIR, "Logs") if os.path.isdir(os.path.join(PROJECT_DIR, "Logs")) else os.path.join(os.path.dirname(PROJECT_DIR), "Logs")
        if sub == "today" or sub == "今天":
            target = os.path.join(log_dir, f"dong_{datetime.now().strftime('%Y-%m-%d')}.log")
            if os.path.exists(target):
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    lines = [l for l in f.readlines() if "ERROR" in l or "CRITICAL" in l]
                if not lines:
                    return "今日无ERROR日志。"
                return f"📋 今日ERROR ({len(lines)}条):\n" + "".join(lines[-20:])
            return "今日日志文件不存在。"
        if sub:
            # 过滤模块日志
            log_files = sorted([f for f in os.listdir(log_dir) if f.endswith(".log")], reverse=True)[:3]
            matched = []
            for lf in log_files:
                try:
                    with open(os.path.join(log_dir, lf), "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if sub.lower() in line.lower():
                                matched.append(line.strip()[:200])
                                if len(matched) >= 30:
                                    break
                except Exception:
                    pass
                if len(matched) >= 30:
                    break
            if not matched:
                return f"未找到包含'{sub}'的日志。"
            return f"📋 日志 '{sub}' ({len(matched)}条):\n" + "\n".join(matched[-20:])
        # 默认：最近错误
        log_files = sorted([f for f in os.listdir(log_dir) if f.endswith(".log")], reverse=True)[:2]
        errors = []
        for lf in log_files:
            try:
                with open(os.path.join(log_dir, lf), "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if "ERROR" in line or "WARNING" in line:
                            errors.append(line.strip()[:200])
                            if len(errors) >= 15:
                                break
            except Exception:
                pass
            if len(errors) >= 15:
                break
        if not errors:
            return "最近日志无异常。"
        return f"📋 最近日志 ({len(errors)}条):\n" + "\n".join(errors[-15:])
    if low.startswith("doc ") or text.startswith("文档 "):
        args = text.split(None, 1)[1].strip() if " " in text else ""
        parts = args.split(None, 1)
        lib = parts[0] if parts else ""
        func = parts[1] if len(parts) > 1 else ""
        if not lib:
            return "用法: /d doc 库名 [函数名]"
        return _search_doc(lib, func)
    if low == "suggest" or low.startswith("suggest off"):
        if low == "suggest off" or "off" in text.lower() or "关闭" in text:
            global _SUGGEST_ENABLED
            _SUGGEST_ENABLED = False
            return "✅ 主动建议已关闭。"
        return _run_suggest_scan()
    if low.startswith("report") or text.startswith("报告"):
        sub = text.split(None, 1)[1].strip() if " " in text else ""
        if sub.startswith("time ") or sub.startswith("时间 "):
            t = sub.split(None, 1)[1].strip() if " " in sub else ""
            if t:
                _SUGGEST_REPORT_TIME = t
                return f"✅ 日报推送时间设为 {t}"
            return "用法: /d report time 21:00"
        if "周" in sub or "week" in sub.lower():
            return _format_weekly_report()
        return _format_daily_report()
    if low.startswith("knowledge") or low.startswith("知识库"):
        topic = text.split(None, 1)[1].strip() if " " in text else ""
        content = _get_knowledge_section(topic) if topic else _load_knowledge()
        return content[:2500] if len(content) > 2500 else content
    if low.startswith("test") or text.startswith("测试"):
        spec = text.split(None, 1)[1].strip() if " " in text else ""
        tests_dir = os.path.join(PROJECT_DIR, "tests")
        if not os.path.isdir(tests_dir):
            return "项目无 tests/ 目录。"
        if not spec:
            # 全量测试
            all_tests = [f"tests/{f}" for f in os.listdir(tests_dir)
                         if f.endswith(".py") and f != "__init__.py" and not f.startswith("_")]
            result = _run_tests(all_tests) if all_tests else {"ok": True, "results": []}
        elif spec.endswith(".py"):
            result = _run_tests([spec])
        else:
            # 关键词匹配
            matching = [f"tests/{f}" for f in os.listdir(tests_dir)
                        if spec.lower() in f.lower() and f.endswith(".py")]
            result = _run_tests(matching if matching else ["tests/smoke_test.py"])
        lines = []
        for r in result.get("results", []):
            icon = "✅" if r["ok"] else "❌"
            lines.append(f"{icon} {r['file']}: {r['detail'][:200]}")
        summary = "全部通过" if result["ok"] else "有失败"
        return f"测试结果 ({summary}):\n" + "\n".join(lines)
    if low.startswith("git") or text.startswith("git "):
        gcmd = text.split(None, 1)[1].strip() if " " in text else ""
        import subprocess as _sp
        repo = PROJECT_DIR if os.path.isdir(os.path.join(PROJECT_DIR, ".git")) else os.path.dirname(PROJECT_DIR)
        try:
            if gcmd == "status":
                r = _sp.run(["git", "-C", repo, "status", "--short"], capture_output=True, text=True, timeout=10)
                return r.stdout.strip() or "(干净)"
            elif gcmd.startswith("commit"):
                msg = gcmd[6:].strip().strip('"')
                if not msg:
                    return "用法: /d git commit \"提交信息\""
                # 只add .py和.md
                r1 = _sp.run(["git", "-C", repo, "add"] +
                             [f for f in [_guess_file_from_text(text)] if f] +
                             ["*.py", "*.md"], capture_output=True, text=True, timeout=5)
                r2 = _sp.run(["git", "-C", repo, "commit", "-m", msg], capture_output=True, text=True, timeout=10)
                if r2.returncode == 0:
                    return f"✅ 已提交: {msg}\n{r2.stdout.strip()}"
                return f"❌ 提交失败: {r2.stderr.strip()[:300]}"
            elif gcmd == "log":
                r = _sp.run(["git", "-C", repo, "log", "--oneline", "-5"], capture_output=True, text=True, timeout=10)
                return r.stdout.strip() or "(无提交记录)"
            elif gcmd == "diff":
                r = _sp.run(["git", "-C", repo, "diff", "--stat"], capture_output=True, text=True, timeout=10)
                return r.stdout.strip()[:2000] or "(无未提交变更)"
            else:
                return f"支持: /d git status|commit|log|diff"
        except Exception as e:
            return f"[错误] git命令失败: {e}"
    if low.startswith("patch"):
        sub = text.split(None, 1)[1].strip() if " " in text else ""
        if low == "patch" or sub == "log":
            return _format_patch_log()
        if sub.startswith("undo") or sub.startswith("撤销"):
            parts = sub.split()
            patch_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            if not patch_id:
                return "用法: /d patch undo <编号>"
            return _undo_patch(patch_id)
        if sub == "preview" or sub == "预览":
            if not _PATCH_LOG:
                return "暂无待应用的patch。"
            last = _PATCH_LOG[-1]
            return (f"📋 Patch #{last['id']} 预览:\n"
                    f"  文件: {last['file']}\n"
                    f"  行: L{last['line_start']}-{last['line_end']}\n"
                    f"  - {last['original'][:100]}\n"
                    f"  + {last['replacement'][:100]}")
        return "用法: /d patch [log|preview|undo <编号>]"
    if low.startswith("validate ") or text.startswith("验证 "):
        spec = text.split(None, 1)[1].strip() if " " in text else ""
        if not spec:
            all_py = _project_py_files()
            plan = build_validation_plan(all_py)
        else:
            plan = build_validation_plan([spec])
        val = run_validation(plan)
        if val["ok"]:
            return f"✅ 验证通过：{val['results'][0]['detail'] if val['results'] else 'OK'}"
        else:
            detail = val.get("results", [{}])[-1].get("detail", "未知")
            return f"❌ 验证失败 [{val['failed_step']}]：{detail}"
    # ★ 自然语言兜底：固定命令未匹配 → 尝试NL解析
    try:
        from .agent_loop import parse_nl_command
        nl_cmd = parse_nl_command(text)
        if nl_cmd and nl_cmd != text:
            log(f"[agent] NL解析: '{text[:40]}' → '{nl_cmd}'")
            # 递归调用自身处理解析后的命令
            return _handle_direct_agent_command(nl_cmd, uid)
    except Exception as e:
        log(f"[agent] NL解析异常: {e}")
    return None

# ════════════════════════════════════════════
# 记忆对接（memory.py API + claude_dc_memory.py）
# ════════════════════════════════════════════

def _read_memory(uid: int) -> str:
    parts = []
    # 1. 冬的记忆库
    try:
        from .memory import get_memory_text
        t = get_memory_text(uid)
        if t:
            parts.append(f"【冬的记忆】\n{t}")
    except Exception as e:
        log(f"[agent] 读冬记忆失败: {e}")

    # 2. DC 会话记忆（用API不裸读JSON）
    try:
        from .bridge.dc_memory import load_context
        ctx = load_context()
        if ctx:
            parts.append(f"【DC上下文】\n{str(ctx)[:2000]}")
    except Exception as e:
        log(f"[agent] 读DC记忆失败: {e}")

    return "\n\n".join(parts) if parts else "（暂无记忆）"

def _write_memory(uid: int, user_msg: str, reply: str):
    # 写冬的记忆
    try:
        from .memory import add_memory
        add_memory(uid, f"[Agent] 用户: {user_msg[:100]} | 回复: {reply[:100]}", is_important=False)
    except Exception as e:
        log(f"[agent] 写冬记忆失败: {e}")

    # 更新 DC 上下文
    try:
        from .bridge.dc_memory import save_context
        summary = f"用户: {user_msg[:200]} | Agent: {reply[:200]}"
        save_context(summary)
    except Exception as e:
        log(f"[agent] 写DC记忆失败: {e}")

# ── 待办任务（登录受阻等场景自动记忆+自动续接）──

def _save_pending_task(uid: int, task: str):
    """保存受阻的待办任务"""
    try:
        from .bridge.dc_memory import save_context
        save_context(f"PENDING|{uid}|{task}")
        log(f"[agent] 待办已存: {task[:80]}")
    except Exception as e:
        log(f"[agent] 存待办失败: {e}")

def _pop_pending_task(uid: int) -> Optional[str]:
    """取出并清除待办任务"""
    try:
        from .bridge.dc_memory import load_context, save_context
        ctx = load_context()
        if ctx and ctx.startswith("PENDING|"):
            parts = ctx.split("|", 2)
            if len(parts) >= 3 and parts[1] == str(uid):
                save_context("")  # 清除
                task = parts[2]
                log(f"[agent] 待办恢复: {task[:80]}")
                return task
    except Exception:
        pass
    return None

# ════════════════════════════════════════════
# LLM 调用（异步 aiohttp + 重试）
# ════════════════════════════════════════════

SYSTEM_PROMPT = """你是冬的智能体引擎，运行在QQ机器人"冬"内部。

## ⚠ 核心原则：行动优先
你容易犯的错误：接到任务→读代码→搜文件→读更多代码→耗尽轮数→什么都没做。
正确做法：接到任务→判断类型→立刻用最直接的工具执行→成功。

## 任务类型判断（第一步必须做！）
- 桌面/GUI操作（打开应用、点击、输入、截图）→ 用 computer_control，不要读代码
- 代码修改（改bug、加功能）→ 先 search_code 定位 → read_file 确认 → edit_file 修改
- 问题排查（为什么出错）→ search_code 搜报错 → read_file 看上下文 → 分析
- 知识查询（怎么用、是什么）→ search_online 搜答案

## 桌面操作流程
1. computer_control(action=launch, window="应用名") 启动应用
2. computer_control(action=screenshot) 截图+分析界面（含VLM描述）
3. 优先：computer_control(action=click_element, name="按钮名") 按名称点击
4. 如果click_element失败（非UIA应用）：用键盘导航
   - computer_control(action=press, key=tab) 切换到下一个控件
   - computer_control(action=press, key=enter) 确认/点击
   - computer_control(action=type, text="xxx") 输入文字
   - 组合键：computer_control(action=press, key=ctrl+v) 粘贴
5. 搜索/查找：按Ctrl+F → 输入关键词 → 回车
6. 每步操作后截图确认结果
7. 发送消息：输入完按Enter发送

## 自主决策流程
遇到当前无法完成的任务：
1. search_online 搜索解决方案
2. 找到了 → install_skill 装技能 → 立刻用
3. 找不到 → computer_control(screenshot) → 模拟操作
4. 新类型任务成功 → install_skill 保存为技能

	## ⚠ 登录/权限 → 必须通知主人
	- 微信/QQ等应用出现登录界面、QR码 → 立刻停止，告诉主人"需要扫码登录"
	- 不要尝试绕过——只有主人能扫码
	- 已登录则直接继续，不用通知

## 所有工具
- read_file: 读取文件，支持行号范围
- write_file: 写入文件
- edit_file: 精确替换文件中的文本
- search_code: 在项目中搜索代码(grep)
- run_bash: 执行命令（白名单限制，项目目录）
- search_memory: 搜索冬的记忆库
- list_directory: 列出目录
- computer_control: 桌面控制（截图/点击/输入/启动应用/滚动/按键）
- wechat_send: 通过微信桌面端发送消息
- browser_control: Playwright浏览器控制（打开网页/点击/输入/截图/读内容，CSS选择器精确定位）
- record_demo + learn_from_demo: 录制主人操作→VLM分析→自动生成技能（"看一遍就学会"）
- search_online: 搜索互联网
- install_skill: 安装新技能到技能库

## 安全规则
1. 文件操作限制在: {project_dir}
2. computer_control只在主人请求或作为fallback时使用
3. 不透露API密钥、完整路径等敏感信息

## 项目知识库
{knowledge_context}

## 项目记忆
{project_memory}

## 用户偏好
{user_preferences}

## 记忆上下文
{memory_context}

## 已安装技能
{skills_context}

## 回复要求
1. 先读后改，不要盲改
2. 复杂任务分步，每步说明
3. 用中文回复，简洁自然
4. 任务完成后，如果是新类型任务，自动 install_skill 保存"""

async def _call_llm(messages: List[Dict], tools: List[Dict] = None, retry: int = 1) -> Dict:
    api_key, api_base, model = _get_agent_config()
    if not api_key:
        raise RuntimeError("Agent API Key 未配置")

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {"model": model, "messages": messages, "max_tokens": AGENT_MAX_TOKENS,
               "temperature": AGENT_TEMPERATURE}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    url = f"{api_base.rstrip('/')}/chat/completions"
    log(f"[agent] LLM: model={model} msgs={len(messages)} tools={len(tools) if tools else 0}")

    last_err = None
    for attempt in range(retry + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload,
                                        timeout=aiohttp.ClientTimeout(total=120, connect=10)) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        log(f"[agent] LLM {resp.status}: {err[:200]}")
                        if resp.status == 429 and attempt < retry:
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                        raise RuntimeError(f"LLM API {resp.status}: {err[:200]}")
                    return await resp.json()
        except aiohttp.ClientError as e:
            last_err = e
            if attempt < retry:
                await asyncio.sleep(1)
        except RuntimeError:
            raise

    raise RuntimeError(f"LLM 网络错误(重试{retry}次后): {last_err}")

# ════════════════════════════════════════════
# Agent 主循环
# ════════════════════════════════════════════

# ════════════════════════════════════════════
# 安全权限检查
# ════════════════════════════════════════════

def _check_security(name: str, args: Dict) -> Optional[str]:
    """返回 None = 允许；返回字符串 = 拒绝原因"""
    # L0黑名单文件：拒绝读取 .env 和密钥文件
    if name == "read_file":
        path = args.get("path", "")
        basename = os.path.basename(path).lower()
        if basename == ".env" or basename.endswith(".key") or "secret" in basename:
            return f"[安全] 拒绝读取敏感文件: {path}"
    if name == "search_code":
        _path = args.get("path", "")
        if ".env" in _path or "secret" in _path.lower():
            return f"[安全] 拒绝搜索敏感路径: {_path}"

    level = _SECURITY_LEVELS.get(name, 0)
    override = _SECURITY_LEVEL_OVERRIDE.get(name)
    effective = override if override is not None else level

    # L3: 除非全局允许L3，否则拒绝
    if effective >= 3 and not _L3_PERMIT:
        return f"[安全] L3 工具 '{name}' 已被拒绝。主人说'允许L3'才能用。"

    # L2: 60分钟临时窗口
    if effective >= 2:
        if _L3_PERMIT:
            return None  # L3全开也覆盖L2
        if time.time() < _L2_PERMIT_UNTIL:
            return None  # 窗口内
        return f"[安全] L2 工具 '{name}' 需要临时授权。主人说 '/d permit L2' 开放60分钟。"

    # L1: 写文件 — 默认允许，除非被 forbid
    if effective >= 1:
        if _SECURITY_LEVEL_OVERRIDE.get(name) == 99:
            return f"[安全] L1 工具 '{name}' 已被 /d forbid 禁止。"
    return None


# ════════════════════════════════════════════
# 项目知识库加载
# ════════════════════════════════════════════

# ════════════════════════════════════════════
# 项目记忆 + 用户偏好 (第1项)
# ════════════════════════════════════════════
_PROJECT_MEMORY: Dict[str, Any] = {}
_USER_PREFS: Dict[int, Dict[str, Any]] = {}

def _load_project_memory() -> str:
    global _PROJECT_MEMORY
    if not _PROJECT_MEMORY:
        try:
            if os.path.exists(AGENT_MEMORY_FILE):
                with open(AGENT_MEMORY_FILE, "r", encoding="utf-8") as f:
                    _PROJECT_MEMORY = json.load(f)
        except Exception:
            pass
    if not _PROJECT_MEMORY:
        return "(暂无项目记忆)"
    lines = ["项目记忆:"]
    facts = _PROJECT_MEMORY.get("facts", [])
    if facts:
        for f in facts[-10:]:
            lines.append(f"- {f}")
    pitfalls = _PROJECT_MEMORY.get("pitfalls", [])
    if pitfalls:
        lines.append("常见坑:")
        for p in pitfalls[-5:]:
            lines.append(f"- {p}")
    return "\n".join(lines)


def _save_project_memory():
    try:
        tmp = AGENT_MEMORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_PROJECT_MEMORY, f, ensure_ascii=False, indent=2)
        os.replace(tmp, AGENT_MEMORY_FILE)
    except Exception as e:
        log(f"[agent] 项目记忆保存失败: {e}")


def _load_user_prefs(uid: int) -> str:
    global _USER_PREFS
    if not _USER_PREFS:
        try:
            if os.path.exists(AGENT_PREFS_FILE):
                with open(AGENT_PREFS_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                _USER_PREFS = {int(k): v for k, v in raw.items()}
        except Exception:
            pass
    prefs = _USER_PREFS.get(uid, {})
    if not prefs:
        return "(暂无用户偏好)"
    lines = ["用户偏好:"]
    for k, v in prefs.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def _save_user_prefs():
    try:
        tmp = AGENT_PREFS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in _USER_PREFS.items()}, f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, AGENT_PREFS_FILE)
    except Exception as e:
        log(f"[agent] 用户偏好保存失败: {e}")


def _load_knowledge() -> str:
    """加载 PROJECT_KNOWLEDGE.md，缓存"""
    global _KNOWLEDGE_CACHE
    if _KNOWLEDGE_CACHE is not None:
        return _KNOWLEDGE_CACHE
    if os.path.exists(_KNOWLEDGE_FILE):
        try:
            with open(_KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
                _KNOWLEDGE_CACHE = f.read()[:3000]
        except Exception as e:
            _KNOWLEDGE_CACHE = f"(知识库加载失败: {e})"
    else:
        _KNOWLEDGE_CACHE = "(项目知识库未创建 — 请主人编写 dong/PROJECT_KNOWLEDGE.md)"
    return _KNOWLEDGE_CACHE


def _get_knowledge_section(topic: str = "") -> str:
    """按主题检索知识库"""
    full = _load_knowledge()
    if not topic:
        return full
    lines = full.splitlines()
    in_section = False
    result = []
    for line in lines:
        if line.startswith("##") or line.startswith("# "):
            in_section = topic.lower() in line.lower()
        if in_section or not topic:
            result.append(line)
    return "\n".join(result) if result else f"知识库中未找到 '{topic}' 相关内容。"


# ════════════════════════════════════════════
# Agent 主循环
# ════════════════════════════════════════════

async def _execute_tool(name: str, args: Dict) -> str:
    # ★ 安全权限检查
    rejection = _check_security(name, args)
    if rejection:
        log(f"[agent] 安全拒绝: {name} — {rejection}")
        _audit_tool(name, args, rejection, 0)
        return rejection
    executor = TOOL_EXECUTORS.get(name)
    if not executor:
        return f"[错误] 未知工具: {name}"
    t0 = time.time()
    try:
        log(f"[agent] 工具: {name}({json.dumps(args, ensure_ascii=False)[:100]})")
        if asyncio.iscoroutinefunction(executor):
            result = await executor(**args)
        else:
            result = await asyncio.to_thread(lambda: executor(**args))
        elapsed = (time.time() - t0) * 1000
        log(f"[agent] 结果({name}): {str(result)[:80]}")
        _audit_tool(name, args, str(result), elapsed)
        return str(result)
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        log(f"[agent] 工具异常 {name}: {e}")
        _audit_tool(name, args, f"[异常] {e}", elapsed)
        return f"[错误] 工具执行异常: {e}"

async def process_agent_command(
    uid: int, user_message: str,
    send_reply: Optional[Callable[[str], Awaitable[None]]] = None,
) -> str:
    api_key, _, _ = _get_agent_config()
    if not api_key:
        return "[错误] Agent API Key 未配置。请设置 DONG_AGENT_API_KEY 环境变量"

    log(f"[agent] 启动: uid={uid} msg={user_message[:80]}")

    # 保存原始消息（用于存待办）
    original_message = user_message
    direct_result = _handle_direct_agent_command(user_message, uid)
    if direct_result is not None:
        return direct_result

    # 检查是否有未完成的待办任务
    pending = _pop_pending_task(uid)
    if pending:
        user_message = f"【你的待办任务】{pending}\n\n【用户说】{user_message}\n\n如果用户表示已登录/已完成/已搞定，请立刻继续执行待办任务。不要问，直接做。"

    knowledge_ctx = _load_knowledge()
    memory_ctx = _read_memory(uid)
    skills_ctx = _build_skills_context()
    project_mem = _load_project_memory()
    user_prefs = _load_user_prefs(uid)
    system = SYSTEM_PROMPT.format(project_dir=PROJECT_DIR,
                                  memory_context=memory_ctx,
                                  skills_context=skills_ctx,
                                  knowledge_context=knowledge_ctx,
                                  project_memory=project_mem,
                                  user_preferences=user_prefs)

    # ★ 持久会话：复用历史messages
    messages = _get_session(uid, system, user_message)

    final = ""
    tool_results = []

    for round_n in range(1, MAX_TOOL_ROUNDS + 1):
        log(f"[agent] 第{round_n}轮")
        try:
            messages = _maybe_compact_messages(uid, messages)
            resp = await _call_llm(messages, TOOL_DEFINITIONS)
        except Exception as e:
            return f"智能体引擎出错: {e}"

        choice = resp.get("choices", [{}])[0]
        msg = choice.get("message", {})
        tool_calls = msg.get("tool_calls", [])

        if not tool_calls:
            final = msg.get("content", "") or "(无回复)"
            break

        # 记录 assistant 消息
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": tool_calls})

        for tc in tool_calls:
            func = tc["function"]
            name = func["name"]
            try:
                args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError as e:
                tr = f"[错误] 参数JSON解析失败: {e}"
            else:
                tr = await _execute_tool(name, args)

            tool_results.append(f"[{name}]: {str(tr)[:200]}")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(tr)})
            _context_snapshot(uid, messages)

        if send_reply:
            await send_reply(f"⚙ 已执行 {len(tool_calls)} 个工具...")

    else:
        final = final or "达到最大执行轮数。已完成:\n" + "\n".join(tool_results[-5:])

    # 保存会话（记录 assistant 最终回复）
    msgs = _SESSION_MESSAGES.get(uid, [])
    if msgs:
        msgs.append({"role": "assistant", "content": final})
        _save_sessions()

    # 写记忆（异步后台，不阻塞）
    try:
        threading.Thread(target=_write_memory, args=(uid, user_message, final), daemon=True).start()
    except Exception:
        pass

    # 检测阻断场景：保存待办任务（用于登录后自动续接）
    blocker_keywords = ("登录", "扫码", "未登录", "未运行", "权限不足", "停止")
    if any(kw in final for kw in blocker_keywords):
        _save_pending_task(uid, original_message)
        log(f"[agent] 检测到阻断，已存待办")

    log(f"[agent] 完成: {round_n}轮 {len(final)}字")
    return final

# ════════════════════════════════════════════
# 对外接口（供 __init__.py /d c 调用）
# ════════════════════════════════════════════

# 启动时恢复动态工具
_restore_dynamic_tools()
_load_task_state()
_load_sessions()
_load_doc_cache()
try:
    threading.Thread(target=_ensure_project_index, daemon=True).start()
except Exception as e:
    log(f"[agent] 启动项目索引失败: {e}")

def on_agent_command(text: str, uid: int = 0) -> str:
    """同步入口：在后台线程运行 agent，返回结果文本"""
    try:
        return asyncio.run(process_agent_command(uid, text))
    except Exception as e:
        log(f"[agent] 异常: {traceback.format_exc()}")
        return f"智能体执行出错: {e}"

def clear_session(uid: int):
    """清除会话记忆（/d c clear）"""
    try:
        from .bridge.dc_memory import session_end
        session_end("手动清除")
        return "会话记忆已清除"
    except Exception as e:
        return f"清除失败: {e}"

def session_info(uid: int) -> str:
    """查看会话状态（/d c info）"""
    try:
        from .bridge.dc_memory import status_summary
        return status_summary()
    except Exception as e:
        return f"获取状态失败: {e}"
