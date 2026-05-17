"""
冬 · 启动健康检查
- 在 main() 中 robot_loop() 之前执行
- 检查序列：配置完整性 → 数据自愈 → NapCat → API → 关键依赖
- 每项检查同时注册到 health_registry，之后可周期性复检
- AUTO_FIX 级检查在启动时自动修复；WARN 级仅记录；FATAL 级阻止启动
"""
from __future__ import annotations
import json
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from .health_registry import CheckLevel, HealthCheck, registry
from .data_healing import (
    STATUS_SCHEMA, LASTDAY_SCHEMA, CYCLE_SCHEMA,
    heal_status, heal_lastday, heal_cycle,
    loader as healing_loader,
)
from .optional_deps import deps
logger = logging.getLogger("dong.core.startup_check")
__all__ = [
    "CheckResult", "StartupReport",
    "run_startup_checks", "install_periodic_checks",
]
# ════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════
@dataclass
class CheckResult:
    name: str
    passed: bool
    level: CheckLevel
    detail: str = ""
    auto_fixed: bool = False       # 是否自动修复了
    repair_count: int = 0          # 自愈修复数
@dataclass
class StartupReport:
    results: List[CheckResult] = field(default_factory=list)
    started_at: float = 0.0
    duration_s: float = 0.0
    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)
    @property
    def has_fatal(self) -> bool:
        return any(not r.passed and r.level == CheckLevel.FATAL
                   for r in self.results)
    @property
    def has_warn(self) -> bool:
        return any(not r.passed and r.level == CheckLevel.WARN
                   for r in self.results)
    @property
    def total_repairs(self) -> int:
        return sum(r.repair_count for r in self.results)
    def summary(self) -> str:
        lines = ["--- Startup Check Report ---"]
        for r in self.results:
            icon = "OK" if r.passed else ("FIX" if r.auto_fixed else "FAIL")
            extra = f" (repaired {r.repair_count})" if r.repair_count else ""
            lines.append(f"  [{icon}] {r.name}: {r.detail}{extra}")
        lines.append(f"  time: {self.duration_s:.1f}s")
        return "\n".join(lines)
# ════════════════════════════════════════════
# 检查实现
# ════════════════════════════════════════════
# ── 配置路径（由 bind_config_paths 注入，解耦硬编码） ──
_paths: Dict[str, str] = {}
def bind_config_paths(**kwargs: str):
    """注入路径配置，避免硬编码依赖旧 config 模块。"""
    _paths.update(kwargs)
# ── 1. 环境配置检查 ──
def _check_env_config() -> CheckResult:
    """检查 .env / API keys 是否就绪。"""
    issues: List[str] = []
    env_path = _paths.get("env_file", "")
    if env_path and not os.path.exists(env_path):
        issues.append(".env 文件不存在")
    # 检查关键环境变量
    key_names = ["DONG_API_KEY_ARK", "DONG_API_KEY_LONGCAT"]
    missing_keys = [k for k in key_names if not os.environ.get(k)]
    if missing_keys:
        issues.append(f"环境变量缺失: {', '.join(missing_keys)}")
    if issues:
        return CheckResult("env_config", False, CheckLevel.WARN,
                           "; ".join(issues))
    return CheckResult("env_config", True, CheckLevel.WARN, "配置就绪")
# ── 2. 数据文件自愈 ──
def _check_data_integrity() -> CheckResult:
    """加载并自愈所有数据 JSON 文件。"""
    total_repairs = 0
    details: List[str] = []
    file_schemas = [
        ("status_file",  "dong_status.json",   STATUS_SCHEMA,  heal_status),
        ("lastday_file", "dong_lastday.json",  LASTDAY_SCHEMA, heal_lastday),
        ("cycle_file",   "dong_cycle.json",    CYCLE_SCHEMA,   heal_cycle),
    ]
    for path_key, label, schema, heal_fn in file_schemas:
        fpath = _paths.get(path_key, "")
        if not fpath:
            # 没注入路径 → 尝试按标签在当前目录查找
            fpath = os.path.join(os.path.dirname(__file__), "..", label)
        data, repairs = healing_loader.load(fpath, schema)
        if repairs:
            total_repairs += len(repairs)
            details.append(f"{label} 修复{len(repairs)}处")
        else:
            details.append(f"{label} 正常")
    detail = "; ".join(details)
    return CheckResult(
        "data_integrity", True, CheckLevel.AUTO_FIX,
        detail, auto_fixed=total_repairs > 0, repair_count=total_repairs,
    )
