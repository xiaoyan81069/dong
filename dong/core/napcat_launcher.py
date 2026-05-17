"""
NapCat 启动器 — 端口检测 / 进程启动 / 就绪等待
从 __init__.py L153-190 提取
"""
import os
import socket
import subprocess
import time

from ..config import NAPCAT_DIR, BASE_DIR
from ..log import log


def is_port_open(host='127.0.0.1', port=3001):
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except Exception:
        return False


def start_napcat():
    log("启动NapCat（无窗口）...")
    try:
        node_exe = os.path.join(NAPCAT_DIR, "node.exe")
        napcat_log = open(os.path.join(BASE_DIR, "napcat_stderr.log"), "a")
        subprocess.Popen(
            [node_exe, "--max-old-space-size=256", "./index.js", "-q", os.environ.get("NAPCAT_QQ", "2020382280")],
            cwd=NAPCAT_DIR,
            creationflags=0x08000000,
            stdout=subprocess.DEVNULL,
            stderr=napcat_log
        )
        log("NapCat启动命令已发送")
    except Exception as e:
        log(f"启动NapCat失败: {e}")


def wait_for_napcat(timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        if is_port_open(port=3001):
            log("NapCat已就绪")
            return True
        time.sleep(1)
    return False
