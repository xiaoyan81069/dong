"""
QQ → Claude CLI 直接桥接
冬收到/d c指令 → 调claude CLI → 返回结果 → QQ回复
不依赖Cherry Studio
"""
import subprocess, sys, os, json, time, threading

CLAUDE_CLI = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")
_DONG_DIR = os.path.dirname(os.path.dirname(__file__))
CMD_FILE = os.path.join(_DONG_DIR, "claude_cmd.json")
RESP_FILE = os.path.join(_DONG_DIR, "claude_resp.json")
_lock = threading.Lock()


def process_command(text: str, timeout: int = 120) -> str:
    """直接调Claude CLI处理指令"""
    try:
        proc = subprocess.run(
            [CLAUDE_CLI, "-p", text, "--max-turns", "3"],
            capture_output=True, text=True,
            timeout=timeout,
            cwd=os.path.dirname(os.path.dirname(__file__)),
            encoding="utf-8", errors="replace",
        )
        output = proc.stdout.strip()
        if proc.returncode != 0:
            output += f"\n[stderr: {proc.stderr[:200]}]"
        return output[:2000] if output else "Claude无输出"
    except subprocess.TimeoutExpired:
        return "Claude处理超时"
    except Exception as e:
        return f"Claude调用失败: {e}"


def write_command(text: str):
    cmd = {"text": text, "time": time.time(), "done": False}
    with open(CMD_FILE, "w", encoding="utf-8") as f:
        json.dump(cmd, f)


def write_response(text: str):
    with _lock:
        with open(RESP_FILE, "w", encoding="utf-8") as f:
            json.dump({"text": text, "time": time.time()}, f)


# ── 冬调用入口 ──
def on_master_command(text: str) -> str:
    """冬收到/d c指令时调用，直接调Claude CLI并返回结果"""
    write_command(text)
    result = process_command(text)
    write_response(result)
    return result
