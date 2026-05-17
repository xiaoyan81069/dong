"""
Claude守护进程 — GLM方案四实现
watchdog监听cmd.json → Claude CLI处理 → 写resp.json → 冬发QQ
完全绕过Cherry Studio，后台静默运行
"""
import os, sys, json, time, subprocess, threading

_DONG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(_DONG_DIR)
CMD_FILE = os.path.join(_DONG_DIR, "claude_cmd.json")
RESP_FILE = os.path.join(_DONG_DIR, "claude_resp.json")
CLAUDE_CLI = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")
CLAUDECMD_TIMEOUT = 120
MAX_TURNS = 3

_lock = threading.Lock()
_last_processed = 0.0
_daemon_started = False


def process_command(text: str) -> str:
    """调用Claude CLI处理指令，返回文本结果"""
    try:
        proc = subprocess.run(
            [CLAUDE_CLI, "-p", text, "--max-turns", str(MAX_TURNS)],
            capture_output=True, text=True,
            timeout=CLAUDECMD_TIMEOUT,
            cwd=PROJECT_DIR,
            encoding="utf-8", errors="replace",
        )
        output = proc.stdout.strip()
        if proc.returncode != 0 and proc.stderr:
            output += f"\n[err: {proc.stderr[:200]}]"
        return output[:4000] if output else "(无输出)"
    except subprocess.TimeoutExpired:
        return "(Claude处理超时)"
    except FileNotFoundError:
        return f"(Claude CLI未找到: {CLAUDE_CLI})"
    except Exception as e:
        return f"(Claude调用失败: {e})"


def check_and_process():
    """检查cmd.json，有未处理指令则调CLI处理并写resp"""
    global _last_processed
    with _lock:
        try:
            if not os.path.exists(CMD_FILE):
                return
            with open(CMD_FILE, "r", encoding="utf-8") as f:
                cmd = json.load(f)
        except:
            return

        cmd_time = cmd.get("time", 0)
        if cmd.get("done", True) or cmd_time <= _last_processed:
            return

        _last_processed = cmd_time
        text = cmd.get("text", "")

    # 在锁外处理（可能耗时）
    result = process_command(text)

    # 写响应
    try:
        resp = {"text": result, "time": time.time(), "sid": f"daemon_{time.time()}"}
        with open(RESP_FILE, "w", encoding="utf-8") as f:
            json.dump(resp, f, ensure_ascii=False)
    except:
        pass


def start_daemon():
    """启动watchdog守护进程"""
    global _daemon_started
    if _daemon_started:
        return
    _daemon_started = True

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        # 降级：用轮询
        _start_polling_fallback()
        return

    class CmdHandler(FileSystemEventHandler):
        def on_modified(self, event):
            if event.is_directory:
                return
            if event.src_path.endswith("claude_cmd.json"):
                check_and_process()

    watch_dir = os.path.dirname(CMD_FILE)
    observer = Observer()
    observer.schedule(CmdHandler(), watch_dir, recursive=False)
    observer.daemon = True
    observer.start()

    # 启动时先处理一次积压
    check_and_process()

    # 保活（watchdog官方建议不要直接join daemon线程）
    try:
        while observer.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        observer.join()


def _start_polling_fallback():
    """watchdog不可用时的轮询降级方案"""
    def _poll():
        while True:
            check_and_process()
            time.sleep(2)

    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    check_and_process()


# ── CLI入口 ──
if __name__ == "__main__":
    print(f"Claude守护进程启动")
    print(f"  监听: {CMD_FILE}")
    print(f"  CLI: {CLAUDE_CLI}")
    print(f"  项目: {PROJECT_DIR}")
    start_daemon()
