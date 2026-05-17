"""
冬 · 配置名字化 + 零风险热手
- ProviderConfig: 以名字为唯一标识的 Provider 配置
- ConfigRegistry: 按名字/任务注册、查询、降级链
- hot_swap: 校验 → 标记swapping → 替换 → 冒烟测试 → 提交/回滚
  其他线程在 swapping 期间不会选中该 Provider，保证零风险
"""
from __future__ import annotations
import logging
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
logger = logging.getLogger("dong.core.config_naming")
__all__ = [
    "ProviderConfig", "ValidateResult", "SwapResult",
    "ConfigRegistry", "registry",
]
# ════════════════════════════════════════════
# Provider 配置
# ════════════════════════════════════════════
@dataclass
class ProviderConfig:
    """单个 API Provider 的完整配置，name 为唯一主键。"""
    name: str                                          # 唯一名称
    model: str                                         # 模型标识
    api_key: str                                       # API 密钥
    api_base: str                                      # API 基础 URL
    tasks: Tuple[str, ...] = ("chat",)                 # 可承担的任务类型
    priority: int          = 0                         # 同任务内优先级 0=最高
    max_failures: int      = 3                         # 熔断阈值
    timeout: int           = 20                        # 默认超时(秒)
    enabled: bool          = True                      # 是否参与路由
    metadata: Dict[str, Any] = field(default_factory=dict)
    # ── 校验 ──
    def validate(self) -> List[str]:
        """基础校验，返回错误列表（空=通过）。"""
        errors: List[str] = []
        if not self.name or not self.name.strip():
            errors.append("name 不能为空")
        if not self.model or not self.model.strip():
            errors.append("model 不能为空")
        if not self.api_key or not self.api_key.strip():
            errors.append("api_key 不能为空")
        if not self.api_base or not self.api_base.strip():
            errors.append("api_base 不能为空")
        if not self.tasks:
            errors.append("tasks 不能为空")
        if self.max_failures < 1:
            errors.append("max_failures 必须 ≥ 1")
        if self.timeout < 1:
            errors.append("timeout 必须 ≥ 1")
        return errors
    # ── 序列化（密钥脱敏）──
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name":        self.name,
            "model":       self.model,
            "api_key":     "***" + self.api_key[-4:] if len(self.api_key) > 4 else "***",
            "api_base":    self.api_base,
            "tasks":       list(self.tasks),
            "priority":    self.priority,
            "max_failures":self.max_failures,
            "timeout":     self.timeout,
            "enabled":     self.enabled,
            "metadata":    self.metadata,
        }
# ════════════════════════════════════════════
# 操作结果
# ════════════════════════════════════════════
@dataclass
class ValidateResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
@dataclass
class SwapResult:
    ok: bool
    name: str
    old_config: Optional[ProviderConfig] = None
    new_config: Optional[ProviderConfig] = None
    smoke_passed: Optional[bool]         = None
    error: Optional[str]                 = None
