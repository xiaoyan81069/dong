"""
/d c 指令处理：Claude会话模式 + Agent引擎
从 __init__.py 消息循环中提取
"""
import time, asyncio


async def handle_dc_command(ws, uid: int, text: str,
                            claude_sessions: dict, save_sessions,
                            send_msg, MASTER_UID: int) -> bool:
    """
    处理 /d c 相关消息。返回 True 表示消息已处理。
    """

    # ★ Claude会话模式：所有消息直接转发给Agent（最高优先级）
    if uid == MASTER_UID and uid in claude_sessions:
        if text.strip() == "/d exit":
            claude_sessions.pop(uid, None)
            save_sessions()
            await send_msg(ws, uid, "已退出Claude模式")
            return True

        # 子命令：清除记忆 / 查看状态
        if text.strip() == "/d c clear":
            from ..agent import clear_session
            result = clear_session(uid)
            await send_msg(ws, uid, result)
            return True
        if text.strip() == "/d c info":
            from ..agent import session_info
            result = session_info(uid)
            await send_msg(ws, uid, result)
            return True

        # 转发给Agent处理
        await send_msg(ws, uid, "智能体思考中...")
        from ..agent import on_agent_command
        result = await asyncio.to_thread(on_agent_command, text, uid)
        for i in range(0, len(result), 400):
            await send_msg(ws, uid, result[i:i+400])
            await asyncio.sleep(0.3)
        return True

    # ★ /d c <指令> → 单次调用Agent
    if text.startswith("/d c ") or text == "/d c":
        cmd = text[4:].strip() if text.startswith("/d c ") else ""

        # /d c（无参数）→ 进入Claude会话模式
        if not cmd:
            claude_sessions[uid] = time.time()
            save_sessions()
            await send_msg(ws, uid, "已进入Claude模式，直接发消息即可，/d exit退出")
            return True

        # 子命令：无需Agent
        if cmd == "clear":
            from ..agent import clear_session
            result = clear_session(uid)
            await send_msg(ws, uid, result)
            return True
        if cmd == "info":
            from ..agent import session_info
            result = session_info(uid)
            await send_msg(ws, uid, result)
            return True

        # 调Agent引擎
        claude_sessions[uid] = time.time()
        save_sessions()
        await send_msg(ws, uid, "智能体思考中...")
        from ..agent import on_agent_command
        result = await asyncio.to_thread(on_agent_command, cmd, uid)
        for i in range(0, len(result), 400):
            await send_msg(ws, uid, result[i:i+400])
            await asyncio.sleep(0.3)
        return True

    return False
