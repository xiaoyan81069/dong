"""
冬 · 数据自愈
- 声明式 Schema 定义每个 JSON 文件的结构
- heal() 验证 + 修复：缺失→补默认、类型错→转换/重置、越界→夹紧、非法值→回退
- 每个 load_*() 调用 heal 后再使用数据，实现运行时自愈
- SelfHealingLoader：封装 load→heal→自动回写 一步到位
"""
from __future__ import annotations
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Type
logger = logging.getLogger("dong.core.data_healing")
__all__ = [
    "FieldSpec", "Repair", "heal",
    "STATUS_SCHEMA", "LASTDAY_SCHEMA", "CYCLE_SCHEMA",
    "heal_status", "heal_lastday", "heal_cycle",
    "SelfHealingLoader", "loader",
]
# ════════════════════════════════════════════
# 类型匹配辅助（处理 bool/int 歧义）
# ════════════════════════════════════════════
def _type_matches(value: Any, expected: Type) -> bool:
    """isinstance 的安全版本：bool 不算 int，int 不算 bool。"""
    if expected is int and isinstance(value, bool):
        return False
    if expected is bool and isinstance(value, int) and not isinstance(value, bool):
        return False
    return isinstance(value, expected)
def _try_cast(value: Any, target: Type) -> Tuple[bool, Any]:
    """尝试类型转换，返回 (成功, 转换后值)。"""
    try:
        if target is bool:
            # 字符串 "true"/"false" → bool
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "1", "yes"):
                    return True, True
                if low in ("false", "0", "no", ""):
                    return True, False
            if isinstance(value, (int, float)):
                return True, bool(value)
            return False, None
        if target is int and isinstance(value, float):
            return True, int(value)
        if target is int and isinstance(value, str):
            s = value.strip()
            if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
                return True, int(s)
            return False, None
        if target is float and isinstance(value, (int, str)):
            return True, float(value)
        if target is str:
            return True, str(value)
        return False, None
    except (ValueError, TypeError):
        return False, None
# ════════════════════════════════════════════
# Schema & Repair
# ════════════════════════════════════════════
@dataclass
class FieldSpec:
    """单个字段的 schema 描述。"""
    type: Type                            # 期望类型
    default: Any                          # 缺失/不可修复时的默认值
    range: Optional[Tuple[float, float]] = None   # (min, max) 数值越界夹紧
    choices: Optional[List[Any]]   = None         # 允许值列表
    children: Optional[Dict[str, 'FieldSpec']] = None  # 嵌套 dict 的子 schema
    nullable: bool = False                       # 是否允许 None
@dataclass
class Repair:
    """单次修复记录。"""
    path: str          # "hormones.dopamine"
    issue: str         # "missing" / "null" / "wrong_type" / "out_of_range" / "invalid_choice"
    old: Any           # 修复前的值（MISSING 表示键不存在）
    new: Any           # 修复后的值
    def __str__(self) -> str:
        return f"{self.path}: {self.issue} ({self.old!r} → {self.new!r})"
_MISSING = object()  # 哨兵：键不存在
# ════════════════════════════════════════════
# 核心 heal 函数
# ════════════════════════════════════════════
def heal(data: Dict[str, Any],
         schema: Dict[str, FieldSpec],
         path: str = "") -> Tuple[Dict[str, Any], List[Repair]]:
    """
    对 data 按 schema 进行验证 + 修复。
    规则：
      1. 缺失键 → 补 default，记录 "missing"
      2. None 值 → nullable 则保留，否则补 default，记录 "null"
      3. 类型不匹配 → 尝试 cast → 失败则补 default，记录 "wrong_type"
      4. 数值越界 → 夹紧到 range，记录 "out_of_range"
      5. 不在 choices → 补 default，记录 "invalid_choice"
      6. 有 children → 递归 heal 子 dict
    返回 (healed_data, repairs)。
    data 本身会被原地修改并返回；如需保留原数据请先 deepcopy。
    """
    if not isinstance(data, dict):
        # data 不是 dict（可能是 list/str/None）→ 整体替换为空 dict
        return {}, [Repair(path or "<root>", "not_dict", data, {})]
    repairs: List[Repair] = []
    for fname, spec in schema.items():
        fpath = f"{path}{fname}" if not path else f"{path}.{fname}"
        # ── 1. 缺失 ──
        if fname not in data:
            data[fname] = spec.default
            repairs.append(Repair(fpath, "missing", _MISSING, spec.default))
            continue
        value = data[fname]
        # ── 2. None ──
        if value is None:
            if spec.nullable:
                continue
            data[fname] = spec.default
            repairs.append(Repair(fpath, "null", None, spec.default))
            continue
        # ── 3. 类型检查 ──
        if not _type_matches(value, spec.type):
            ok, cast_val = _try_cast(value, spec.type)
            if ok:
                data[fname] = cast_val
                repairs.append(Repair(fpath, "wrong_type", value, cast_val))
                value = cast_val
            else:
                data[fname] = spec.default
                repairs.append(Repair(fpath, "wrong_type", value, spec.default))
                continue
        # ── 4. 数值越界 ──
        if spec.range is not None and isinstance(value, (int, float)):
            lo, hi = spec.range
            if value < lo or value > hi:
                clamped = max(lo, min(hi, value))
                data[fname] = clamped
                repairs.append(Repair(fpath, "out_of_range", value, clamped))
                value = clamped
        # ── 5. choices 校验 ──
        if spec.choices is not None and value not in spec.choices:
            data[fname] = spec.default
            repairs.append(Repair(fpath, "invalid_choice", value, spec.default))
            continue
        # ── 6. 递归 heal 子 dict ──
        if spec.children is not None:
            if isinstance(value, dict):
                healed_child, child_repairs = heal(value, spec.children, fpath)
                data[fname] = healed_child
                repairs.extend(child_repairs)
            else:
                # children 要求 dict，但值不是 dict → 用默认子结构
                default_child = _build_default_dict(spec.children)
                data[fname] = default_child
                repairs.append(Repair(fpath, "wrong_type", value, default_child))
    return data, repairs
