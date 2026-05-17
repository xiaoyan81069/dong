"""
冬 · 事件总线
- 生命周期阶段 Phase 枚举
- @bus.on / @bus.on_phase 订阅装饰器
- emit / emit_async 触发
- 深度 / 频率 / 循环 三重保护
"""
from __future__ import annotations
import asyncio
import logging
import threading
import time
from collections import defaultdict, deque
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
logger = logging.getLogger("dong.core.event_bus")
__all__ = ["Phase", "EventBus", "bus"]
# ────────────────────────────────────────────
# 生命周期阶段
# ────────────────────────────────────────────
class Phase(Enum):
    """机器人生命周期阶段"""
    INIT      = auto()   # 模块加载 / 配置读取
    STARTUP   = auto()   # main() 执行中，连接建立前
    CONNECTED = auto()   # WS 连接就绪
    RUNNING   = auto()   # robot_loop 正常运转
    SLEEP     = auto()   # 进入休眠
    WAKE      = auto()   # 从休眠醒来
    SHUTDOWN  = auto()   # 关闭清理
EventKey = Union[Phase, str]
# ────────────────────────────────────────────
# 处理器描述
# ────────────────────────────────────────────
class _Handler:
    __slots__ = ("fn", "event", "priority", "is_async", "once")
    def __init__(self, fn: Callable, event: EventKey,
                 priority: int, is_async: bool, once: bool):
        self.fn        = fn
        self.event     = event
        self.priority  = priority      # 数值越高越先执行
        self.is_async  = is_async
        self.once      = once
# ────────────────────────────────────────────
# 保护配置
# ────────────────────────────────────────────
class _GuardConfig:
    MAX_DEPTH        = 8       # 嵌套 emit 最大深度
    FREQ_WINDOW_S    = 1.0     # 频率窗口（秒）
    FREQ_MAX_PER_WIN = 10      # 窗口内同一事件最大 emit 次数
    LOOP_MAX_EDGE    = 2       # 同一条边在链中出现此次数即判循环
