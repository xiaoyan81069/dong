"""冬桌宠 · 统一入口
Live2D/Frog 角色 + 后端游戏数据 + 截屏陪伴
"""
import sys
import os
import tempfile
import ctypes
import threading
import time

import PyQt5.QtWebEngineWidgets  # 必须在 QApplication 之前导入
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QPen
from PyQt5.QtCore import Qt

from bridge import PetBridge  # 后端数据桥接（从 winter_pet 搬过来）
from companion import CompanionBrain

LOCK_PATH = os.path.join(tempfile.gettempdir(), "desktop_pet_v2.lock")
BACKEND_URL = "http://127.0.0.1:5120"


def _create_tray_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(180, 200, 230))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 12, 12)
    p.setPen(QPen(QColor(255, 255, 255), 3))
    p.drawLine(32, 12, 32, 52)
    p.drawLine(12, 32, 52, 32)
    p.drawLine(18, 18, 46, 46)
    p.drawLine(46, 18, 18, 46)
    p.end()
    return QIcon(pixmap)


def try_acquire_lock():
    our_pid = os.getpid()
    try:
        if os.path.exists(LOCK_PATH):
            with open(LOCK_PATH, "r") as f:
                old_pid = int(f.read().strip())
            try:
                import psutil
                if psutil.pid_exists(old_pid):
                    ctypes.windll.user32.AllowSetForegroundWindow(old_pid)
                    print(f"桌宠已在运行 (PID {old_pid})")
                    return False
            except ImportError:
                pass
        with open(LOCK_PATH, "w") as f:
            f.write(str(our_pid))
        return True
    except Exception:
        return True


def release_lock():
    try:
        if os.path.exists(LOCK_PATH):
            with open(LOCK_PATH, "r") as f:
                pid = int(f.read().strip())
            if pid == os.getpid():
                os.remove(LOCK_PATH)
    except Exception:
        pass


def main():
    use_frog = "--frog" in sys.argv

    if not try_acquire_lock():
        sys.exit(0)

    # 高分屏
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
    app = QApplication(sys.argv)
    app.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app.setApplicationName("冬 · 桌面宠物")
    app.setQuitOnLastWindowClosed(False)
    app.aboutToQuit.connect(release_lock)

    # ===== 后端桥接 =====
    bridge = PetBridge(BACKEND_URL)
    if bridge.check_backend():
        print("[Pet] 后端已连接")
    else:
        print("[Pet] 后端未启动，游戏功能暂不可用")
    bridge.start_polling(1000)

    # ===== L1 角色（Live2D 或 Frog）=====
    if use_frog:
        from pet import FrogPet
        pet = FrogPet()
    else:
        from live2d_pet import Live2DPet
        pet = Live2DPet()

    pet.companion_brain = CompanionBrain(pet)

    # 注入后端桥接到陪伴大脑
    pet.companion_brain.backend_bridge = bridge

    pet.move(100, 400)
    pet.show()

    # ===== L2-L4 游戏窗口 =====
    from game_window import GameWindow
    game = GameWindow(bridge)

    # 启动后端数据注入到陪伴脑
    def on_backend_state(json_str):
        import json as _json
        try:
            state = _json.loads(json_str)
            pet.companion_brain.on_backend_state(state)
        except Exception:
            pass

    bridge.state_updated.connect(on_backend_state)

    # 双击 → 展开游戏窗
    def on_double_click():
        game.show_with_animation(pet.pos())

    pet.double_clicked.connect(on_double_click)
    game.close_requested.connect(game.hide)

    # ===== 系统托盘 =====
    tray = QSystemTrayIcon(_create_tray_icon(), app)
    tray_menu = QMenu()
    show_act = tray_menu.addAction("显示小屋")
    show_act.triggered.connect(game.show)
    hide_act = tray_menu.addAction("隐藏小屋")
    hide_act.triggered.connect(game.hide)
    tray_menu.addSeparator()
    tray_menu.addAction("陪伴模式").setCheckable(True)
    tray_menu.addAction("退出冬").triggered.connect(app.quit)
    tray.setContextMenu(tray_menu)
    tray.setToolTip("冬 · 桌面宠物")
    tray.show()
    tray.activated.connect(
        lambda reason: game.show() if reason == QSystemTrayIcon.DoubleClick else None
    )

    print("[Pet] 冬桌宠已启动（Live2D + 游戏 + 陪伴）")
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
