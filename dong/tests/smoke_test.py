"""
冬 · 烟雾测试 — 启动时快速验证核心功能可用
所有检查项注册到 health_registry，周期性自动执行。
运行: python -m dong.tests.smoke_test
"""
import importlib
import json as _json
import logging
import os
import socket
import sys

import requests

from ..core.health_registry import CheckLevel, register_check

logger = logging.getLogger("dong.tests.smoke_test")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ═══════════════════ 环境检查 ═══════════════════

@register_check("smoke_env", interval=3600, level=CheckLevel.FATAL)
def check_env() -> bool:
    env_path = os.path.join(BASE, ".env")
    if not os.path.exists(env_path):
        logger.error(".env 文件缺失")
        return False
    for k in ("DONG_API_KEY_ARK", "DONG_API_KEY_LONGCAT", "DONG_API_KEY_QWEN"):
        val = os.environ.get(k, "")
        if not val or len(val) <= 5:
            logger.error("环境变量 %s 缺失或过短", k)
            return False
    return True


# ═══════════════════ 数据文件完整性 ═══════════════════

_DATA_FILES = [
    "dong_status.json", "dong_memory.json", "dong_intimacy.json",
    "dong_cycle.json", "dong_factory_archive.json",
    "dong_style_fingerprint.json", "dong_factory_hashes.json",
]


@register_check("smoke_data_files", interval=3600, level=CheckLevel.WARN)
def check_data_files() -> bool:
    ok = True
    for f in _DATA_FILES:
        path = os.path.join(BASE, f)
        if not os.path.exists(path):
            logger.warning("数据文件缺失: %s", f)
            ok = False
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                _json.load(fh)
        except Exception:
            logger.warning("数据文件损坏: %s", f)
            ok = False
    return ok


# ═══════════════════ 人设文件 ═══════════════════

@register_check("smoke_persona", interval=3600, level=CheckLevel.FATAL)
def check_persona() -> bool:
    path = os.path.join(BASE, "characters", "dong.persona.txt")
    try:
        txt = open(path, encoding="utf-8").read()
        return len(txt) > 100
    except Exception:
        logger.error("人设文件不可读: %s", path)
        return False


# ═══════════════════ 模块导入（dong/）═══════════════════

_DONG_MODULES = [
    "dong.config", "dong.log", "dong.api", "dong.status",
    "dong.memory", "dong.schedule", "dong.interaction",
    "dong.persona", "dong.media", "dong.expression",
    "dong.factory", "dong.grudge", "dong.amygdala",
    "dong.intimacy", "dong.overwhelm", "dong.decision",
    "dong.sleep_guardian", "dong.conflict_tracker",
    "dong.regret", "dong.tools", "dong.command_channel",
    "dong.agent_loop", "dong.gui_agent", "dong.screen_guard",
    "dong.update", "dong.dashboard", "dong.dong_connector",
    "dong.finance", "dong.mail", "dong.game",
    "dong.dialogue_evaluator", "dong.style_fingerprint",
    "dong.optimizer",
]

_CORE_MODULES = [
    "dong.core.state_store", "dong.core.health_registry",
    "dong.core.startup_check", "dong.core.data_healing",
    "dong.core.api_gateway", "dong.core.config_naming",
    "dong.core.event_bus", "dong.core.optional_deps",
    "dong.core.hot_reload", "dong.core.skill_memory",
    "dong.core.logging_setup", "dong.core.module_loader",
]


@register_check("smoke_imports", interval=3600, level=CheckLevel.FATAL)
def check_imports() -> bool:
    ok = True
    for m in _DONG_MODULES + _CORE_MODULES:
        try:
            importlib.import_module(m)
        except Exception as e:
            logger.error("模块导入失败 %s: %s", m, e)
            ok = False
    return ok


# ═══════════════════ Agent Loop 功能检查 ═══════════════════

def _check_notepad_content() -> bool:
    """验证 notepad 窗口存在，且包含文本内容。"""
    try:
        import uiautomation as _uia
    except ImportError:
        return False
    notepad = _uia.WindowControl(searchDepth=1, ClassName="Notepad")
    if not notepad.Exists(maxSearchSeconds=2):
        return False
    edit = notepad.EditControl(searchDepth=3)
    if not edit.Exists():
        return False
    text = edit.GetValuePattern().Value if edit.GetValuePattern() else ""
    return len(text.strip()) >= 1


def _run_agent_e2e(task_desc: str, verify_fn) -> bool:
    """通用 Agent 端到端测试：执行任务 → 验证结果。verify_fn 返回 True/False。"""
    import asyncio, subprocess
    try:
        subprocess.run("taskkill /f /im notepad.exe", shell=True, capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        from dong.agent_loop import run_computer_task
        result = asyncio.run(run_computer_task(task_desc, 0))
        if not result or result.get("type") != "done":
            logger.warning("Agent 任务未完成: %s → %s", task_desc, result)
            return False
        return verify_fn()
    except Exception as e:
        logger.warning("Agent E2E 异常: %s", e)
        return False


@register_check("smoke_agent_notepad", interval=7200, level=CheckLevel.WARN)
def check_agent_notepad() -> bool:
    """端到端Agent测试（静默模式：最小化运行，不干扰桌面）"""
    import asyncio, subprocess
    try:
        from dong.agent_loop import run_computer_task
        result = asyncio.run(run_computer_task("打开记事本输入agent测试", 0))
        if not result or result.get("type") != "done":
            logger.warning("Agent任务未完成: %s", result)
            return False
        # 静默：关闭测试记事本
        subprocess.run("taskkill /f /im notepad.exe", shell=True, capture_output=True, timeout=5)
        return True
    except Exception as e:
        logger.warning("Agent E2E异常: %s", e)
        return False


@register_check("smoke_agent_plan", interval=7200, level=CheckLevel.WARN)
def check_agent_plan() -> bool:
    """验证 Agent Loop 任务规划可用"""
    try:
        from dong.agent_loop import _local_match
        result = _local_match("打开记事本输入测试")
        return result and any("launch" in a for a in result) and any("type" in a for a in result)
    except Exception:
        return False


# ═══════════════════ Pet 连接检查 ═══════════════════

PET_PING_URL = "http://127.0.0.1:5120/api/ping"


@register_check("smoke_pet_connector", interval=120, level=CheckLevel.WARN)
def check_pet_connector() -> bool:
    """验证前端连接器 :5120 可访问（Pet 桌宠依赖此服务）。"""
    try:
        r = requests.get(PET_PING_URL, timeout=2)
        return r.status_code == 200
    except Exception:
        logger.warning("Pet 连接器不可达: %s", PET_PING_URL)
        return False


# ═══════════════════ CLI 入口 ═══════════════════

def main() -> int:
    from ..core.health_registry import registry

    print("===== 冬 · Smoke Test =====")
    passed, failed = 0, 0
    for chk in registry.get_all():
        ok = registry.run_check(chk)
        if ok:
            passed += 1
            print(f"  [OK] {chk.name}")
        else:
            failed += 1
            print(f"  [FAIL] {chk.name}")
    print(f"\n===== {passed} passed, {failed} failed =====")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
