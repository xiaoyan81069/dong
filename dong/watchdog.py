"""
冬 · 独立看门狗进程
- 监控冬(:8899)和QQ(:3000)的存活状态
- 冬挂 → QQ通知主人 + 自动重启
- QQ挂 → 微信桌面通知（VLM视觉识别导航）
- 完全独立进程，不 import 冬的任何模块

用法: pythonw dong/watchdog.py
"""
import base64
import io
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import requests

# ═══════════════ 配置 ═══════════════
DASHBOARD_URL = "http://127.0.0.1:8899/status.json"
ONEBOT_HTTP   = "http://127.0.0.1:3000"
PET_URL       = "http://127.0.0.1:5120/api/ping"
MASTER_UID    = 1592741204

DONG_DIR   = Path(__file__).resolve().parent.parent

def _find_napcat_dir():
    """自动检测NapCat目录"""
    candidates = [
        os.environ.get("NAPCAT_DIR", ""),
        r"D:\NapCatQQ",
        os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "NapCatQQ"),
    ]
    for d in candidates:
        if d and os.path.isdir(d):
            return d
    return None

def _find_pythonw():
    """自动检测pythonw.exe路径"""
    # 优先用当前解释器同目录的pythonw
    base = Path(sys.executable).parent
    for name in ("pythonw.exe", "pythonw"):
        pw = base / name
        if pw.exists():
            return str(pw)
    return sys.executable  # 降级到python.exe

NAPCAT_DIR = _find_napcat_dir() or ""
PYTHONW    = _find_pythonw()
NODE_EXE   = os.path.join(NAPCAT_DIR, "node.exe") if NAPCAT_DIR else ""

CHECK_INTERVAL     = 30
DONG_DOWN_CONFIRM  = 3
QQ_DOWN_CONFIRM    = 3
RESTART_COOLDOWN   = 120

LOG_FILE = DONG_DIR / "dong" / "watchdog.log"

# 加载环境变量
_ENV_FILE = DONG_DIR / "dong" / ".env"
if _ENV_FILE.exists():
    with open(_ENV_FILE, "r", encoding="utf-8") as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                if _key.strip() and _val.strip():
                    os.environ.setdefault(_key.strip(), _val.strip())

# VLM 配置（用于微信桌面视觉导航）
VLM_API_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
VLM_MODEL    = "qwen3.5-omni-flash"

# ═══════════════ 日志 ═══════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("watchdog")


