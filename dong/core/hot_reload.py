"""
冬 · 热更新沙箱 — 子进程验证 + 自动回滚
优化器改完代码后：
1. 备份受影响文件
2. 写入新代码
3. 子进程验证（import + 健康检查 + 5s 超时）
4. 通过 → importlib.reload 热加载
5. 失败 → 自动回滚到备份版本
"""
from __future__ import annotations
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
logger = logging.getLogger("dong.core.hot_reload")
__all__ = ["ApplyResult", "HotReloader", "reloader"]
# ────────────────────────────────────────────
# 子进程验证脚本
# ────────────────────────────────────────────
_VERIFY_SCRIPT = r"""
import sys, json, traceback
def main():
    r = {"imports": [], "checks": [], "ok": True, "detail": ""}
    core = [
        "dong.config", "dong.status", "dong.memory",
        "dong.schedule", "dong.api", "dong.intimacy",
        "dong.expression", "dong.amygdala",
    ]
    for mod in core:
        try:
            __import__(mod)
            r["imports"].append({"module": mod, "ok": True})
        except Exception as e:
            r["imports"].append({"module": mod, "ok": False, "error": str(e)})
            r["ok"] = False
            r["detail"] += f"import fail: {mod} ({e}); "
    if r["ok"]:
        try:
            from dong.config import _get_cfg, API_CONFIGS
            cfg = _get_cfg("chat")
            if not cfg.api_key:
                r["ok"] = False
                r["detail"] += "api_key empty; "
            else:
                r["checks"].append({"name": "api_config", "ok": True})
        except Exception as e:
            r["ok"] = False
            r["detail"] += f"config check fail: {e}; "
    print(json.dumps(r, ensure_ascii=False))
    sys.exit(0 if r["ok"] else 1)
if __name__ == "__main__":
    main()
"""
# ────────────────────────────────────────────
# 结果
# ────────────────────────────────────────────
@dataclass
class ApplyResult:
    ok: bool
    rolled_back: bool = False
    reloaded: List[str] = field(default_factory=list)
    detail: str = ""
    verify_output: Optional[Dict] = None
