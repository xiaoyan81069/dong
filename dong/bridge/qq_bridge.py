"""
QQ → Claude Code 桥接
主人通过QQ发指令给Claude，Claude执行后回复

═══ 记忆隔离边界 ═══
Claude专用（冬绝不读取）:
  claude_dc_memory.json  — DC持久记忆
  claude_dc_log.jsonl    — DC操作日志
  claude_chat.log        — 桥接对话记录
  claude_resp.json       — 响应传递（阅后即焚）

冬专用（Claude只读不写）:
  dong_memory.json       — 冬的记忆
  chat_history.txt       — QQ聊天记录
  dong_*.json            — 冬的状态文件

共享（仅命令传递）:
  claude_cmd.json        — 指令传递
  claude_queue.jsonl     — 指令队列
"""
import asyncio, json, os, time, requests, threading
from ..log import log

# 配置
_DONG_DIR = os.path.dirname(os.path.dirname(__file__))
CHAT_FILE = os.path.join(_DONG_DIR, "chat_history.txt")
CMD_FILE = os.path.join(_DONG_DIR, "claude_cmd.json")
QUEUE_FILE = os.path.join(_DONG_DIR, "claude_queue.jsonl")
RESP_FILE = os.path.join(_DONG_DIR, "claude_resp.json")
from ..config import MASTER_UID, SENDBOT_API
CLAUDECMD_TIMEOUT = 120  # Claude处理超时（秒）

_last_pos = 0  # chat_history.txt读取位置
_bridge_lock = threading.Lock()  # 保护所有文件读写
_session_id = str(time.time())  # 本次桥接会话ID，用于区分新旧响应


def _get_new_messages():
    """读取chat_history.txt中主人发来的新消息"""
    global _last_pos
    msgs = []
    try:
        with _bridge_lock:
            with open(CHAT_FILE, "r", encoding="utf-8") as f:
                f.seek(max(_last_pos, 0))
                for line in f:
                    line = line.strip()
                    if f"QQ{MASTER_UID}:" in line:
                        parts = line.split(f"QQ{MASTER_UID}:", 1)
                        if len(parts) == 2:
                            msgs.append(parts[1].strip())
                _last_pos = f.tell()
    except Exception as e:
        log(f"读取新消息异常: {type(e).__name__}: {e}")
    return msgs


# ── 标识系统：所有Claude回复带前缀，主人一眼能分 ──
REPLY_PREFIX = "[C]"       # 纯Claude技术回复
REPLY_PREFIX_PERSONA = "[C冬]"  # 读了冬人格后的回复
READY_MARKER = " ✓"       # 就绪标记：主人看到这个就知道Claude处理完了
_current_prefix = REPLY_PREFIX

def set_persona_mode(on: bool = True):
    """切换回复标识：True=带上冬人格标记，False=纯Claude标记"""
    global _current_prefix
    _current_prefix = REPLY_PREFIX_PERSONA if on else REPLY_PREFIX
    return _current_prefix

def get_prefix() -> str:
    return _current_prefix

def append_ready(msg: str) -> str:
    """给消息末尾加就绪标记（防重复）"""
    if not msg.rstrip().endswith(READY_MARKER):
        return msg.rstrip() + READY_MARKER
    return msg


def _send_qq_reply(msg: str):
    """通过OneBot API回复主人，自动加标识前缀+就绪标记（防重复）"""
    try:
        if not msg.startswith("[C"):
            msg = f"{_current_prefix} {msg}"
        msg = append_ready(msg)
        requests.post(
            f"{SENDBOT_API}/send_private_msg",
            json={"user_id": MASTER_UID, "message": msg},
            timeout=10,
        )
    except Exception as e:
        log(f"发送QQ回复异常: {type(e).__name__}: {e}")


# ── 桥接对话日志（Cherry崩了也不丢上下文）──
LOG_FILE = os.path.join(_DONG_DIR, "claude_chat.log")

def log_conversation(sender: str, text: str):
    """记录桥接对话"""
    try:
        ts = time.strftime("%m-%d %H:%M:%S")
        with _bridge_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {sender}: {text[:500]}\n")
    except Exception as e:
        log(f"记录对话日志异常: {type(e).__name__}: {e}")

def read_recent_log(lines: int = 50) -> str:
    """读取最近N行对话日志"""
    if not os.path.exists(LOG_FILE):
        return "(空)"
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:])
    except Exception as e:
        log(f"读取对话日志异常: {type(e).__name__}: {e}")
        return "(读取失败)"