# ────────────────────────────────────────────
# 事件总线
# ────────────────────────────────────────────
class EventBus:
    """
    线程安全的事件总线，带三重保护：
      1. 深度保护 — 嵌套 emit 超 MAX_DEPTH 层时静默丢弃
      2. 频率保护 — 同一事件在滑动窗口内超过阈值时静默丢弃
      3. 循环保护 — emit 链中同一条有向边出现 ≥ LOOP_MAX_EDGE 次时静默丢弃
    """
    def __init__(self, guard: Optional[_GuardConfig] = None):
        self._g = guard or _GuardConfig()
        # event → sorted list[_Handler]（priority 降序）
        self._handlers: Dict[EventKey, List[_Handler]] = defaultdict(list)
        self._lock = threading.Lock()
        # ── 保护状态 ──
        self._depth_counter = 0                           # 当前嵌套深度
        self._freq_tracker: Dict[EventKey, deque] = defaultdict(deque)   # event → 时间戳队列
        self._chain: List[EventKey] = []                  # 当前 emit 链
        self._chain_lock = threading.Lock()
    # ════════════════ 订阅 ════════════════
    def on(self, event: EventKey, *, priority: int = 0, once: bool = False):
        """通用订阅装饰器，支持 Phase 或 str 事件名。"""
        def decorator(fn: Callable) -> Callable:
            h = _Handler(
                fn=fn,
                event=event,
                priority=priority,
                is_async=asyncio.iscoroutinefunction(fn),
                once=once,
            )
            with self._lock:
                self._handlers[event].append(h)
                self._handlers[event].sort(key=lambda x: -x.priority)
            return fn
        return decorator
    def on_phase(self, phase: Phase, *, priority: int = 0, once: bool = False):
        """生命周期阶段订阅装饰器（on 的语义快捷方式）。"""
        return self.on(phase, priority=priority, once=once)
    def remove(self, fn: Callable):
        """按函数引用移除处理器。"""
        with self._lock:
            for key in list(self._handlers):
                self._handlers[key] = [
                    h for h in self._handlers[key] if h.fn is not fn
                ]
    # ════════════════ 保护检查 ════════════════
    def _check_depth(self) -> bool:
        """深度保护：嵌套层数 ≤ MAX_DEPTH"""
        if self._depth_counter >= self._g.MAX_DEPTH:
            logger.warning("emit 深度保护触发 (depth=%d)", self._depth_counter)
            return False
        return True
    def _check_freq(self, event: EventKey) -> bool:
        """频率保护：滑动窗口内同一事件 ≤ FREQ_MAX_PER_WIN"""
        now = time.monotonic()
        q = self._freq_tracker[event]
        # 清除过期时间戳
        while q and now - q[0] > self._g.FREQ_WINDOW_S:
            q.popleft()
        if len(q) >= self._g.FREQ_MAX_PER_WIN:
            logger.warning("emit 频率保护触发 (event=%s, window_count=%d)",
                           event, len(q))
            return False
        q.append(now)
        return True
    def _check_loop(self, event: EventKey) -> bool:
        """循环保护：当前链中同一条有向边出现 < LOOP_MAX_EDGE 次"""
        if not self._chain:
            return True
        new_edge = (self._chain[-1], event)
        count = 0
        for i in range(len(self._chain) - 1):
            if (self._chain[i], self._chain[i + 1]) == new_edge:
                count += 1
                if count >= self._g.LOOP_MAX_EDGE:
                    logger.warning("emit 循环保护触发 (edge=%s, count=%d)",
                                   new_edge, count)
                    return False
        return True
    def _enter_emit(self, event: EventKey) -> bool:
        """进入 emit 前的三重保护检查，通过返回 True。"""
        with self._chain_lock:
            if not self._check_depth():
                return False
            if not self._check_freq(event):
                return False
            if not self._check_loop(event):
                return False
            self._depth_counter += 1
            self._chain.append(event)
            return True
    def _leave_emit(self, event: EventKey):
        """离开 emit，恢复保护状态。"""
        with self._chain_lock:
            self._depth_counter = max(0, self._depth_counter - 1)
            if self._chain and self._chain[-1] is event:
                self._chain.pop()
            # 顶层 emit 结束后清空频率追踪（释放内存）
            if self._depth_counter == 0:
                self._freq_tracker.clear()
    # ════════════════ 同步触发 ════════════════
    def emit(self, event: EventKey, data: Any = None) -> bool:
        """
        同步触发事件。
        同步处理器立即执行；异步处理器尝试调度到运行中的事件循环。
        返回 True 表示至少有一个处理器被调度/执行。
        """
        if not self._enter_emit(event):
            return False
        try:
            with self._lock:
                handlers = list(self._handlers[event])
                # 移除 once 标记的处理器
                self._handlers[event] = [
                    h for h in self._handlers[event] if not h.once
                ]
            if not handlers:
                return False
            executed = False
            for h in handlers:
                try:
                    if h.is_async:
                        self._schedule_async(h, event, data)
                    else:
                        h.fn(event, data)
                    executed = True
                except Exception:
                    logger.exception("处理器异常 (event=%s, fn=%s)",
                                     event, getattr(h.fn, "__name__", "?"))
            return executed
        finally:
            self._leave_emit(event)
    def _schedule_async(self, h: _Handler, event: EventKey, data: Any):
        """尝试将异步处理器调度到运行中的事件循环。"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(h.fn(event, data))
        except RuntimeError:
            logger.debug("无运行中事件循环，跳过异步处理器 fn=%s",
                         getattr(h.fn, "__name__", "?"))
    # ════════════════ 异步触发 ════════════════
    async def emit_async(self, event: EventKey, data: Any = None) -> bool:
        """
        异步触发事件。
        异步处理器 await；同步处理器直接调用（不包装 to_thread，避免延迟）。
        """
        if not self._enter_emit(event):
            return False
        try:
            with self._lock:
                handlers = list(self._handlers[event])
                self._handlers[event] = [
                    h for h in self._handlers[event] if not h.once
                ]
            if not handlers:
                return False
            executed = False
            for h in handlers:
                try:
                    if h.is_async:
                        await h.fn(event, data)
                    else:
                        h.fn(event, data)
                    executed = True
                except Exception:
                    logger.exception("异步处理器异常 (event=%s, fn=%s)",
                                     event, getattr(h.fn, "__name__", "?"))
            return executed
        finally:
            self._leave_emit(event)
    # ════════════════ 查询 ════════════════
    def handler_count(self, event: EventKey) -> int:
        with self._lock:
            return len(self._handlers[event])
    def clear(self, event: Optional[EventKey] = None):
        """清除指定事件或全部处理器。"""
        with self._lock:
            if event is None:
                self._handlers.clear()
            else:
                self._handlers.pop(event, None)
# 全局单例
bus = EventBus()