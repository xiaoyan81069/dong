"""
QQ断线自救 — 通过微信通知主人
从 __init__.py robot_loop 底部提取
"""
import asyncio


async def do_qq_rescue():
    """执行QQ断线自救：打开微信通知主人。去重标记由调用方管理。"""
    from ..agent_loop import _do_launch, _do_type
    _do_launch("微信")
    await asyncio.sleep(3)
    _do_type("QQ断线了，我正在尝试重连...", "微信")
