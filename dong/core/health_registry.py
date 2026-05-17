"""
冬 · 健康检查注册表
- @register_check 装饰器声明检查项
- get_checks_due() 返回到期待执行的检查
- AUTO_FIX / WARN / FATAL 三级处理
- 支持自动修复函数
"""
from __future__ import annotations
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional
logger = logging.getLogger("dong.core.health_registry")
__all__ = ["CheckLevel", "HealthCheck", "HealthRegistry", "registry", "register_check"]
# ────────────────────────────────────────────
# 检查等级
# ────────────────────────────────────────────
class CheckLevel(Enum):
    AUTO_FIX = auto()   # 尝试自动修复，修复失败升级为 WARN
    WARN     = auto()   # 仅记录警告
    FATAL    = auto()   # 记录错误，标记系统不可用
# ────────────────────────────────────────────
# 检查项描述
# ────────────────────────────────────────────
@dataclass
class HealthCheck:
    name: str                                # 唯一名称
    fn: Callable[[], bool]                   # 检查函数，返回 True=健康
    interval: float                          # 检查间隔（秒）
    level: CheckLevel                        # 失败等级
    auto_fix: Optional[Callable[[], bool]] = None   # 自动修复函数
    last_run: float = 0.0                    # 上次执行时间戳
    last_result: Optional[bool] = None       # 上次检查结果
    consecutive_failures: int = 0            # 连续失败次数
    @property
    def is_due(self) -> bool:
        return (time.monotonic() - self.last_run) >= self.interval
