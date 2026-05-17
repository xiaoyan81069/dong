"""
屏幕占用检测 —— 用户在用电脑时，冬不能碰桌面操作

Windows: GetLastInputInfo 检测距上次键盘/鼠标操作的秒数
如果用户在过去30秒内操作过 → 桌面被占用
"""
import ctypes
from ctypes import wintypes
import time

# Windows GetLastInputInfo
_LAST_INPUT_INFO = None

def _init_last_input_info():
    global _LAST_INPUT_INFO
    if _LAST_INPUT_INFO is None:
        try:
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT),
                    ("dwTime", wintypes.DWORD),
                ]
            _LAST_INPUT_INFO = LASTINPUTINFO
        except Exception:
            _LAST_INPUT_INFO = False


def get_idle_seconds() -> float:
    """获取用户空闲秒数（距上次键盘/鼠标操作）
    失败返回 -1
    """
    _init_last_input_info()
    if not _LAST_INPUT_INFO:
        return -1

    try:
        lii = _LAST_INPUT_INFO()
        lii.cbSize = ctypes.sizeof(lii)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return millis / 1000.0
    except Exception:
        pass
    return -1


def is_desktop_occupied(idle_threshold: float = 30.0) -> bool:
    """桌面是否被用户占用？
    idle_threshold: 空闲多少秒以上算不占用（默认30秒）
    """
    idle = get_idle_seconds()
    if idle < 0:
        # 无法检测 → 保守策略：认为被占用，不操作
        return True
    return idle < idle_threshold


def get_occupancy_status() -> str:
    """获取桌面占用状态描述"""
    idle = get_idle_seconds()
    if idle < 0:
        return "无法检测用户活动状态"
    if idle < 30:
        return f"用户正在操作电脑（空闲{idle:.0f}秒），禁止桌面操作"
    return f"桌面空闲{idle:.0f}秒，可以操作"