# ── 3. NapCat 连通性 ──
def _is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False
def _check_napcat() -> CheckResult:
    """检查 NapCat WS 端口是否可达，不可达则尝试启动。"""
    host = _paths.get("napcat_host", "127.0.0.1")
    port = int(_paths.get("napcat_port", "3001"))
    napcat_dir = _paths.get("napcat_dir", "")
    if _is_port_open(host, port):
        return CheckResult("napcat", True, CheckLevel.AUTO_FIX,
                           f"端口 {port} 已开放")
    # 尝试自动启动
    if napcat_dir and os.path.isdir(napcat_dir):
        node_exe = os.path.join(napcat_dir, "node.exe")
        index_js = os.path.join(napcat_dir, "index.js")
        if os.path.exists(node_exe) and os.path.exists(index_js):
            try:
                subprocess.Popen(
                    [node_exe, "--max-old-space-size=256", "./index.js", "-q", os.environ.get("NAPCAT_QQ", "2020382280")],
                    cwd=napcat_dir,
                    **({"creationflags": 0x08000000} if sys.platform == "win32" else {}),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info("NapCat 启动命令已发送，等待就绪...")
                for _ in range(30):
                    time.sleep(1)
                    if _is_port_open(host, port):
                        return CheckResult("napcat", True, CheckLevel.AUTO_FIX,
                                           f"自动启动成功，端口 {port} 已开放",
                                           auto_fixed=True)
                return CheckResult("napcat", False, CheckLevel.AUTO_FIX,
                                   "启动命令已发送但 30s 内端口未开放")
            except Exception as e:
                return CheckResult("napcat", False, CheckLevel.AUTO_FIX,
                                   f"自动启动失败: {e}")
    return CheckResult("napcat", False, CheckLevel.AUTO_FIX,
                       f"端口 {port} 不可达，无启动目录配置")
# ── 4. API 连通性 ──
def _check_api() -> CheckResult:
    """对主力 chat Provider 做冒烟测试。"""
    try:
        from .config_naming import registry as cfg_registry
        primary = cfg_registry.get_primary("chat")
    except Exception:
        primary = None
    if primary is None:
        return CheckResult("api", False, CheckLevel.WARN,
                           "无可用 chat Provider 配置")
    # 冒烟测试
    try:
        import requests
        r = requests.post(
            f"{primary.api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {primary.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": primary.model,
                "temperature": 0.1,
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=10,
        )
        if r.status_code == 200:
            return CheckResult("api", True, CheckLevel.WARN,
                               f"主力 Provider '{primary.name}' 连通正常")
        if r.status_code == 429:
            return CheckResult("api", True, CheckLevel.WARN,
                               f"主力 Provider '{primary.name}' 限流中(429)，端点可达")
        return CheckResult("api", False, CheckLevel.WARN,
                           f"主力 Provider 返回 HTTP {r.status_code}")
    except Exception as e:
        return CheckResult("api", False, CheckLevel.WARN,
                           f"主力 Provider 不可达: {e}")
# ── 5. 关键依赖 ──
def _check_critical_deps() -> CheckResult:
    """检查关键可选依赖。"""
    # 确保已注册
    if deps.count == 0:
        _register_default_deps()
    status = deps.all_status()
    critical = ["ffmpeg", "websockets"]
    missing: List[str] = []
    for name in critical:
        dep = deps.get(name)
        if dep and not dep.available:
            missing.append(name)
    if missing:
        return CheckResult("critical_deps", False, CheckLevel.WARN,
                           f"缺失关键依赖: {', '.join(missing)}")
    return CheckResult("critical_deps", True, CheckLevel.WARN, "关键依赖就绪")
def _register_default_deps():
    """注册冬项目的标准可选依赖。"""
    deps.require("ffmpeg",      kind="executable", import_name="ffmpeg")
    deps.require("ffprobe",     kind="executable", import_name="ffprobe")
    deps.require("websockets",  kind="package",    import_name="websockets", min_version="10.0")
    deps.require("requests",    kind="package",    import_name="requests")
    deps.require("openai",      kind="package",    import_name="openai")
    deps.require("PIL",         kind="package",    import_name="Pillow")
# ════════════════════════════════════════════
# 启动检查主流程
# ════════════════════════════════════════════
# 检查序列定义：(name, check_fn, level, interval_for_periodic)
_CHECK_SEQUENCE: List[Tuple[str, Callable[[], CheckResult], CheckLevel, float]] = [
    ("env_config",      _check_env_config,     CheckLevel.WARN,     300),
    ("data_integrity",  _check_data_integrity,  CheckLevel.AUTO_FIX, 60),
    ("napcat",          _check_napcat,          CheckLevel.AUTO_FIX, 30),
    ("api",             _check_api,             CheckLevel.WARN,     120),
    ("critical_deps",   _check_critical_deps,   CheckLevel.WARN,     600),
]
def run_startup_checks() -> StartupReport:
    """
    执行启动检查序列。
    在 main() 中 robot_loop() 之前调用：
        report = run_startup_checks()
        log(report.summary())
        if report.has_fatal:
            return  # 阻止启动
    同时将所有检查注册到 health_registry，供后续周期性复检。
    """
    report = StartupReport(started_at=time.monotonic())
    logger.info("═══ 启动健康检查 ═══")
    for name, check_fn, level, interval in _CHECK_SEQUENCE:
        try:
            result = check_fn()
        except Exception as e:
            logger.exception("启动检查异常: %s", name)
            result = CheckResult(name, False, level, f"检查异常: {e}")
        # 确保结果与预期等级一致
        result.level = level
        report.results.append(result)
        icon = "OK" if result.passed else ("FIX" if result.auto_fixed else "FAIL")
        logger.info("  [%s] %s: %s", icon, name, result.detail or "")
        # 注册到 health_registry（供周期性复检）
        _register_periodic(name, check_fn, level, interval)
    report.duration_s = time.monotonic() - report.started_at
    # 历史对比：与上次启动对比差异
    _compare_and_append(report)
    logger.info("═══ 启动检查完成 %.1fs ═══", report.duration_s)
    return report
def _register_periodic(name: str, check_fn: Callable[[], CheckResult],
                       level: CheckLevel, interval: float):
    """将启动检查注册为 health_registry 的周期性检查。"""
    # 包装：CheckResult → bool
    def _wrapper() -> bool:
        try:
            result = check_fn()
            return result.passed
        except Exception:
            return False
    # 如果已注册则跳过
    if registry.get_check(name) is not None:
        return
    # AUTO_FIX 级：生成 auto_fix 函数（再次运行检查，看能否修复）
    auto_fix_fn = None
    if level == CheckLevel.AUTO_FIX:
        def _auto_fix() -> bool:
            try:
                result = check_fn()
                return result.passed
            except Exception:
                return False
        auto_fix_fn = _auto_fix
    registry.register(name, _wrapper, interval=interval,
                      level=level, auto_fix=auto_fix_fn)
def install_periodic_checks():
    """
    仅注册周期性检查（不执行启动序列）。
    适用于已在其他地方完成启动检查、只需注册定期复检的场景。
    """
    for name, check_fn, level, interval in _CHECK_SEQUENCE:
        _register_periodic(name, check_fn, level, interval)
# ════════════════════════════════════════════
# 集成辅助：注入路径的快捷方式
# ════════════════════════════════════════════
def bind_from_config_module():
    """
    从旧 config 模块读取路径并注入。
    在 main() 中调用：
        from .core.startup_check import bind_from_config_module
        bind_from_config_module()
    """
    try:
        from .config import (
            STATUS_FILE, LAST_DAY_FILE, CYCLE_FILE,
            NAPCAT_DIR, BASE_DIR,
        )
        bind_config_paths(
            status_file  = STATUS_FILE,
            lastday_file = LAST_DAY_FILE,
            cycle_file   = CYCLE_FILE,
            napcat_dir   = NAPCAT_DIR,
            napcat_host  = "127.0.0.1",
            napcat_port  = "3001",
            env_file     = os.path.join(BASE_DIR, ".env"),
        )
    except ImportError:
        logger.warning("无法从 config 模块导入路径，使用默认值")
        bind_config_paths(
            napcat_host = "127.0.0.1",
            napcat_port = "3001",
        )

# ============ 健康历史 ============
_HEALTH_HISTORY_FILE = Path(__file__).parent.parent / "dong_health_history.jsonl"

def _compare_and_append(report: StartupReport):
    """与上次启动对比差异，并追加健康记录"""
    last = _read_last_health()
    if last:
        last_checks = {r["name"]: r for r in last.get("results", [])}
        diffs = []
        for cr in report.results:
            lr = last_checks.get(cr.name)
            if not lr:
                continue
            was_ok = lr.get("passed", True)
            if was_ok and not cr.passed:
                diffs.append(f"{cr.name}: OK→FAIL")
            elif not was_ok and cr.passed:
                diffs.append(f"{cr.name}: FAIL→OK")
        if diffs:
            logger.info("健康对比: %s", "; ".join(diffs))
    _append_health_record(report)

def _read_last_health() -> dict:
    """读取最后一条健康记录（从文件末尾反向查找，避免全量加载）。"""
    try:
        if not _HEALTH_HISTORY_FILE.exists():
            return {}
        with open(_HEALTH_HISTORY_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return {}
            # 从末尾反向读取，找最后一个完整JSON行
            chunk_size = 4096
            buf = b""
            pos = size
            while pos > 0 and b"\n" not in buf:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                buf = f.read(read_size) + buf
            # 取最后一个换行后的内容
            lines = buf.split(b"\n")
            for line in reversed(lines):
                line = line.strip()
                if line:
                    return json.loads(line.decode("utf-8"))
    except Exception:
        pass
    return {}

def _append_health_record(report: StartupReport):
    try:
        _HEALTH_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().isoformat(),
            "type": "startup",
            "results": [{"name": r.name, "passed": r.passed, "level": r.level.name,
                         "detail": r.detail} for r in report.results],
            "has_fatal": report.has_fatal, "duration_s": round(report.duration_s, 1),
        }
        with open(_HEALTH_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        # 只保留最近100条，防止文件无限膨胀
        _truncate_history(100)
    except Exception:
        pass

_MAX_HISTORY = 100
def _truncate_history(max_lines: int = _MAX_HISTORY):
    """保留最后 max_lines 条健康记录。"""
    try:
        if not _HEALTH_HISTORY_FILE.exists():
            return
        with open(_HEALTH_HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            with open(_HEALTH_HISTORY_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-max_lines:])
    except Exception:
        pass