# ────────────────────────────────────────────
# 健康注册表
# ────────────────────────────────────────────
class HealthRegistry:
    """
    集中管理所有健康检查项。
    使用方式：
        @register_check("napcat_ws", interval=30, level=CheckLevel.AUTO_FIX,
                        auto_fix=_reconnect_napcat)
        def check_napcat():
            return is_port_open(3001)
        # 周期性调用
        for chk in registry.get_checks_due():
            registry.run_check(chk)
    """
    # 连续失败升级阈值
    _WARN_THRESHOLD  = 2
    _FATAL_THRESHOLD = 5
    def __init__(self):
        self._checks: Dict[str, HealthCheck] = {}
        self._lock = threading.Lock()
        # FATAL状态由has_fatal属性动态计算
    # ════════════════ 注册 ════════════════
    def register(self, name: str, fn: Callable[[], bool], *,
                 interval: float = 60.0,
                 level: CheckLevel = CheckLevel.WARN,
                 auto_fix: Optional[Callable[[], bool]] = None) -> None:
        """注册一个健康检查项。"""
        with self._lock:
            self._checks[name] = HealthCheck(
                name=name,
                fn=fn,
                interval=interval,
                level=level,
                auto_fix=auto_fix,
            )
    # ════════════════ 查询 ════════════════
    def get_checks_due(self) -> List[HealthCheck]:
        """返回所有到期待执行的检查项。"""
        with self._lock:
            return [c for c in self._checks.values() if c.is_due]
    def get_all(self) -> List[HealthCheck]:
        """返回所有已注册的检查项。"""
        with self._lock:
            return list(self._checks.values())
    def get_check(self, name: str) -> Optional[HealthCheck]:
        with self._lock:
            return self._checks.get(name)
    @property
    def has_fatal(self) -> bool:
        """实时计算：是否有任何检查处于 FATAL 失败状态"""
        with self._lock:
            for chk in self._checks.values():
                if chk.consecutive_failures >= self._FATAL_THRESHOLD:
                    return True
                if chk.level == CheckLevel.FATAL and chk.last_result is False:
                    return True
        return False
    # ════════════════ 执行 ════════════════
    def run_check(self, check: HealthCheck) -> bool:
        """
        执行单个检查，按等级处理失败。
        返回检查是否通过。
        """
        try:
            ok = check.fn()
        except Exception:
            logger.exception("健康检查异常 (name=%s)", check.name)
            ok = False
        # 加锁保护状态修改（修复竞态条件）
        with self._lock:
            check.last_run = time.monotonic()
            check.last_result = ok
            if ok:
                if check.consecutive_failures > 0:
                    logger.info("健康检查恢复 (name=%s, 之前连续失败%d次)",
                                check.name, check.consecutive_failures)
                check.consecutive_failures = 0
                return True
            # ── 失败处理 ──
            check.consecutive_failures += 1
        effective_level = self._resolve_level(check)
        logger.warning("健康检查失败 (name=%s, level=%s, 连续%d次)",
                       check.name, effective_level.name,
                       check.consecutive_failures)
        if effective_level == CheckLevel.AUTO_FIX:
            self._try_auto_fix(check)
        elif effective_level == CheckLevel.WARN:
            pass
        elif effective_level == CheckLevel.FATAL:
            logger.error("[FATAL] 健康检查: %s (连续失败%d次)",
                         check.name, check.consecutive_failures)
        return False
    def run_all_due(self) -> Dict[str, bool]:
        """执行所有到期检查，返回 {name: passed}。"""
        results = {}
        for chk in self.get_checks_due():
            results[chk.name] = self.run_check(chk)
        return results
    # ════════════════ 内部 ════════════════
    def _resolve_level(self, check: HealthCheck) -> CheckLevel:
        """根据连续失败次数升级等级。"""
        base = check.level
        cf = check.consecutive_failures
        if base == CheckLevel.AUTO_FIX:
            if cf >= self._FATAL_THRESHOLD:
                return CheckLevel.FATAL
            if cf >= self._WARN_THRESHOLD:
                return CheckLevel.WARN
            return CheckLevel.AUTO_FIX
        if base == CheckLevel.WARN:
            if cf >= self._FATAL_THRESHOLD:
                return CheckLevel.FATAL
            return CheckLevel.WARN
        # FATAL 直接返回
        return CheckLevel.FATAL
    def _try_auto_fix(self, check: HealthCheck) -> None:
        """尝试自动修复。"""
        if check.auto_fix is None:
            logger.debug("无 auto_fix 函数 (name=%s)", check.name)
            return
        logger.info("尝试自动修复 (name=%s)...", check.name)
        try:
            fixed = check.auto_fix()
            if fixed:
                logger.info("自动修复成功 (name=%s)", check.name)
                # 修复后立即重验
                recheck = check.fn()
                if recheck:
                    with self._lock:
                        check.consecutive_failures = 0
                        check.last_result = True
                    logger.info("修复后重验通过 (name=%s)", check.name)
                else:
                    logger.warning("修复后重验仍失败 (name=%s)", check.name)
            else:
                logger.warning("自动修复返回失败 (name=%s)", check.name)
        except Exception:
            logger.exception("自动修复异常 (name=%s)", check.name)
    # ════════════════ 状态导出 ════════════════
    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """导出所有检查项当前状态（供仪表盘读取）。"""
        with self._lock:
            return {
                name: {
                    "level": chk.level.name,
                    "last_result": chk.last_result,
                    "consecutive_failures": chk.consecutive_failures,
                    "last_run_ago": round(time.monotonic() - chk.last_run, 1)
                                    if chk.last_run > 0 else None,
                    "interval": chk.interval,
                    "has_auto_fix": chk.auto_fix is not None,
                }
                for name, chk in self._checks.items()
            }
# ────────────────────────────────────────────
# 全局单例 & 装饰器
# ────────────────────────────────────────────
registry = HealthRegistry()
def register_check(name: str, *,
                   interval: float = 60.0,
                   level: CheckLevel = CheckLevel.WARN,
                   auto_fix: Optional[Callable[[], bool]] = None):
    """
    装饰器：注册健康检查函数。
    用法：
        @register_check("disk_space", interval=300, level=CheckLevel.WARN)
        def check_disk():
            return shutil.disk_usage("/").free > 1_000_000_000
    """
    def decorator(fn: Callable[[], bool]) -> Callable[[], bool]:
        registry.register(name, fn, interval=interval, level=level, auto_fix=auto_fix)
        return fn
    return decorator


# 注册质量漂移检查
def _check_quality_drift() -> bool:
    try:
        from .quality_monitor import check_quality_drift
        return check_quality_drift() is None
    except Exception:
        return True

registry.register("quality_drift", _check_quality_drift, interval=21600, level=CheckLevel.WARN)