def _build_default_dict(schema: Dict[str, FieldSpec]) -> Dict[str, Any]:
    """从 schema 构建全默认值 dict（递归）。"""
    result = {}
    for fname, spec in schema.items():
        if spec.children is not None:
            result[fname] = _build_default_dict(spec.children)
        else:
            result[fname] = spec.default
    return result
# ════════════════════════════════════════════
# 各文件 Schema 定义
# ════════════════════════════════════════════
# ── dong_status.json ──
_HORMONE_SCHEMA: Dict[str, FieldSpec] = {
    "dopamine":   FieldSpec(int, 60, range=(0, 100)),
    "adrenaline": FieldSpec(int, 30, range=(0, 100)),
    "cortisol":   FieldSpec(int, 20, range=(0, 100)),
    "oxytocin":   FieldSpec(int, 50, range=(0, 100)),
    "serotonin":  FieldSpec(int, 60, range=(0, 100)),
}
STATUS_SCHEMA: Dict[str, FieldSpec] = {
    "fatigue":    FieldSpec(int,   50,  range=(0, 100)),
    "mood":       FieldSpec(int,   60,  range=(0, 100)),
    "sleeping":   FieldSpec(bool,  False),
    "last_update": FieldSpec(str,  "",  nullable=True),
    "hormones":   FieldSpec(dict,  _build_default_dict(_HORMONE_SCHEMA),
                             children=_HORMONE_SCHEMA),
}
# ── dong_lastday.json ──
LASTDAY_SCHEMA: Dict[str, FieldSpec] = {
    "date":             FieldSpec(str,  "",  nullable=True),
    "mood_avg":         FieldSpec(int,  60,  range=(0, 100)),
    "fatigue_at_sleep": FieldSpec(int,  50,  range=(0, 100)),
    "sleep_time":       FieldSpec(str,  "",  nullable=True),
    "weekend":          FieldSpec(bool, False),
}
# ── dong_cycle.json ──
_CYCLE_SCHEMA: Dict[str, FieldSpec] = {
    "type":      FieldSpec(str, "normal",
                           choices=["high_energy", "low_energy", "sensitive",
                                    "independent", "normal"]),
    "days_left": FieldSpec(int, 3, range=(0, 30)),
    "started":   FieldSpec(str, "", nullable=True),
}
_PUSHPULL_SCHEMA: Dict[str, FieldSpec] = {
    "phase":     FieldSpec(str, "approach",
                           choices=["approach", "discomfort", "withdrawal", "regret"]),
    "intensity": FieldSpec(int, 0, range=(0, 100)),
    "started":   FieldSpec(str, "", nullable=True),
}
_OFFLINE_SCHEMA: Dict[str, FieldSpec] = {
    "last_check":     FieldSpec(str, "", nullable=True),
    "pending_events": FieldSpec(list, []),
}
CYCLE_SCHEMA: Dict[str, FieldSpec] = {
    "cycle":     FieldSpec(dict, _build_default_dict(_CYCLE_SCHEMA),
                           children=_CYCLE_SCHEMA),
    "pushpull":  FieldSpec(dict, _build_default_dict(_PUSHPULL_SCHEMA),
                           children=_PUSHPULL_SCHEMA),
    "offline":   FieldSpec(dict, _build_default_dict(_OFFLINE_SCHEMA),
                           children=_OFFLINE_SCHEMA),
    "last_interaction": FieldSpec(str, "", nullable=True),
}
# ════════════════════════════════════════════
# 便捷 heal 函数
# ════════════════════════════════════════════
def heal_status(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Repair]]:
    """heal dong_status.json 数据。"""
    return heal(data, STATUS_SCHEMA)
