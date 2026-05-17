"""
冬 · API 统一网关
- 所有 LLM API 调用统一经过此网关
- 按任务类型路由到对应 Provider 降级链
- 熔断器 Circuit Breaker per Provider
- 限速器 Rate Limiter per Provider
- 指数退避重试
- 输出过滤（视角安全 + 动作描写剥离）
- 全局冷却（所有 Provider 熔断时）
- 请求缓存（可关闭）
- 统计信息导出
替代原有 api.py chat() + status.py _call_ai_simple() 的全部路由/熔断/退避逻辑
"""
from __future__ import annotations
import hashlib
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
logger = logging.getLogger("dong.core.api_gateway")
__all__ = [
    "CircuitState", "GatewayResult", "ApiGateway", "gateway",
    "sanitize_response_content", "apply_output_filter",
]
# ════════════════════════════════════════════
# 熔断器状态
# ════════════════════════════════════════════
class CircuitState(Enum):
    CLOSED    = auto()   # 正常通行
    OPEN      = auto()   # 熔断，不允许请求
    HALF_OPEN = auto()   # 半开，允许一个试探请求
# ════════════════════════════════════════════
# 网关返回
# ════════════════════════════════════════════
@dataclass
class GatewayResult:
    ok: bool
    content: Optional[str]       = None
    provider: str                = ""
    task: str                    = ""
    attempts: int                = 0
    fell_back: bool              = False
    latency_ms: float            = 0.0
    from_cache: bool             = False
    filtered: bool               = False
    filtered_chars: int          = 0
    error: Optional[str]         = None
# ════════════════════════════════════════════
# 输出过滤（从 api.py 迁移，作为网关内建能力）
# ════════════════════════════════════════════
_SAFE_PARENS = {
    "不是", "bushi", "没有", "真的", "别", "来", "没",
    "嗯", "啊", "哦", "哈", "好", "行", "对", "错",
    "啥", "咋", "诶", "哼", "呀",
}
_ACTION_VERBS = [
    "夹着", "顿了一下", "挑眉", "思考", "手指", "从兜", "深吸", "吐出",
    "站起身", "转过头", "看了一眼", "皱眉", "嘴角", "抬起", "放下",
    "摸出", "拿起", "指了指", "叹了口气", "捋了", "撩了", "踢了",
    "踩了", "推了", "拉了", "拍了拍", "敲了", "点开", "滑动", "打字",
    "耸了耸肩", "点了根", "叼着", "呼出一口", "掐灭", "弹了弹",
    "推门", "躺在", "翻了个身", "打了个", "揉了揉", "瞥了一眼",
]
_ACTION_SINGLE = {
    "笑", "哭", "叹", "抖", "愣", "呆", "看", "盯", "瞥",
    "站", "坐", "躺", "走", "跑", "跳", "爬", "喊", "叫",
    "咳", "喘", "哼", "啧", "嘶", "嘘", "吹", "点",
}
# API Key 模式 — 响应内容中出现这些直接替换
_API_KEY_PATTERNS = [
    (r'(sk-[A-Za-z0-9]{20,})', '[REDACTED]'),          # OpenAI key
    (r'(Bearer\s+[A-Za-z0-9\-_\.]{20,})', '[REDACTED]'),  # Bearer token
    (r'(api_key[=:]\s*["\']?[A-Za-z0-9\-_]{16,})', 'api_key=***'),  # api_key=xxx
    (r'(DONG_AGENT_API_KEY\s*=\s*["\']?[^"\'\s]{8,})', 'DONG_AGENT_API_KEY=***'),
]

def sanitize_response_content(content: str) -> str:
    """视角安全锁 + API Key过滤：若返回内容混入多轮标记或密钥，清理。"""
    if not content or len(content) < 10:
        return content
    # API Key 模式过滤
    for pattern, replacement in _API_KEY_PATTERNS:
        content = re.sub(pattern, replacement, content)
    markers = ["User:", "Assistant:", "用户：", "冬：", "user:", "assistant:"]
    found = [m for m in markers if m in content]
    if len(found) < 2:
        return content
    parts = re.split(r'(?:User:|Assistant:|用户：|冬：|user:|assistant:)\s*', content)
    for p in reversed(parts):
        p = p.strip()
        if p and len(p) >= 2:
            logger.debug("视角安全锁: %d→%d字", len(content), len(p))
            return p
    return content
