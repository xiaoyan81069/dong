"""
微信VLM导航助手 —— 独立于冬，看门狗专用
VLM截图识别+点击微信UI元素 发送通知消息

用法:
    from wechat_ocr_helper import send_wechat_message
    send_wechat_message("冬挂了，快来看看")
"""
import base64
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from typing import Optional

import pyautogui
import requests

logger = logging.getLogger("wechat_vlm")

# ── VLM配置 ──
_VLM_KEY_ENV = "DONG_API_KEY_QWEN"
_VLM_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_VLM_MODEL = "qwen3.5-omni-flash"


def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())


# ═══════════════ 窗口操作 ═══════════════
def _find_wechat():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, "微信")
    if not hwnd:
        return None
    # 隐藏到托盘的窗口 → 先显示
    if not user32.IsWindowVisible(hwnd):
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        time.sleep(0.3)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)
    return hwnd


def _force_foreground(hwnd: int):
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    try:
        # 先确保窗口可见（微信常隐藏到托盘，IsVisible=0）
        if not user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            time.sleep(0.3)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.3)
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        if cur_tid != fg_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, True)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        if cur_tid != fg_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
        time.sleep(0.4)
    except Exception:
        pass


def _show_desktop():
    """Win+D 最小化所有窗口，避免干扰"""
    import ctypes
    user32 = ctypes.windll.user32
    user32.keybd_event(0x5B, 0, 0, 0)  # Win
    user32.keybd_event(0x44, 0, 0, 0)  # D
    user32.keybd_event(0x44, 0, 2, 0)  # D up
    user32.keybd_event(0x5B, 0, 2, 0)  # Win up
    time.sleep(0.5)


def _ensure_wechat_front():
    """确保微信在最前面并可见
    微信常隐藏到系统托盘，API ShowWindow有时无效，用Ctrl+Alt+W快捷键唤出"""
    import ctypes
    user32 = ctypes.windll.user32

    hwnd = _find_wechat()
    if not hwnd:
        return None

    # 如果窗口不可见，用微信快捷键唤出
    if not user32.IsWindowVisible(hwnd):
        pyautogui.hotkey("ctrl", "alt", "w")
        time.sleep(1.0)
        # 再次检查
        if not user32.IsWindowVisible(hwnd):
            # API兜底
            user32.ShowWindow(hwnd, 1)  # SW_SHOWNORMAL
            time.sleep(0.3)
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            time.sleep(0.3)

    _force_foreground(hwnd)
    time.sleep(0.4)
    return hwnd if user32.IsWindowVisible(hwnd) else None


# ═══════════════ 剪贴板 ═══════════════
def _clipboard_put(text: str) -> bool:
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
        logger.error("剪贴板: %s", e)
        return False


# ═══════════════ VLM ═══════════════
def _vlm_ask(question: str, expect_json: bool = False) -> str:
    """截图→VLM→返回文本"""
    api_key = os.environ.get(_VLM_KEY_ENV, "")
    if len(api_key) < 10:
        return ""

    img = pyautogui.screenshot()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode()

    try:
        payload = {
            "model": _VLM_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                    {"type": "text", "text": question},
                ],
            }],
            "max_tokens": 100,
            "temperature": 0.1,
        }
        if expect_json:
            payload["response_format"] = {"type": "json_object"}

        r = requests.post(
            f"{_VLM_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        logger.debug("VLM HTTP %s", r.status_code)
        return ""
    except Exception as e:
        logger.debug("VLM异常: %s", e)
        return ""


def _vlm_extract_coords(description: str) -> Optional[tuple]:
    """
    截图→VLM找元素→返回屏幕绝对坐标(x,y)
    关键是VLM看到全屏所以返回的就是绝对坐标，不需要换算
    """
    content = _vlm_ask(
        f"截图中{description}。"
        f"返回该元素中心的像素坐标。"
        f"格式: x=数字 y=数字",
    )
    logger.debug("VLM响应: %s", (content or "")[:150])

    if not content:
        return None

    # 尝试多种格式解析
    # 1. x=数字 y=数字
    m = re.search(r'x\s*[=:：]\s*(\d+).*?y\s*[=:：]\s*(\d+)', content, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 2. JSON格式 {"x": num, "y": num} (可能在markdown中)
    content_clean = re.sub(r'```\w*\n?', '', content).replace('```', '')
    # 修复畸形JSON如 {"x": 491, 200}
    content_clean = re.sub(r'"x":\s*(\d+),\s*(\d+)', r'"x": \1, "y": \2', content_clean)
    try:
        data = json.loads(content_clean)
        if isinstance(data, dict) and "x" in data and "y" in data:
            return int(data["x"]), int(data["y"])
    except json.JSONDecodeError:
        pass

    # 3. 数组格式 [{"x": ..., "y": ...}]
    for m in re.finditer(r'\{[^{}]*"x"[^{}]*"y"[^{}]*\}', content_clean):
        try:
            data = json.loads(m.group())
            if "x" in data and "y" in data:
                return int(data["x"]), int(data["y"])
        except json.JSONDecodeError:
            continue

    return None


def _vlm_click(description: str) -> bool:
    """VLM找元素并点击"""
    coords = _vlm_extract_coords(description)
    if not coords:
        logger.warning("VLM点击失败: %s", description)
        return False
    x, y = coords
    logger.info("点击(%d,%d) → %s", x, y, description)
    pyautogui.click(x, y)
    return True


# ═══════════════ 主入口 ═══════════════
def send_wechat_message(message: str) -> bool:
    """
    向微信"文件传输助手"发送消息。
    纯键盘+固定点击方案，不依赖VLM坐标（VLM在搜索下拉遮挡时不可靠）
    流程: Ctrl+Alt+W唤出 → Ctrl+F搜索 → Down+Enter进聊天 → Esc关搜索 → 点击输入区 → 粘贴发送
    """
    _load_env()
    logger.info("微信通知: %s...", message[:40])

    # 0. 激活微信
    hwnd = _ensure_wechat_front()
    if not hwnd:
        logger.error("微信未找到或无法显示")
        return False

    import ctypes
    user32 = ctypes.windll.user32
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    wxL, wxT, wxR, wxB = rect.left, rect.top, rect.right, rect.bottom
    wxW, wxH = wxR - wxL, wxB - wxT
    logger.info("微信: (%d,%d)-(%d,%d) %dx%d", wxL, wxT, wxR, wxB, wxW, wxH)

    # 1. Ctrl+F 搜索
    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.4)

    if not _clipboard_put("文件传输助手"):
        return False
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.8)

    # 2. Down → Enter 打开聊天
    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(1.2)

    # 3. Esc 关闭搜索面板（关键！搜索面板遮挡会影响后续操作）
    pyautogui.press("esc")
    time.sleep(0.5)

    # 4. 点击输入区（微信右下角固定位置）
    # 输入框位于窗口底部，大约 x=50% y=90%
    input_x = wxL + wxW // 2
    input_y = wxT + int(wxH * 0.88)
    logger.info("点击输入区 (%d,%d)", input_x, input_y)
    pyautogui.click(input_x, input_y)
    time.sleep(0.3)

    # 5. 粘贴 → 发送
    if not _clipboard_put(message):
        return False
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.3)
    pyautogui.press("enter")

    logger.info("微信通知流程完成")
    return True


# 兼容别名
send_wechat_message_via_ocr = send_wechat_message
