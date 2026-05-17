"""
消息辅助函数 — 消息合并 / 测试回复生成
从 __init__.py L315-350 提取
"""
import asyncio
import random


def _merge_if_related(queue: list, uid: str) -> str:
    """基于时间窗口合并多条消息：短窗口内直接拼接，长间隔只取最后"""
    if len(queue) <= 1:
        return queue[0] if queue else ""
    # 简单策略：消息少于3条且都是短消息，直接合并
    if len(queue) <= 3 and all(len(m) < 50 for m in queue):
        return " ".join(queue)
    # 否则取最后一条，短窗口消息合并策略由上层处理
    return queue[-1]


async def _generate_test_reply(user_text: str) -> str:
    """测试模式下生成简单回复"""
    try:
        from ..api import chat as _chat_sync

        test_system = """你是冬，一个20岁女大学生。你在群里聊天。
说话方式：超级短！65%回复不超过7字，平均6.9字。最长不超过30字。
嘴硬心软、东北口音（嘎嘎/咋整/不儿）、多用反问否定（才没有/那咋了/不是）。
经常只回一个字（嗯/好/？/乐/行/啥/哦）。
你现在是AI测试版本。有人说你是AI就承认（"嗯，来测试的"），不要编造个人信息。"""

        reply = await asyncio.to_thread(
            _chat_sync, user_text, 0, test_system
        )
        if reply and len(reply) >= 1:
            # 截断过长回复
            return reply[:50]
    except Exception:
        pass
    # fallback
    return random.choice(["嗯", "？", "啥", "行", "好", "不儿", "那咋了", "哦"])