def apply_output_filter(text: str) -> Tuple[str, int]:
    """
    过滤动作描写括号，保留真实口癖。
    返回 (过滤后文本, 删除字数)。
    """
    if not text:
        return text, 0
    def _is_speech(content: str) -> bool:
        content = content.strip()
        if content in _SAFE_PARENS:
            return True
        if len(content) <= 2:
            return content not in _ACTION_SINGLE
        return not any(v in content for v in _ACTION_VERBS)
    def _filter(m: re.Match) -> str:
        return m.group(0) if _is_speech(m.group(1)) else ""
    filtered = text
    filtered = re.sub(r'（([^）]*)）', _filter, filtered)
    filtered = re.sub(r'\(([^)]*)\)', _filter, filtered)
    filtered = re.sub(r' +', ' ', filtered).strip()
    filtered = re.sub(r'\n +', '\n', filtered)
    removed = len(text.replace(" ", "")) - len(filtered.replace(" ", ""))
    return filtered, removed
def sanitize_for_tts(text: str) -> str:
    """TTS 前剥离非台词内容。"""
    text = re.sub(r'【[^】]*内心[^】]*】[^。！？\n]*[。！？]?', '', text)
    text = re.sub(r'【[^】]*独白[^】]*】[^。！？\n]*[。！？]?', '', text)
    text = re.sub(r'【[^】]*心声[^】]*】[^。！？\n]*[。！？]?', '', text)
    text = re.sub(r'（[^）]*(?:' + '|'.join(_ACTION_VERBS) + r')[^）]*）', '', text)
    text = text.replace('（撤回）', '').replace('(撤回)', '')
    text = re.sub(r' +', ' ', text).strip()
    return text
# ════════════════════════════════════════════
# 熔断器
# ════════════════════════════════════════════
class CircuitBreaker:
    """
    单个 Provider 的熔断器。
    CLOSED  → 连续失败 ≥ threshold → OPEN
    OPEN    → 冷却期过后 → HALF_OPEN
    HALF_OPEN → 试探成功 → CLOSED；试探失败 → OPEN
    """
    def __init__(self, provider: str, threshold: int = 3,
                 cooldown: float = 60.0):
        self.provider  = provider
        self.threshold = threshold
        self.cooldown  = cooldown
        self.state: CircuitState   = CircuitState.CLOSED
        self.failures: int         = 0
        self.last_failure: float   = 0.0
        self.last_success: float   = 0.0
        self._lock = threading.Lock()
    def allow(self) -> bool:
        """当前是否允许请求通过。"""
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                if time.monotonic() - self.last_failure >= self.cooldown:
                    self.state = CircuitState.HALF_OPEN
                    logger.info("熔断器半开: %s", self.provider)
                    return True
                return False
            # HALF_OPEN: 允许一个试探，立即转为OPEN防止并发请求雪崩
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                return True
    def record_success(self):
        with self._lock:
            self.failures = 0
            self.state = CircuitState.CLOSED
            self.last_success = time.monotonic()
    def record_failure(self):
        with self._lock:
            self.failures += 1
            self.last_failure = time.monotonic()
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                logger.warning("熔断器半开→再开: %s", self.provider)
            elif self.failures >= self.threshold:
                self.state = CircuitState.OPEN
                logger.warning("熔断器打开: %s (连续%d次失败)",
                               self.provider, self.failures)
    def record_429(self):
        """429 限流：直接熔断。"""
        with self._lock:
            self.failures = self.threshold
            self.last_failure = time.monotonic()
            self.state = CircuitState.OPEN
            logger.warning("熔断器打开(429): %s", self.provider)
    def reset(self):
        with self._lock:
            self.failures = 0
            self.state = CircuitState.CLOSED
    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "provider":  self.provider,
                "state":     self.state.name,
                "failures":  self.failures,
                "threshold": self.threshold,
                "cooldown":  self.cooldown,
                "last_failure_ago": round(time.monotonic() - self.last_failure, 1)
                                    if self.last_failure > 0 else None,
            }
# ════════════════════════════════════════════
# 限速器
# ════════════════════════════════════════════
class RateLimiter:
    """滑动窗口限速器。"""
    def __init__(self, max_per_minute: int = 20):
        self.max_per_minute = max_per_minute
        self._timestamps: Deque[float] = deque()
        self._lock = threading.Lock()
    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            # 清除过期
            while self._timestamps and now - self._timestamps[0] > 60.0:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.max_per_minute:
                return False
            self._timestamps.append(now)
            return True
    def wait_if_needed(self) -> float:
        """若限速返回需等待秒数，0=放行并记录时间戳"""
        now = time.monotonic()
        with self._lock:
            while self._timestamps and now - self._timestamps[0] > 60.0:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.max_per_minute:
                return 60.0 - (now - self._timestamps[0])
            self._timestamps.append(now)
            return 0.0