# ═══════════════ 存活检测 ═══════════════
def _check_port(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def check_dashboard() -> bool:
    """冬仪表盘是否响应"""
    try:
        r = requests.get(DASHBOARD_URL, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def check_onebot() -> bool:
    """QQ OneBot HTTP 是否响应"""
    try:
        r = requests.get(f"{ONEBOT_HTTP}/get_login_info", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def check_pet() -> bool:
    """Pet 桌宠连接器是否响应"""
    try:
        r = requests.get(PET_URL, timeout=2)
        return r.status_code == 200
    except Exception:
        return False


# ═══════════════ 通知 ═══════════════
def notify_via_qq(message: str) -> bool:
    """通过 OneBot HTTP API 发私聊给主人"""
    try:
        r = requests.post(
            f"{ONEBOT_HTTP}/send_private_msg",
            json={"user_id": MASTER_UID, "message": f"[看门狗] {message}"},
            timeout=5,
        )
        if r.status_code == 200:
            logger.info("QQ通知已发送: %s", message)
            return True
        logger.warning("QQ通知失败 HTTP %s", r.status_code)
        return False
    except Exception as e:
        logger.warning("QQ通知异常: %s", e)
        return False


# ═══════════════ 微信桌面通知 ═══════════════
def _find_hwnd_by_title(keyword: str):
    """根据窗口标题关键词查找窗口句柄"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        result = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @WNDENUMPROC
        def _enum(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                title = buf.value
                if keyword.lower() in title.lower():
                    result.append(hwnd)
            return True

        user32.EnumWindows(_enum, 0)
        return result[0] if result else None
    except Exception:
        return None


def _force_foreground(hwnd: int) -> bool:
    """强制窗口到前台（绕过Windows前台锁定）"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return True
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        if cur_tid != fg_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, True)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        if cur_tid != fg_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
        time.sleep(0.3)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _ensure_window_focused(keyword: str, retries: int = 3) -> bool:
    """确保指定标题关键词的窗口被聚焦"""
    for _ in range(retries):
        hwnd = _find_hwnd_by_title(keyword)
        if hwnd and _force_foreground(hwnd):
            return True
        time.sleep(0.5)
    return False


def _do_launch_wechat() -> bool:
    """启动微信"""
    try:
        subprocess.Popen(["start", "weixin://"], shell=True)
        for _ in range(10):
            time.sleep(0.3)
            if _ensure_window_focused("微信"):
                return True
        return True  # 启动了但窗口没找到也算部分成功
    except Exception as e:
        logger.error("微信启动失败: %s", e)
        return False


def _clipboard_put(text: str) -> bool:
    """将文本放入剪贴板（GBK编码，绕过中文输入法）"""
    try:
        tmp = tempfile.NamedTemporaryFile(mode='wb', suffix='.txt', delete=False)
        tmp.write(text.encode('gbk', errors='replace'))
        tmp.close()
        # 安全剪贴板写入：用文件重定向代替shell管道
        with open(tmp.name, 'r', encoding='gbk', errors='replace') as _tf:
            subprocess.run(["clip"], stdin=_tf, timeout=5)
        os.unlink(tmp.name)
        return True
    except Exception as e:
        logger.error("剪贴板写入失败: %s", e)
        return False


# ═══════════════ VLM 视觉导航 ═══════════════
def _vlm_api_key() -> str:
    """获取VLM API密钥（优先Qwen，备选Ark）"""
    for k in ("DONG_API_KEY_QWEN", "DONG_API_KEY_ARK", "DONG_API_KEY_LONGCAT"):
        val = os.environ.get(k, "")
        if val and len(val) > 10:
            return val
    return ""


def _vlm_find_click(description: str, timeout: int = 15) -> bool:
    """
    截图 → 发送给VLM → 解析坐标 → 点击
    description: 要查找的UI元素描述（如"文件传输助手"、"聊天输入框"）
    """
    try:
        import pyautogui as _pg
        import ctypes

        api_key = _vlm_api_key()
        if not api_key:
            logger.error("VLM: 无可用API密钥")
            return False

        # 截图
        buf = io.BytesIO()
        _pg.screenshot().save(buf, format="PNG")
        img_b64 = base64.b64encode(buf.getvalue()).decode()

        # 获取屏幕尺寸，帮助VLM输出正确坐标
        w = ctypes.windll.user32.GetSystemMetrics(0)
        h = ctypes.windll.user32.GetSystemMetrics(1)

        resp = requests.post(
            f"{VLM_API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": VLM_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                        {"type": "text", "text": (
                            f"屏幕分辨率 {w}x{h}。"
                            f"请在截图中找到【{description}】的位置，只返回精确的像素坐标。"
                            f"格式: x=数字 y=数字"
                            f"如果找不到返回: NOT_FOUND"
                        )},
                    ],
                }],
                "max_tokens": 60,
                "temperature": 0.1,
            },
            timeout=timeout,
        )

        if resp.status_code != 200:
            logger.warning("VLM HTTP %s: %s", resp.status_code, resp.text[:200])
            return False

        content = resp.json()["choices"][0]["message"]["content"]
        logger.info("VLM响应: %s", content[:100])

        m = re.search(r"x\s*[=:：]\s*(\d+).*?y\s*[=:：]\s*(\d+)", content, re.IGNORECASE)
        if not m:
            logger.warning("VLM未返回有效坐标")
            return False

        x, y = int(m.group(1)), int(m.group(2))
        # 边界保护
        x = max(0, min(x, w - 1))
        y = max(0, min(y, h - 1))
        logger.info("VLM定位(%d,%d) → 点击", x, y)
        _pg.click(x, y)
        return True

    except Exception as e:
        logger.warning("VLM异常: %s", e)
        return False


def _do_type_to_wechat(text: str, contact: str = "文件传输助手") -> bool:
    """向微信发送消息 —— VLM视觉导航版
    流程：聚焦微信 → VLM找联系人并点击 → VLM找输入框并点击 → 粘贴 → 发送"""
    import pyautogui as _pg

    if not _ensure_window_focused("微信"):
        logger.warning("无法聚焦微信窗口")
        return False
    time.sleep(0.5)

    try:
        # 1. VLM找到联系人并点击
        logger.info("VLM查找: %s", contact)
        if not _vlm_find_click(contact):
            return False
        time.sleep(0.8)

        # 2. VLM找到输入框并点击
        if not _vlm_find_click("微信聊天输入框"):
            return False
        time.sleep(0.3)

        # 3. 粘贴消息并发送
        if not _clipboard_put(text):
            return False
        _pg.hotkey("ctrl", "v")
        time.sleep(0.2)
        _pg.press("enter")
        return True

    except Exception as e:
        logger.error("微信VLM通知失败: %s", e)
        return False