def heal_lastday(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Repair]]:
    """heal dong_lastday.json 数据。"""
    return heal(data, LASTDAY_SCHEMA)
def heal_cycle(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Repair]]:
    """heal dong_cycle.json 数据。"""
    return heal(data, CYCLE_SCHEMA)
def heal_any(data: Dict[str, Any],
             schema: Dict[str, FieldSpec]) -> Tuple[Dict[str, Any], List[Repair]]:
    """通用 heal 入口。"""
    return heal(data, schema)
# ════════════════════════════════════════════
# SelfHealingLoader — 封装 load → heal → 自动回写
# ════════════════════════════════════════════
class SelfHealingLoader:
    """
    带自愈的 JSON 文件加载器。
    用法：
        loader = SelfHealingLoader()
        data, repairs = loader.load("dong_status.json", STATUS_SCHEMA)
        # 如果有 repairs，已自动回写修复后的数据
    集成到现有 load_*() 中：
        def load_status(self):
            data, repairs = loader.load(STATUS_FILE, STATUS_SCHEMA)
            self._status = UserStatus.from_dict(data)
    """
    def __init__(self, auto_save: bool = True, backup_corrupted: bool = True):
        self.auto_save = auto_save            # 有修复时自动回写
        self.backup_corrupted = backup_corrupted  # JSON 解析失败时备份原文件
        self._history: List[Tuple[str, List[Repair]]] = []  # 修复历史
    def load(self, path: str,
             schema: Dict[str, FieldSpec]) -> Tuple[Dict[str, Any], List[Repair]]:
        """
        加载 JSON 文件 → heal → 如有修复则自动回写。
        文件不存在时返回全默认值（0 处修复）。
        JSON 解析失败时：尝试恢复 → 备份原文件 → 返回默认值。
        """
        # ── 文件不存在 → 返回默认 ──
        if not os.path.exists(path):
            defaults = _build_default_dict(schema)
            logger.info("数据文件不存在，使用默认值: %s", path)
            if self.auto_save:
                self._atomic_write(path, defaults)
            return defaults, []
        # ── 读取 + 解析 ──
        raw_data, parse_error = self._read_json(path)
        if parse_error or raw_data is None:
            # JSON 损坏 → 尝试从残留文本恢复
            raw_data = self._try_recover_from_file(path)
            if raw_data is None:
                raw_data = {}
            if self.backup_corrupted:
                self._backup_file(path)
            logger.warning("JSON 损坏已恢复/使用默认: %s", path)
        # ── heal ──
        if not isinstance(raw_data, dict):
            raw_data = {}
        data, repairs = heal(raw_data, schema)
        # ── 记录 & 回写 ──
        if repairs:
            self._history.append((path, repairs))
            for r in repairs:
                logger.info("  自愈 %s", r)
            if self.auto_save:
                self._atomic_write(path, data)
                logger.info("自愈回写: %s (%d处修复)", path, len(repairs))
        return data, repairs
    # ─── 读取 ───
    @staticmethod
    def _read_json(path: str) -> Tuple[Optional[Dict], bool]:
        """读取 JSON 文件，返回 (data, has_error)。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data, False
        except (json.JSONDecodeError, ValueError):
            return None, True
        except Exception:
            return None, True
    def _try_recover_from_file(self, path: str) -> Optional[Dict]:
        """从损坏文件中尝试提取最后一个完整 JSON 对象。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except Exception:
            return None
        last_brace = raw.rfind("}")
        if last_brace < 0:
            return None
        for cut in range(last_brace, max(last_brace - 5000, -1), -1):
            try:
                obj = json.loads(raw[:cut + 1])
                if isinstance(obj, dict):
                    logger.info("从损坏文件恢复: 截取前%d字符得到%d个键",
                                cut + 1, len(obj))
                    return obj
            except json.JSONDecodeError:
                continue
        return None
    # ─── 备份 ───
    @staticmethod
    def _backup_file(path: str) -> Optional[str]:
        """将损坏文件重命名为 .bak，返回备份路径。"""
        bak = path + ".bak"
        try:
            if os.path.exists(path):
                shutil.move(path, bak)
                logger.info("损坏文件已备份: %s → %s", path, bak)
                return bak
        except Exception as e:
            logger.warning("备份失败: %s", e)
        return None
    # ─── 原子写入 ───
    @staticmethod
    def _atomic_write(path: str, data: Dict[str, Any]):
        """原子写入 JSON 文件。"""
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            logger.exception("原子写入失败: %s", path)
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    # ─── 历史 ───
    @property
    def history(self) -> List[Tuple[str, List[Repair]]]:
        return list(self._history)
    def total_repairs(self) -> int:
        return sum(len(repairs) for _, repairs in self._history)
    def clear_history(self):
        self._history.clear()
# 全局单例
loader = SelfHealingLoader()