# ════════════════════════════════════════════
# 请求缓存
# ════════════════════════════════════════════
class RequestCache:
    """LRU 请求缓存，避免短时间重复请求。"""
    def __init__(self, ttl: float = 60.0, max_size: int = 100):
        self.ttl      = ttl
        self.max_size = max_size
        from collections import OrderedDict
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
    def _key(self, messages: List[Dict], task: str) -> str:
        raw = f"{task}:" + "|".join(m.get("content", "") for m in messages)
        return hashlib.sha256(raw.encode()).hexdigest()
    def get(self, messages: List[Dict], task: str) -> Optional[str]:
        k = self._key(messages, task)
        with self._lock:
            if k in self._cache:
                content, ts = self._cache[k]
                if time.monotonic() - ts < self.ttl:
                    self._cache.move_to_end(k)
                    return content
                del self._cache[k]
        return None
    def set(self, messages: List[Dict], task: str, content: str):
        k = self._key(messages, task)
        with self._lock:
            self._cache[k] = (content, time.monotonic())
            if len(self._cache) > self.max_size:
                # LRU淘汰：删除最旧的10%（OrderedDict按插入顺序，最早的在前面）
                remove_n = len(self._cache) // 10 + 1
                for _ in range(remove_n):
                    self._cache.popitem(last=False)
    def clear(self):
        with self._lock:
            self._cache.clear()