def notify_via_wechat(message: str) -> bool:
    """微信通知：OCR定位 + 点击 + 输入（不用盲打键盘）"""
    logger.info("尝试微信通知...")
    try:
        from .wechat_ocr_helper import send_wechat_message_via_ocr
        return send_wechat_message_via_ocr(f"[冬看门狗] {message}")
    except ImportError as e:
        logger.warning("OCR模块不可用: %s，降级到键盘方式", e)
        if not _do_launch_wechat():
            return False
        time.sleep(2)
        return _do_type_to_wechat(f"[冬看门狗] {message}")


# ═══════════════ 重启 ═══════════════
_last_restart_time: float = 0.0
_restart_attempts: int = 0


def try_restart_dong() -> bool:
    """尝试重启冬进程"""
    global _last_restart_time, _restart_attempts
    now = time.monotonic()

    if now - _last_restart_time < RESTART_COOLDOWN:
        logger.info("重启冷却中，跳过 (距上次 %.0fs)", now - _last_restart_time)
        return False

    _last_restart_time = now
    _restart_attempts += 1
    logger.info("尝试重启冬 (第%d次)...", _restart_attempts)

    try:
        subprocess.Popen(
            [PYTHONW, "-m", "dong"],
            cwd=str(DONG_DIR),
            creationflags=0x08000000 if sys.platform == "win32" else 0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("冬启动命令已发送")
        return True
    except Exception as e:
        logger.error("冬重启失败: %s", e)
        return False


def try_restart_napcat() -> bool:
    """尝试重启 NapCat QQ"""
    logger.info("尝试重启 NapCat...")
    try:
        node_exe = os.path.join(NAPCAT_DIR, "node.exe")
        index_js = os.path.join(NAPCAT_DIR, "index.js")
        if not os.path.exists(node_exe) or not os.path.exists(index_js):
            logger.error("NapCat 文件不存在")
            return False
        subprocess.Popen(
            [node_exe, "--max-old-space-size=256", "./index.js", "-q", os.environ.get("NAPCAT_QQ", "2020382280")],
            cwd=NAPCAT_DIR,
            creationflags=0x08000000,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("NapCat 启动命令已发送")
        return True
    except Exception as e:
        logger.error("NapCat 重启失败: %s", e)
        return False


# ═══════════════ 主循环 ═══════════════
def main():
    logger.info("═══ 冬·看门狗 启动 ═══")
    logger.info("监控: 仪表盘=%s  OneBot=%s", DASHBOARD_URL, ONEBOT_HTTP)

    consecutive_dong = 0
    consecutive_qq   = 0
    dong_notified    = False  # 本次故障是否已通知
    dong_restarted   = False  # 本次故障是否已尝试重启

    while True:
        dong_ok = check_dashboard()
        qq_ok   = check_onebot()
        pet_ok  = check_pet()

        status_line = f"冬={'OK' if dong_ok else 'DOWN'}  QQ={'OK' if qq_ok else 'DOWN'}  Pet={'OK' if pet_ok else 'DOWN'}"
        logger.info(status_line)

        # ── 冬状态处理 ──
        if not dong_ok:
            consecutive_dong += 1
            logger.warning("冬无响应 (%d/%d)", consecutive_dong, DONG_DOWN_CONFIRM)

            if consecutive_dong >= DONG_DOWN_CONFIRM:
                if not dong_notified:
                    msg = f"冬进程异常（已{consecutive_dong * CHECK_INTERVAL}秒无响应）"
                    if qq_ok:
                        notify_via_qq(msg + "，正在尝试自动重启...")
                    else:
                        notify_via_wechat(msg + "，QQ也已断连，正在尝试重启...")
                    dong_notified = True

                if not dong_restarted:
                    try_restart_dong()
                    dong_restarted = True
        else:
            if consecutive_dong > 0:
                logger.info("冬已恢复")
                if dong_notified:
                    notify_via_qq("冬已恢复运行 ✓")
            consecutive_dong = 0
            dong_notified    = False
            dong_restarted   = False

        # ── QQ状态处理 ──
        if not qq_ok:
            consecutive_qq += 1
            logger.warning("QQ无响应 (%d/%d)", consecutive_qq, QQ_DOWN_CONFIRM)

            if consecutive_qq >= QQ_DOWN_CONFIRM and dong_ok:
                # QQ挂了但冬还在 → 冬自己会处理QQ重连，看门狗只记录
                logger.warning("QQ持续无响应，冬应自行重连中...")
                try_restart_napcat()
        else:
            consecutive_qq = 0

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
