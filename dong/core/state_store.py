"""
冬 · 状态存储
- register / get / set / atomic_update / flush
- 原子写入（write-tmp + fsync + os.replace）
- 损坏自动恢复（JSON 解析失败 → 备份 .bak → 从默认值恢复）
"""
from __future__ import annotations
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional
logger = logging.getLogger("dong.core.state_store")
__all__ = ["StateStore"]
# ────────────────────────────────────────────
# 状态存储
# ────────────────────────────────────────────
class StateStore:
    """
    持久化键值存储。
    使用方式：
        store = StateStore("/path/to/state.json")
        store.register("fatigue", default=50)
        store.register("mood",    default=60)
        store.get("fatigue")                    # → 50
        store.set("fatigue", 75)
        store.atomic_update("fatigue", lambda v: min(100, v + 5))
        store.flush()                           # 原子写入磁盘
    """
    def __init__(self, path: str):
        self._path = Path(path)
        self._defaults: Dict[str, Any] = {}
        self._data: Dict[str, Any] = {}
        self._dirty = False
        self._lock = threading.Lock()
        self._loaded = False
    # ════════════════ 注册 ════════════════
    def register(self, key: str, default: Any = None) -> None:
        """
        注册一个键及其默认值。
        可在 load 前多次调用，注册信息会用于损坏恢复。
        """
        with self._lock:
            self._defaults[key] = default
            # 如果尚未加载且 data 中没有该键，先填默认值
            if key not in self._data:
                self._data[key] = default
    # ════════════════ 读取 ════════════════
    def get(self, key: str, fallback: Any = None) -> Any:
        """获取键值，优先内存 → 已注册默认值 → fallback。"""
        self._ensure_loaded()
        with self._lock:
            if key in self._data:
                return self._data[key]
            if key in self._defaults:
                return self._defaults[key]
            return fallback
    # ════════════════ 写入 ════════════════
    def set(self, key: str, value: Any) -> None:
        """设置键值并标记脏。"""
        with self._lock:
            self._data[key] = value
            self._dirty = True
    def atomic_update(self, key: str, fn: Callable[[Any], Any]) -> Any:
        """
        原子读-改-写：在锁内读取旧值、调用 fn、写回新值。
        返回新值。
        """
        with self._lock:
            old = self._data.get(key, self._defaults.get(key))
            new = fn(old)
            self._data[key] = new
            self._dirty = True
            return new
    def setdefault(self, key: str, default: Any = None) -> Any:
        """若键不存在则写入默认值，返回当前值。"""
        with self._lock:
            if key not in self._data:
                self._data[key] = default
                self._dirty = True
            return self._data[key]
    def delete(self, key: str) -> bool:
        """删除键，返回是否实际删除了。"""
        with self._lock:
            if key in self._data:
                del self._data[key]
                self._dirty = True
                return True
            return False
    # ════════════════ 持久化 ════════════════
    def flush(self) -> None:
        """将脏数据原子写入磁盘。"""
        if not self._dirty:
            return
        with self._lock:
            snapshot = dict(self._data)
            # 写入期间不持锁（序列化可能慢），但标记清脏
            self._dirty = False
        self._atomic_write(snapshot)
    def force_flush(self) -> None:
        """无论是否脏都完整写入一次。"""
        with self._lock:
            snapshot = dict(self._data)
            self._dirty = False
        self._atomic_write(snapshot)
    # ════════════════ 加载 & 恢复 ════════════════
    def _ensure_loaded(self) -> None:
        """首次访问时懒加载。"""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._load()
            self._loaded = True
    def _load(self) -> None:
        """从磁盘加载，损坏时自动恢复。"""
        if not self._path.exists():
            self._data = dict(self._defaults)
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("状态文件 JSON 损坏: %s，尝试恢复", self._path)
            data = self._try_recover(raw)
            self._backup_corrupted()
        except Exception as e:
            logger.warning("状态文件读取异常: %s，使用默认值", e)
            data = None
            self._backup_corrupted()
        if isinstance(data, dict):
            # 合并：文件值覆盖默认值
            self._data = dict(self._defaults)
            self._data.update(data)
            self._dirty = True  # 补全可能缺失的键，需回写
        else:
            self._data = dict(self._defaults)
    def _try_recover(self, raw: str) -> Optional[Dict]:
        """
        尝试从损坏的 JSON 文本中恢复有效数据。
        策略：找最后一个完整 } 闭合的 JSON 对象前缀。
        """
        # 从末尾向前找最后一个 }
        last_brace = raw.rfind("}")
        if last_brace < 0:
            return None
        # 逐步尝试截断并解析
        for cut in range(last_brace, max(last_brace - 5000, -1), -1):
            candidate = raw[: cut + 1]
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    logger.info("恢复成功：截取前 %d 字符得到 %d 个键",
                                cut + 1, len(obj))
                    return obj
            except json.JSONDecodeError:
                continue
        logger.warning("恢复失败：无法从损坏文件中提取有效 JSON")
        return None
    def _backup_corrupted(self) -> None:
        """将损坏文件重命名为 .bak。"""
        bak = self._path.with_suffix(self._path.suffix + ".bak")
        try:
            if self._path.exists():
                self._path.replace(bak)
                logger.info("损坏文件已备份: %s → %s", self._path, bak)
        except Exception as e:
            logger.warning("备份损坏文件失败: %s", e)
    def _atomic_write(self, data: Dict[str, Any]) -> None:
        """
        原子写入：写临时文件 → fsync → os.replace。
        os.replace 在 POSIX 和 Windows 上都是原子操作。
        """
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            # 确保目录存在
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(self._path))
        except Exception:
            self._dirty = True  # 写入失败，重新标记脏
            logger.exception("原子写入失败: %s", self._path)
            # 清理临时文件
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise
    # ════════════════ 批量 & 查询 ════════════════
    def keys(self) -> list:
        self._ensure_loaded()
        with self._lock:
            return list(self._data.keys())
    def items(self) -> Dict[str, Any]:
        """返回快照（副本）。"""
        self._ensure_loaded()
        with self._lock:
            return dict(self._data)
    @property
    def dirty(self) -> bool:
        return self._dirty
    @property
    def path(self) -> str:
        return str(self._path)