def append_command(text: str):
    """追加命令到队列（JSONL格式，不覆盖）"""
    log_conversation("主人", text)  # 记录对话
    cmd = {"text": text, "time": time.time(), "done": False}
    with _bridge_lock:
        with open(QUEUE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(cmd, ensure_ascii=False) + "\n")
    write_command(text)


def write_command(text: str):
    """写入待处理命令"""
    cmd = {"text": text, "time": time.time(), "done": False}
    with _bridge_lock:
        with open(CMD_FILE, "w", encoding="utf-8") as f:
            json.dump(cmd, f)


def read_command():
    """读取待处理命令"""
    with _bridge_lock:
        if not os.path.exists(CMD_FILE):
            return None
        with open(CMD_FILE, "r", encoding="utf-8") as f:
            cmd = json.load(f)
    if cmd.get("done"):
        return None
    return cmd


def mark_done():
    """标记命令已处理"""
    with _bridge_lock:
        if os.path.exists(CMD_FILE):
            with open(CMD_FILE, "r", encoding="utf-8") as f:
                cmd = json.load(f)
            cmd["done"] = True
            with open(CMD_FILE, "w", encoding="utf-8") as f:
                json.dump(cmd, f)


def write_response(text: str):
    """写入Claude的回复（带标识前缀+就绪标记+会话ID防过期）"""
    if not text.startswith("[C"):
        text = f"{_current_prefix} {text}"
    text = append_ready(text)
    log_conversation("Claude", text)
    with _bridge_lock:
        with open(RESP_FILE, "w", encoding="utf-8") as f:
            json.dump({"text": text, "time": time.time(), "sid": _session_id}, f)


def read_response():
    """读取Claude的回复"""
    with _bridge_lock:
        if not os.path.exists(RESP_FILE):
            return None
        with open(RESP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


# ── 冬调用：收到主人指令时转发给Claude ──
def on_master_command(text: str):
    """冬收到主人/d指令时调用，转发给Claude"""
    write_command(text)
    # 等Claude处理（最多 CLAUDECMD_TIMEOUT 秒），只认本会话的新响应
    deadline = time.time() + CLAUDECMD_TIMEOUT
    while time.time() < deadline:
        time.sleep(0.5)
        resp = read_response()
        if resp and resp.get("sid") == _session_id and resp.get("time", 0) > time.time() - CLAUDECMD_TIMEOUT:
            with _bridge_lock:
                if os.path.exists(RESP_FILE):
                    os.remove(RESP_FILE)
            # 带标识前缀返回
            return f"{_current_prefix} {resp['text']}"
    return None


# ── Claude调用：检查并回复待处理命令 ──
def check_and_handle():
    """Claude检查是否有待处理命令，有则执行并回复"""
    cmd = read_command()
    if not cmd:
        return None
    return cmd["text"]


async def bridge_loop():
    """桥接主循环：监控QQ消息 + claude_cmd.json，转发给Claude处理"""
    _last_cmd_time = 0.0
    while True:
        # ── 路径1：旧式 /c 和 @claude 消息转发（兼容）──
        msgs = _get_new_messages()
        for msg in msgs:
            if msg.startswith("/c ") or msg.startswith("@claude"):
                cmd = msg.replace("/c ", "").replace("@claude ", "").strip()
                write_command(cmd)
                for _ in range(60):
                    await asyncio.sleep(0.5)
                    resp = read_response()
                    if resp and resp.get("time", 0) > time.time() - 30:
                        _send_qq_reply(f"[Claude] {resp['text'][:500]}")
                        with _bridge_lock:
                            if os.path.exists(RESP_FILE):
                                os.remove(RESP_FILE)
                        break

        # ── 路径2：主动扫描claude_cmd.json（1秒一次）──
        try:
            with _bridge_lock:
                if os.path.exists(CMD_FILE):
                    with open(CMD_FILE, "r", encoding="utf-8") as f:
                        cmd = json.load(f)
                    cmd_time = cmd.get("time", 0)
                    if cmd_time > _last_cmd_time and not cmd.get("done", False):
                        _last_cmd_time = cmd_time
                        # 检测到新指令，等待Claude处理并转发回复
                        deadline = time.time() + CLAUDECMD_TIMEOUT
                        while time.time() < deadline:
                            await asyncio.sleep(1)
                            resp = read_response()
                            if resp and resp.get("sid") == _session_id and resp.get("time", 0) > cmd_time:
                                _send_qq_reply(resp["text"][:500])
                                with _bridge_lock:
                                    if os.path.exists(RESP_FILE):
                                        os.remove(RESP_FILE)
                                break
        except Exception:
            pass

        await asyncio.sleep(1)