# ════════════════════════════════════════════
# 配置注册表
# ════════════════════════════════════════════
class ConfigRegistry:
    """
    以名字为键的 Provider 配置注册表。
    核心能力：
      register       — 注册，按 task+priority 组织
      get / get_primary / get_chain — 查询
      hot_swap       — 零风险热切换
      enable/disable — 运行时开关
    """
    def __init__(self):
        self._configs:  Dict[str, ProviderConfig] = {}
        self._swapping: Set[str]  = set()          # 正在热切换中的名字
        self._lock = threading.RLock()              # 可重入，允许嵌套
    # ════════════ 注册 ════════════
    def register(self, config: ProviderConfig, *,
                 overwrite: bool = False) -> ValidateResult:
        """注册一个 Provider 配置；同名已存在时需 overwrite=True。"""
        errors = config.validate()
        if errors:
            return ValidateResult(ok=False, errors=errors)
        with self._lock:
            if config.name in self._configs and not overwrite:
                return ValidateResult(
                    ok=False,
                    errors=[f"名字 '{config.name}' 已存在，设 overwrite=True 覆盖"],
                )
            self._configs[config.name] = config
            logger.info("配置注册: %s (tasks=%s, priority=%d)",
                        config.name, config.tasks, config.priority)
        return ValidateResult(ok=True)
    def register_many(self, configs: List[ProviderConfig], *,
                      overwrite: bool = False) -> List[ValidateResult]:
        return [self.register(c, overwrite=overwrite) for c in configs]
    # ════════════ 查询 ════════════
    def get(self, name: str) -> Optional[ProviderConfig]:
        """按名字获取配置副本。"""
        with self._lock:
            cfg = self._configs.get(name)
            return deepcopy(cfg) if cfg else None
    def get_primary(self, task: str) -> Optional[ProviderConfig]:
        """获取指定任务的当前主力配置（enabled + 非swapping + 最低priority）。"""
        with self._lock:
            candidates = [
                c for c in self._configs.values()
                if c.enabled and task in c.tasks and c.name not in self._swapping
            ]
        if not candidates:
            return None
        candidates.sort(key=lambda c: c.priority)
        return deepcopy(candidates[0])
    def get_chain(self, task: str) -> List[ProviderConfig]:
        """获取指定任务的完整降级链（排除 disabled / swapping）。"""
        with self._lock:
            candidates = [
                deepcopy(c) for c in self._configs.values()
                if c.enabled and task in c.tasks and c.name not in self._swapping
            ]
        candidates.sort(key=lambda c: c.priority)
        return candidates
    def list_names(self) -> List[str]:
        with self._lock:
            return list(self._configs.keys())
    # ════════════ 开关 ════════════
    def enable(self, name: str) -> bool:
        with self._lock:
            cfg = self._configs.get(name)
            if cfg:
                cfg.enabled = True
                return True
        return False
    def disable(self, name: str) -> bool:
        """禁用一个 Provider（不删除，只是退出路由）。"""
        with self._lock:
            cfg = self._configs.get(name)
            if cfg:
                cfg.enabled = False
                return True
        return False
    # ════════════ 零风险热切换 ════════════
    def hot_swap(self, name: str, new_config: ProviderConfig,
                 smoke_test_fn: Optional[Callable[[ProviderConfig], bool]] = None,
                 skip_smoke: bool = False) -> SwapResult:
        """
        零风险热切换流程：
        1. 校验 new_config
        2. 标记 name 为 swapping（路由跳过该 Provider）
        3. 原子替换配置
        4. 执行冒烟测试（默认：发一条 max_tokens=5 的请求）
        5. 通过 → 提交；失败 → 回滚到旧配置
        6. 清除 swapping 标记
        其他线程在 swapping 期间不会选中该 Provider，
        因此绝不会出现"用了半截新配置"的情况。
        """
        result = SwapResult(ok=False, name=name)
        # 1. 校验
        errors = new_config.validate()
        if errors:
            result.error = "校验失败: " + "; ".join(errors)
            return result
        if new_config.name != name:
            result.error = f"配置名 '{new_config.name}' 与目标名 '{name}' 不一致"
            return result
        # 2. 加锁 → 取旧配置快照 → 标记 swapping
        with self._lock:
            old_config = self._configs.get(name)
            if old_config is None:
                result.error = f"名字 '{name}' 未注册，请先 register"
                return result
            old_snapshot = deepcopy(old_config)
            self._swapping.add(name)
        try:
            # 3. 原子替换
            with self._lock:
                self._configs[name] = deepcopy(new_config)
            # 4. 冒烟测试
            if skip_smoke:
                result.smoke_passed = None
                result.ok = True
            else:
                test_fn = smoke_test_fn or self._default_smoke_test
                try:
                    passed = test_fn(new_config)
                except Exception as e:
                    logger.warning("冒烟测试异常: %s", e)
                    passed = False
                result.smoke_passed = passed
                if passed:
                    result.ok = True
                    logger.info("热切换成功: %s (model: %s → %s)",
                                name, old_snapshot.model, new_config.model)
                else:
                    # 6. 回滚
                    with self._lock:
                        self._configs[name] = old_snapshot
                    result.error = "冒烟测试失败，已回滚"
                    logger.warning("热切换回滚: %s", name)
            result.old_config = old_snapshot
            result.new_config = deepcopy(new_config) if result.ok else None
            return result
        finally:
            with self._lock:
                self._swapping.discard(name)
    # ── 默认冒烟测试 ──
    @staticmethod
    def _default_smoke_test(config: ProviderConfig) -> bool:
        """
        向 Provider 发一条极短请求验证连通性。
        max_tokens=5, temperature=0.1，成本极低。
        429（限流）视为连通正常——说明端点可达。
        """
        import requests as _req
        try:
            r = _req.post(
                f"{config.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": config.model,
                    "temperature": 0.1,
                    "max_tokens": 5,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                return bool(
                    "choices" in data
                    and data["choices"]
                    and data["choices"][0].get("message", {}).get("content")
                )
            if r.status_code == 429:
                logger.info("冒烟测试收到 429（限流），视为连通正常")
                return True
            return False
        except Exception as e:
            logger.debug("冒烟测试异常: %s", e)
            return False
    # ════════════ 状态导出 ════════════
    def snapshot(self) -> Dict[str, Any]:
        """导出注册表快照（密钥脱敏）。"""
        with self._lock:
            return {
                "providers": {
                    n: c.to_dict() for n, c in self._configs.items()
                },
                "swapping": list(self._swapping),
                "total":    len(self._configs),
                "enabled":  sum(1 for c in self._configs.values() if c.enabled),
            }
    # ════════════ 从旧索引列表迁移 ════════════
    def migrate_from_indexed(self, indexed_list: list) -> List[str]:
        """
        从旧的 API_CONFIGS 索引列表迁移。
        按列表顺序分配 priority (0, 1, 2, ...)，同 task 内自动排序。
        返回注册成功的名字列表。
        """
        registered: List[str] = []
        task_counters: Dict[str, int] = {}       # task → 下一个 priority
        for idx, cfg in enumerate(indexed_list):
            task = getattr(cfg, "task", "chat")
            pri  = task_counters.get(task, 0)
            task_counters[task] = pri + 1
            pc = ProviderConfig(
                name        = getattr(cfg, "name", f"provider_{idx}"),
                model       = getattr(cfg, "model", ""),
                api_key     = getattr(cfg, "api_key", ""),
                api_base    = getattr(cfg, "api_base", ""),
                tasks       = (task,),
                priority    = pri,
                max_failures= getattr(cfg, "max_failures", 3),
            )
            vr = self.register(pc, overwrite=True)
            if vr.ok:
                registered.append(pc.name)
            else:
                logger.warning("迁移失败: %s — %s", pc.name, vr.errors)
        return registered
# 全局单例
registry = ConfigRegistry()