# ────────────────────────────────────────────
# 热更新沙箱
# ────────────────────────────────────────────
class HotReloader:
    """
    完整流程：备份 → 写入 → 子进程验证 → 热加载 / 回滚。
    子进程隔离保证主进程安全；5s 超时防止挂死。
    """
    BACKUP_DIR = "__hot_reload_backup__"
    VERIFY_TIMEOUT = 5.0
    def __init__(self, base_dir: str = ""):
        self._base = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
        self._bak_dir = self._base / self.BACKUP_DIR
        self._bak_dir.mkdir(exist_ok=True)
        self._backups: Dict[str, str] = {}  # original → backup
    # ════════════ 备份 ════════════
    def backup(self, file_path: str) -> str:
        src = Path(file_path)
        if not src.exists():
            raise FileNotFoundError(file_path)
        ts = int(time.time() * 1000)
        bak = self._bak_dir / f"{src.stem}_{ts}{src.suffix}.bak"
        shutil.copy2(str(src), str(bak))
        self._backups[str(src)] = str(bak)
        logger.info("备份: %s → %s", src.name, bak.name)
        return str(bak)
    def backup_many(self, paths: List[str]):
        for p in paths:
            self.backup(p)
    # ════════════ 子进程验证 ════════════
    def verify_in_subprocess(self, timeout: float = None) -> Tuple[bool, Optional[Dict]]:
        timeout = timeout or self.VERIFY_TIMEOUT
        script = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        )
        script.write(_VERIFY_SCRIPT)
        script.close()
        try:
            proc = subprocess.run(
                [sys.executable, script.name],
                capture_output=True, text=True, timeout=timeout,
                cwd=str(self._base.parent),
                env={**os.environ, "PYTHONPATH": str(self._base.parent)},
            )
        except subprocess.TimeoutExpired:
            logger.error("子进程验证超时 (%.1fs)", timeout)
            return False, {"ok": False, "detail": f"timeout({timeout}s)"}
        finally:
            try:
                os.unlink(script.name)
            except Exception:
                pass
        output = None
        try:
            output = json.loads(proc.stdout.strip()) if proc.stdout.strip() else None
        except json.JSONDecodeError:
            pass
        ok = proc.returncode == 0
        if not ok:
            detail = (output or {}).get("detail", "") or (proc.stderr or "")[:200]
            logger.warning("子进程验证失败: %s", detail)
        return ok, output
    # ════════════ 热加载 ════════════
    def reload_module(self, module_name: str) -> bool:
        try:
            if module_name in sys.modules:
                importlib.reload(sys.modules[module_name])
            else:
                __import__(module_name)
            logger.info("热加载: %s", module_name)
            return True
        except Exception as e:
            logger.error("热加载失败 %s: %s", module_name, e)
            return False
    def reload_modules(self, names: List[str]) -> List[str]:
        return [n for n in names if self.reload_module(n)]
    # ════════════ 回滚 ════════════
    def rollback(self, file_paths: List[str] = None) -> int:
        targets = list(self._backups.keys()) if file_paths is None \
            else [str(p) for p in file_paths if str(p) in self._backups]
        rolled = 0
        for orig in targets:
            bak = self._backups.get(orig)
            if bak and os.path.exists(bak):
                shutil.copy2(bak, orig)
                mod = self._path_to_module(orig)
                if mod:
                    self.reload_module(mod)
                logger.info("回滚: %s", Path(orig).name)
                rolled += 1
        return rolled
    # ════════════ 完整流程 ════════════
    def verify_and_apply(
        self,
        changes: Dict[str, str],
        module_names: List[str] = None,
        timeout: float = None,
    ) -> ApplyResult:
        """
        完整流程：备份 → 写入 → 验证 → 热加载 / 回滚。
        Args:
            changes: {文件路径: 新内容}
            module_names: 要热加载的模块名（None 则自动推断）
            timeout: 子进程超时
        """
        result = ApplyResult(ok=False)
        paths = list(changes.keys())
        # 1. 备份
        try:
            self.backup_many(paths)
        except Exception as e:
            result.detail = f"备份失败: {e}"
            return result
        # 2. 写入
        for path, content in changes.items():
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                result.detail = f"写入失败 {path}: {e}"
                self.rollback(paths)
                result.rolled_back = True
                return result
        # 3. 子进程验证
        ok, output = self.verify_in_subprocess(timeout)
        result.verify_output = output
        if not ok:
            rolled = self.rollback(paths)
            result.rolled_back = rolled > 0
            detail = (output or {}).get("detail", "")
            result.detail = f"验证失败，已回滚{rolled}个文件: {detail}"
            logger.error("[FATAL] 热更新验证失败，已回滚: %s", detail)
            return result
        # 4. 热加载
        if module_names:
            result.reloaded = self.reload_modules(module_names)
        else:
            inferred = [mn for p in paths if (mn := self._path_to_module(p))]
            result.reloaded = self.reload_modules(inferred)
        result.ok = True
        result.detail = f"验证通过，已热加载 {len(result.reloaded)} 个模块"
        logger.info("[OK] 热更新成功: %s", result.detail)
        self._cleanup_old_backups()
        return result
    # ════════════ 工具 ════════════
    def _path_to_module(self, file_path: str) -> Optional[str]:
        try:
            rel = Path(file_path).resolve().relative_to(self._base.parent)
            parts = list(rel.parts)
            if parts[-1].endswith(".py"):
                parts[-1] = parts[-1][:-3]
            if parts[-1] == "__init__":
                parts = parts[:-1]
            return ".".join(parts)
        except Exception:
            return None
    def _cleanup_old_backups(self, max_age_hours: int = 24):
        now = time.time()
        for f in self._bak_dir.iterdir():
            if f.suffix == ".bak":
                if (now - f.stat().st_mtime) / 3600 > max_age_hours:
                    f.unlink(missing_ok=True)
# 全局单例
reloader = HotReloader()