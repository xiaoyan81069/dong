"""
冬 · 可选依赖管理
- require() 声明式注册
- 懒加载（首次访问时才检查/导入）
- all_status() 汇总全部依赖状态
- 支持 package / executable / module 三种类型
"""
from __future__ import annotations
import importlib
import logging
import shutil
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
logger = logging.getLogger("dong.core.optional_deps")
__all__ = ["LazyDep", "OptionalDeps", "deps"]
# ────────────────────────────────────────────
# 懒加载依赖代理
# ────────────────────────────────────────────
class LazyDep:
    """
    单个可选依赖的懒加载代理。
    不在注册时导入/检查，而是推迟到首次访问 .available / .module 时。
    用法：
        ffmpeg = deps.require("ffmpeg", kind="executable")
        # 此时还没检查任何东西
        if ffmpeg.available:       # ← 首次访问时才检查
            ...
    """
    def __init__(self, name: str, kind: str = "package",
                 import_name: Optional[str] = None,
                 min_version: Optional[str] = None,
                 fallback: Optional[Callable] = None):
        self._name        = name
        self._kind        = kind               # "package" | "executable" | "module"
        self._import_name = import_name or name
        self._min_version = min_version
        self._fallback    = fallback
        self._resolved  = False
        self._available = False
        self._module    = None       # 导入的模块对象（仅 package/module）
        self._path      = None       # 可执行文件路径（仅 executable）
        self._version   = None
        self._error     = None       # 解析失败时的异常消息
    # ─── 触发解析 ───
    def _resolve(self) -> None:
        if self._resolved:
            return
        try:
            if self._kind == "executable":
                self._resolve_executable()
            elif self._kind in ("package", "module"):
                self._resolve_package()
            else:
                self._error = f"未知 kind: {self._kind}"
        except Exception as e:
            self._available = False
            self._error = str(e)
        finally:
            self._resolved = True
    def _resolve_executable(self) -> None:
        found = shutil.which(self._import_name)
        if found:
            self._available = True
            self._path = found
        else:
            self._available = False
            self._error = f"可执行文件未找到: {self._import_name}"
    def _resolve_package(self) -> None:
        mod = importlib.import_module(self._import_name)
        self._module = mod
        self._available = True
        # 版本检查
        if self._min_version:
            ver = self._extract_version(mod)
            if ver:
                self._version = ver
                if not self._version_gte(ver, self._min_version):
                    self._available = False
                    self._error = (
                        f"版本过低: {ver} < {self._min_version}"
                    )
    # ─── 属性 ───
    @property
    def name(self) -> str:
        return self._name
    @property
    def available(self) -> bool:
        """是否可用（懒解析）。"""
        self._resolve()
        return self._available
    @property
    def module(self) -> Any:
        """导入的模块对象（仅 package/module，懒解析）。"""
        self._resolve()
        return self._module
    @property
    def path(self) -> Optional[str]:
        """可执行文件路径（仅 executable，懒解析）。"""
        self._resolve()
        return self._path
    @property
    def version(self) -> Optional[str]:
        self._resolve()
        return self._version
    @property
    def error(self) -> Optional[str]:
        self._resolve()
        return self._error
    # ─── 严格模式 ───
    def require(self) -> Any:
        """
        严格获取：不可用时抛出 ImportError。
        对于 package/module 返回模块对象；对于 executable 返回路径。
        """
        self._resolve()
        if not self._available:
            msg = f"可选依赖不可用: {self._name}"
            if self._error:
                msg += f" ({self._error})"
            if self._fallback:
                logger.warning("%s，使用 fallback", msg)
                return self._fallback()
            raise ImportError(msg)
        if self._kind == "executable":
            return self._path
        return self._module
    def or_else(self, fallback_fn: Callable) -> Any:
        """不可用时调用 fallback_fn 获取替代值。"""
        self._resolve()
        if self._available:
            return self._module if self._kind != "executable" else self._path
        return fallback_fn()
    # ─── 内部工具 ───
    @staticmethod
    def _extract_version(mod) -> Optional[str]:
        """从模块中提取版本字符串。"""
        for attr in ("__version__", "VERSION", "version"):
            v = getattr(mod, attr, None)
            if v is not None:
                return str(v)
        return None
    @staticmethod
    def _version_gte(actual: str, required: str) -> bool:
        """简单版本比较（按 . 分段数值比较）。"""
        def parts(s: str):
            nums = []
            for seg in s.split("."):
                try:
                    nums.append(int(seg))
                except ValueError:
                    nums.append(0)
            return nums
        return parts(actual) >= parts(required)
    def __repr__(self) -> str:
        state = "unresolved" if not self._resolved else (
            "available" if self._available else f"unavailable({self._error})"
        )
        return f"<LazyDep {self._name!r} {state}>"
# ────────────────────────────────────────────
# 依赖管理器
# ────────────────────────────────────────────
class OptionalDeps:
    """
    集中管理所有可选依赖。
    用法：
        deps = OptionalDeps()
        ffmpeg = deps.require("ffmpeg", kind="executable")
        ws     = deps.require("websockets", kind="package", min_version="10.0")
        # 查看全部状态
        deps.all_status()
    """
    def __init__(self):
        self._deps: Dict[str, LazyDep] = {}
        self._lock = threading.Lock()
    def require(self, name: str, *,
                kind: str = "package",
                import_name: Optional[str] = None,
                min_version: Optional[str] = None,
                fallback: Optional[Callable] = None) -> LazyDep:
        """
        声明一个可选依赖。返回 LazyDep 代理（尚未解析）。
        同名重复注册直接返回已有实例。
        """
        with self._lock:
            if name in self._deps:
                return self._deps[name]
            dep = LazyDep(
                name=name,
                kind=kind,
                import_name=import_name,
                min_version=min_version,
                fallback=fallback,
            )
            self._deps[name] = dep
            return dep
    def get(self, name: str) -> Optional[LazyDep]:
        """按名称获取已注册的依赖。"""
        with self._lock:
            return self._deps.get(name)
    def all_status(self) -> Dict[str, Dict[str, Any]]:
        """
        返回所有依赖的状态快照（触发全部解析）。
        返回格式：
        {
            "ffmpeg": {"available": True, "kind": "executable", "path": "/usr/bin/ffmpeg", ...},
            "websockets": {"available": True, "kind": "package", "version": "12.0", ...},
            ...
        }
        """
        with self._lock:
            names = list(self._deps.keys())
        result = {}
        for name in names:
            dep = self._deps[name]
            dep._resolve()  # 强制解析
            entry: Dict[str, Any] = {
                "available": dep._available,
                "kind": dep._kind,
                "error": dep._error,
            }
            if dep._version is not None:
                entry["version"] = dep._version
            if dep._path is not None:
                entry["path"] = dep._path
            if dep._min_version is not None:
                entry["min_version"] = dep._min_version
            result[name] = entry
        return result
    def summary(self) -> str:
        """人类可读的状态摘要。"""
        status = self.all_status()
        lines = []
        for name, info in status.items():
            icon = "✅" if info["available"] else "❌"
            detail = ""
            if info.get("version"):
                detail += f" v{info['version']}"
            if info.get("path"):
                detail += f" ({info['path']})"
            if info.get("error"):
                detail += f" — {info['error']}"
            lines.append(f"  {icon} {name}{detail}")
        return "\n".join(lines) if lines else "  (无已注册依赖)"
    @property
    def count(self) -> int:
        with self._lock:
            return len(self._deps)
# 全局单例
deps = OptionalDeps()