# ════════════════════════════════════════════
# API 统一网关
# ════════════════════════════════════════════
class ApiGateway:
    """
    统一 API 网关 —— 所有 LLM 调用的唯一入口。
    职责：
      1. 路由：按 task 查询 Provider 降级链
      2. 熔断：per Provider，连续失败/429 自动熔断
      3. 限速：per Provider，滑动窗口
      4. 重试：同 Provider 指数退避 → 切换下一 Provider
      5. 过滤：视角安全锁 + 动作描写剥离
      6. 缓存：可选，短时间重复请求命中缓存
      7. 全局冷却：所有 Provider 熔断时，30 分钟内直接返回 fallback
    """
    # 兜底回复
    _FALLBACKS_SHORT = ["哼", "才没有呢", "谁理你啊", "烦死了啦", "不要！"]
    _FALLBACKS_LONG  = ["才不要告诉你", "自己猜啦", "忙着呢", "才不困", "别管我"]
    # 全局冷却时长（秒）
    GLOBAL_COOLDOWN = 1800  # 30 分钟
    def __init__(self, config_registry=None):
        """
        Args:
            config_registry: ConfigRegistry 实例。
                若为 None，则 call() 会因找不到 Provider 而返回失败。
        """
        self._registry = config_registry
        self._circuits: Dict[str, CircuitBreaker] = {}
        self._limiters: Dict[str, RateLimiter]    = {}
        self._cache = RequestCache(ttl=60, max_size=100)
        self._global_cooldown_until: float = 0.0     # 全局冷却截止时间戳
        self._lock = threading.Lock()
        self._session = None  # 延迟创建Session以复用连接池
        # 统计
        self._stats_calls: Dict[str, int]       = {}   # provider → 调用次数
        self._stats_failures: Dict[str, int]    = {}   # provider → 失败次数
        self._stats_fallbacks: int              = 0
        self._stats_cache_hits: int             = 0
        self._stats_total: int                  = 0
    # ════════════ 核心: call ════════════
    def call(self, messages: List[Dict[str, str]], *,
             task: str              = "chat",
             temperature: float     = 1.0,
             max_tokens: int        = 1500,
             timeout: Optional[int] = None,
             use_cache: bool        = True,
             filter_output: bool    = True,
             max_retries: int       = 3,
             on_fallback: Optional[Callable[[str, str], None]] = None,
             ) -> GatewayResult:
        """
        统一调用入口。
        流程：
          全局冷却? → 返回 fallback
          缓存命中? → 返回缓存
          遍历降级链:
            熔断? → 跳过
            限速? → 等待或跳过
            调用 → 成功? 过滤 → 缓存 → 返回
                 → 429? 熔断 → 下一个
                 → 失败? 记录 → 指数退避 → 重试/下一个
          全部失败 → 全局冷却 → 返回 fallback
        """
        with self._lock:
            self._stats_total += 1
        start = time.monotonic()
        result = GatewayResult(ok=False, task=task)
        # ── 1. 全局冷却检查 ──
        with self._lock:
            cooldown_until = self._global_cooldown_until
        if time.monotonic() < cooldown_until:
            remaining = cooldown_until - time.monotonic()
            result.error = f"全局冷却中 ({remaining:.0f}s)"
            result.content = self._pick_fallback()
            logger.debug("全局冷却: %.0fs 剩余", remaining)
            return result
        # ── 2. 缓存 ──
        if use_cache:
            cached = self._cache.get(messages, task)
            if cached is not None:
                with self._lock:
                    self._stats_cache_hits += 1
                result.ok = True
                result.content = cached
                result.from_cache = True
                result.latency_ms = (time.monotonic() - start) * 1000
                return result
        # ── 3. 获取降级链 ──
        if self._registry is None:
            result.error = "无配置注册表"
            return result
        chain = self._registry.get_chain(task)
        if not chain:
            result.error = f"任务 '{task}' 无可用 Provider"
            result.content = self._pick_fallback()
            return result
        # ── 4. 遍历 Provider ──
        for cfg in chain:
            circuit  = self._get_circuit(cfg.name, cfg.max_failures)
            limiter  = self._get_limiter(cfg.name)
            # 熔断检查
            if not circuit.allow():
                logger.debug("跳过熔断 Provider: %s", cfg.name)
                continue
            # 限速检查
            wait_s = limiter.wait_if_needed()
            if wait_s > 0:
                if wait_s <= 2.0:
                    time.sleep(wait_s)
                else:
                    logger.debug("限速跳过 Provider: %s (需等%.1fs)", cfg.name, wait_s)
                    continue
            # 同一 Provider 内重试
            for attempt in range(max_retries):
                result.attempts += 1
                result.provider = cfg.name
                resp = None
                try:
                    # 延迟创建Session以复用连接池
                    if self._session is None:
                        import requests as _req
                        self._session = _req.Session()
                    resp = self._http_call(cfg, messages,
                                           temperature=temperature,
                                           max_tokens=max_tokens,
                                           timeout=timeout or cfg.timeout,
                                           session=self._session)
                except Exception as e:
                    logger.warning("API异常 [%s] attempt=%d: %s",
                                   cfg.name, attempt + 1, e)
                    circuit.record_failure()
                    self._inc_stat(self._stats_failures, cfg.name)
                    delay = min(2 ** attempt, 8)
                    if delay <= 8 and attempt < max_retries - 1:
                        time.sleep(delay)
                    continue
                try:
                    # ── HTTP 响应处理 ──
                    if resp.status_code == 200:
                        content = self._extract_content(resp)
                        if content is None:
                            circuit.record_failure()
                            self._inc_stat(self._stats_failures, cfg.name)
                            continue
                        # 成功
                        circuit.record_success()
                        self._inc_stat(self._stats_calls, cfg.name)
                        # 过滤
                        if filter_output:
                            content = sanitize_response_content(content)
                            content, removed = apply_output_filter(content)
                            result.filtered = removed > 0
                            result.filtered_chars = removed
                            if len(content) <= 2:
                                logger.debug("过滤后过短(%d字)，返回兜底", len(content))
                                result.ok = True
                                result.content = self._pick_fallback(short=True)
                                result.latency_ms = (time.monotonic() - start) * 1000
                                return result
                        if use_cache:
                            self._cache.set(messages, task, content)
                        result.ok = True
                        result.content = content
                        result.latency_ms = (time.monotonic() - start) * 1000
                        if cfg != chain[0]:
                            result.fell_back = True
                        return result
                    elif resp.status_code == 429:
                        logger.warning("429限流: %s", cfg.name)
                        circuit.record_429()
                        self._inc_stat(self._stats_failures, cfg.name)
                        if on_fallback:
                            on_fallback(cfg.name, "429")
                        break  # 跳到下一个 Provider
                    else:
                        logger.warning("HTTP %d: %s", resp.status_code, cfg.name)
                        circuit.record_failure()
                        self._inc_stat(self._stats_failures, cfg.name)
                        if attempt < max_retries - 1:
                            import time as _t
                            _t.sleep(min(2 ** attempt, 8))
                        continue
                finally:
                    if resp is not None:
                        resp.close()
            # 同 Provider 重试耗尽 → 尝试下一个
            if on_fallback:
                on_fallback(cfg.name, "retry_exhausted")
            with self._lock:
                self._stats_fallbacks += 1
        # ── 5. 全部失败 → 全局冷却 + fallback ──
        with self._lock:
            self._global_cooldown_until = time.monotonic() + self.GLOBAL_COOLDOWN
        logger.warning("所有 Provider 失败，进入 %.0fmin 全局冷却",
                        self.GLOBAL_COOLDOWN / 60)
        result.content = self._pick_fallback()
        result.error = "全部 Provider 失败"
        result.latency_ms = (time.monotonic() - start) * 1000
        return result
    # ════════════ 便捷方法 ════════════
    def call_simple(self, system: str, user: str, *,
                    task: str          = "analysis",
                    temperature: float = 0.5,
                    max_tokens: int    = 150,
                    timeout: int       = 10,
                    ) -> Optional[str]:
        """
        简化调用：替代 _call_ai_simple()。
        成功返回文本，失败返回 None。
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]
        result = self.call(
            messages, task=task,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            use_cache=False, filter_output=False, max_retries=1,
        )
        return result.content if result.ok else None
    def call_chat(self, messages: List[Dict[str, str]], *,
                  task: str = "chat", **kwargs) -> Optional[str]:
        """
        聊天调用：替代原 chat()。
        成功返回文本，失败返回 None。
        """
        result = self.call(messages, task=task, **kwargs)
        return result.content if result.ok else None
    # ════════════ 管理 ════════════
    def reset_circuit(self, provider: str):
        """手动重置某个 Provider 的熔断器。"""
        with self._lock:
            if provider in self._circuits:
                self._circuits[provider].reset()
    def reset_all_circuits(self):
        """重置所有熔断器。"""
        with self._lock:
            for cb in self._circuits.values():
                cb.reset()
            self._global_cooldown_until = 0.0
    def clear_cache(self):
        self._cache.clear()
    # ════════════ 统计 ════════════
    def stats(self) -> Dict[str, Any]:
        """导出网关统计信息。"""
        with self._lock:
            circuits = {n: c.to_dict() for n, c in self._circuits.items()}
        in_cooldown = time.monotonic() < self._global_cooldown_until
        cooldown_remaining = max(0, self._global_cooldown_until - time.monotonic())
        return {
            "total_calls":    self._stats_total,
            "cache_hits":     self._stats_cache_hits,
            "fallbacks":      self._stats_fallbacks,
            "per_provider":   dict(self._stats_calls),
            "per_provider_failures": dict(self._stats_failures),
            "circuits":       circuits,
            "global_cooldown": in_cooldown,
            "cooldown_remaining": round(cooldown_remaining, 1),
        }
    # ════════════ 内部 ════════════
    def _get_circuit(self, provider: str, threshold: int) -> CircuitBreaker:
        with self._lock:
            if provider not in self._circuits:
                self._circuits[provider] = CircuitBreaker(
                    provider, threshold=threshold
                )
            return self._circuits[provider]
    def _get_limiter(self, provider: str) -> RateLimiter:
        with self._lock:
            if provider not in self._limiters:
                self._limiters[provider] = RateLimiter(max_per_minute=20)
            return self._limiters[provider]
    @staticmethod
    def _http_call(cfg, messages: List[Dict], *,
                   temperature: float, max_tokens: int,
                   timeout: int, session=None):
        """底层 HTTP 调用（不含任何路由/重试逻辑）。"""
        import requests as _req
        s = session or _req
        # 统一超时30秒，除非传入更短timeout
        effective_timeout = max(timeout, 30) if timeout else 30
        return s.post(
            f"{cfg.api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model":       cfg.model,
                "temperature": temperature,
                "max_tokens":  max_tokens,
                "messages":    messages,
            },
            timeout=effective_timeout,
        )
    @staticmethod
    def _extract_content(resp) -> Optional[str]:
        """从 HTTP 响应中提取文本内容。"""
        try:
            data = resp.json()
            if "choices" in data and data["choices"]:
                content = data["choices"][0].get("message", {}).get("content")
                if content and content.strip():
                    lines = [l.strip() for l in content.strip().split('\n') if l.strip()]
                    return '\n'.join(lines)
        except Exception as e:
            logger.debug("解析响应异常: %s", e)
        return None
    @staticmethod
    def _pick_fallback(short: bool = False) -> str:
        import random
        pool = ApiGateway._FALLBACKS_SHORT if short else (
            ApiGateway._FALLBACKS_SHORT + ApiGateway._FALLBACKS_LONG
        )
        return random.choice(pool)
    def _inc_stat(self, counter: Dict[str, int], key: str):
        with self._lock:
            counter[key] = counter.get(key, 0) + 1
# ════════════════════════════════════════════
# 全局单例（延迟绑定 config_registry）
# ════════════════════════════════════════════
gateway = ApiGateway(config_registry=None)
def bind_registry(registry) -> None:
    """启动时调用一次，将配置注册表绑定到网关。"""
    gateway._registry = registry