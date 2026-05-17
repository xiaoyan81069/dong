"""旅行青蛙桌宠 — 纯透明窗口, 角色扒在桌面上, 向妹居物语看齐"""
import sys
import os
import random
import math

from PyQt5.QtCore import Qt, QTimer, QPoint, pyqtSignal
from PyQt5.QtGui import QPixmap, QCursor, QPainter, QColor, QRadialGradient, QBrush
from PyQt5.QtWidgets import QWidget, QLabel, QMenu, QAction, QApplication, QVBoxLayout

PET_W, PET_H = 128, 128          # 小窗
BIG_W, BIG_H = 200, 200          # 大窗（双击切换）

ROLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "res", "role")


# ═══════════════════════════════════════════════
# 独立气泡窗口 — 抄自妹居 ChatWidget
# ═══════════════════════════════════════════════
class ChatBubble(QWidget):
    """独立 frameless Tool 窗口，文字渲染完美"""

    def __init__(self, text="", duration_ms=0, thinking=False):
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        display = f"[思考] {text}" if thinking else text
        self._label = QLabel(display)
        self._label.setWordWrap(True)
        bg = "rgba(60, 60, 90, 230)" if thinking else "rgba(0, 0, 0, 210)"
        self._label.setStyleSheet(f"""
            QLabel {{
                background-color: {bg};
                color: white;
                border-radius: 10px;
                padding: 10px 14px;
                font-size: 14px;
            }}
        """)
        self._label.setMaximumWidth(280)
        layout.addWidget(self._label)
        self.adjustSize()

        if duration_ms > 0:
            QTimer.singleShot(duration_ms, self.close)

    def mousePressEvent(self, event):
        self.close()


# ═══════════════════════════════════════════════
# 青蛙主窗口
# ═══════════════════════════════════════════════
class FrogPet(QWidget):
    companion_brain = None
    double_clicked = pyqtSignal()

    def __init__(self):
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SubWindow
        super().__init__(None, flags)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._is_big = False           # 小窗/大窗
        self._is_companion = False
        self._voice_enabled = True
        self._border_shown = False

        # ── 加载图片 ──
        self._frames = self._load_frames()
        self._frame_idx = 0
        self._anim_timer_ms = 0
        self._displayed_idx = -1

        # ── 角色渲染 ──
        self._current_pixmap = None
        self._char_x = 0
        self._char_y = 0
        self._show_frame(0)

        self.setFixedSize(PET_W, PET_H)

        # ── 聊天气泡 (独立窗口) ──
        self._bubble_win = None
        self._think_win = None

        # ── 长按检测 ──
        self._press_pos = QPoint()
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.timeout.connect(self._toggle_companion)
        self._press_moved = False

        # ── 漂浮动画 ──
        self._float_t = random.random() * 3000
        self._base_y = 0
        self._last_float_off = 0

        # ── 眨眼 ──
        self._blink_cd = random.randint(3000, 8000)
        self._blinking = False
        self._blink_left = 0

        # ── 右键菜单 ──
        self._menu = QMenu(self)
        self._companion_act = QAction("开始陪伴", self._menu)
        self._companion_act.triggered.connect(self._toggle_companion)
        self._menu.addAction(self._companion_act)
        self._menu.addSeparator()
        self._voice_act = QAction("关闭语音", self._menu)
        self._voice_act.triggered.connect(self._toggle_voice)
        self._menu.addAction(self._voice_act)
        self._translate_act = QAction("翻译屏幕", self._menu)
        self._translate_act.triggered.connect(self._do_translate)
        self._menu.addAction(self._translate_act)
        self._talk_act = QAction("语音对话", self._menu)
        self._talk_act.triggered.connect(self._do_voice_chat)
        self._menu.addAction(self._talk_act)
        self._menu.addSeparator()
        self._border_act = QAction("拖动青蛙", self._menu)
        self._border_act.triggered.connect(self._toggle_border)
        self._menu.addAction(self._border_act)
        self._menu.addSeparator()
        quit_act = QAction("退出", self._menu)
        quit_act.triggered.connect(self._quit)
        self._menu.addAction(quit_act)

        # ── 主循环 ──
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    # ═══════════════════════════════════════════════
    # 图片
    # ═══════════════════════════════════════════════
    def _load_frames(self) -> list:
        folder = os.path.join(ROLE_DIR, "青蛙", "卧室", "action")
        if not os.path.isdir(folder):
            return []
        files = [f for f in os.listdir(folder) if f.endswith(".png")]
        files.sort(key=lambda n: int(n.replace(".png", "")))
        frames = []
        for f in files:
            pix = QPixmap(os.path.join(folder, f))
            if not pix.isNull():
                frames.append(pix)
        return frames

    def _show_frame(self, idx: int):
        if not self._frames:
            return
        idx = idx % len(self._frames)
        if idx == self._displayed_idx:
            return  # 没变就不用重新缩放
        self._displayed_idx = idx
        pix = self._frames[idx]
        scaled = pix.scaled(self.width(), self.height(),
                            Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._current_pixmap = scaled
        self._char_x = (self.width() - scaled.width()) // 2
        self._char_y = (self.height() - scaled.height()) // 2
        self.update()

    # ═══════════════════════════════════════════════
    # 动画
    # ═══════════════════════════════════════════════
    def _tick(self):
        dt = 50
        frame_count = len(self._frames)
        if frame_count == 0:
            return

        # 眨眼
        if self._blinking:
            self._blink_left -= dt
            if self._blink_left <= 0:
                self._blinking = False
                self._blink_cd = random.randint(3000, 8000)
        else:
            self._blink_cd -= dt
            if self._blink_cd <= 0:
                self._blinking = True
                self._blink_left = 150

        # 帧选择
        if self._blinking:
            self._show_frame(1)
        elif frame_count >= 4:
            phase = int((self._anim_timer_ms % 4000) / 1000)
            if phase == 0:
                self._show_frame(2)
            elif phase == 2:
                self._show_frame(3)
            else:
                self._show_frame(0)
        else:
            self._show_frame(self._frame_idx)
        self._anim_timer_ms += dt

        # 漂浮
        if not self._border_shown and not self._is_companion:
            self._float_t += dt
            off = int(math.sin(self._float_t / 3000 * 2 * math.pi) * 3)
            if off != self._last_float_off:
                self._last_float_off = off
                self.move(self.x(), self._base_y + off)

    # ═══════════════════════════════════════════════
    # 绘制 — 只画青蛙，无背景
    # ═══════════════════════════════════════════════
    def paintEvent(self, event):
        if not self._current_pixmap or self._current_pixmap.isNull():
            return
        p = QPainter(self)
        # 柔光底（对抗深色桌面）
        cx = self._char_x + self._current_pixmap.width() // 2
        cy = self._char_y + self._current_pixmap.height() // 2
        r = max(self._current_pixmap.width(), self._current_pixmap.height()) // 2 + 8
        grad = QRadialGradient(cx, cy, r)
        grad.setColorAt(0.0, QColor(255, 255, 255, 160))
        grad.setColorAt(0.5, QColor(255, 255, 255, 40))
        grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(grad))
        p.setPen(Qt.NoPen)
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
        # 角色
        p.drawPixmap(self._char_x, self._char_y, self._current_pixmap)
        p.end()

    # ═══════════════════════════════════════════════
    # 双击切换大小
    # ═══════════════════════════════════════════════
    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        if self._is_companion or self._border_shown:
            return
        self._is_big = not self._is_big
        self._displayed_idx = -1  # 强制重新缩放
        cx = self.x() + self.width() // 2
        cy = self.y() + self.height() // 2
        self.hide()
        if self._is_big:
            self.setFixedSize(BIG_W, BIG_H)
        else:
            self.setFixedSize(PET_W, PET_H)
        self.move(cx - self.width() // 2, cy - self.height() // 2)
        self._base_y = self.y()
        self._float_t = 0
        self._last_float_off = 0
        self.show()
        self.double_clicked.emit()

    # ═══════════════════════════════════════════════
    # 滚轮缩放
    # ═══════════════════════════════════════════════
    def wheelEvent(self, event):
        if self._border_shown or self._is_companion:
            return
        self._displayed_idx = -1
        delta = event.angleDelta().y()
        factor = 1.15 if delta > 0 else 0.85
        new_w = max(64, min(300, int(self.width() * factor)))
        new_h = max(64, min(300, int(self.height() * factor)))
        cx = self.x() + self.width() // 2
        cy = self.y() + self.height() // 2
        self.setFixedSize(new_w, new_h)
        self.move(cx - new_w // 2, cy - new_h // 2)

    # ═══════════════════════════════════════════════
    # 鼠标 — 右键菜单 / 左键长按
    # ═══════════════════════════════════════════════
    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._companion_act.setText("结束陪伴" if self._is_companion else "开始陪伴")
            self._voice_act.setText("开启语音" if not self._voice_enabled else "关闭语音")
            self._border_act.setText("锁定位置" if self._border_shown else "拖动青蛙")
            self._translate_act.setVisible(self._is_companion)
            self._talk_act.setVisible(self._is_companion)
            self._menu.popup(QCursor.pos())
            return
        if event.button() == Qt.LeftButton and not self._border_shown:
            self._press_pos = event.pos()
            self._press_moved = False
            self._long_press_timer.start(1000)
            event.accept()

    def mouseMoveEvent(self, event):
        if self._long_press_timer.isActive():
            delta = event.pos() - self._press_pos
            if delta.manhattanLength() > 5:
                self._press_moved = True
                self._long_press_timer.stop()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._long_press_timer.stop()

    # ═══════════════════════════════════════════════
    # 边框拖拽 — 照抄妹居
    # ═══════════════════════════════════════════════
    def _toggle_border(self):
        self._border_shown = not self._border_shown
        self.hide()
        if self._border_shown:
            self.setWindowFlags(Qt.SubWindow | Qt.WindowStaysOnTopHint)
            self._border_act.setText("锁定位置")
        else:
            self.setWindowFlags(
                Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SubWindow)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self._border_act.setText("拖动青蛙")
            self._base_y = self.y()
            self._float_t = 0
            self._last_float_off = 0
        self.show()

    # ═══════════════════════════════════════════════
    # 陪伴模式
    # ═══════════════════════════════════════════════
    def _toggle_companion(self):
        if self._press_moved:
            return
        self._is_companion = not self._is_companion

        if self._is_companion:
            self._enter_companion()
        else:
            self._exit_companion()

    def _enter_companion(self):
        from companion import ScreenWorker, WindowTracker

        # 启动截屏线程 (先隐藏青蛙 → 截屏 → 显示)
        self._screen_worker = ScreenWorker()
        self._screen_worker.hide_pet.connect(self.hide)
        self._screen_worker.show_pet.connect(self.show)
        self._screen_worker.screenshot_ready.connect(
            self.companion_brain.on_screenshot
        )
        self._screen_worker.start()

        self._window_tracker = WindowTracker()
        self._window_tracker.window_changed.connect(
            self.companion_brain.on_window_change
        )
        self._window_tracker.start()

        self._companion_act.setText("结束陪伴")
        # 停下漂浮
        self._last_float_off = 0

    def _exit_companion(self):
        if hasattr(self, '_screen_worker') and self._screen_worker:
            self._screen_worker.requestInterruption()
            self._screen_worker.quit()
            self._screen_worker.wait(2000)
        if hasattr(self, '_window_tracker') and self._window_tracker:
            self._window_tracker.requestInterruption()
            self._window_tracker.quit()
            self._window_tracker.wait(2000)

        self._companion_act.setText("开始陪伴")
        self._hide_bubble()
        self._base_y = self.y()
        self._float_t = 0
        self._last_float_off = 0

    # ═══════════════════════════════════════════════
    # 菜单功能
    # ═══════════════════════════════════════════════
    def _toggle_voice(self):
        self._voice_enabled = not self._voice_enabled
        self._voice_act.setText("开启语音" if not self._voice_enabled else "关闭语音")

    def _do_translate(self):
        if not self._is_companion or not self.companion_brain:
            return
        self._show_thinking("正在翻译屏幕...")
        self.companion_brain.translate_screen()

    def _do_voice_chat(self):
        if not self._is_companion or not self.companion_brain:
            return
        self.companion_brain.voice_chat()

    # ═══════════════════════════════════════════════
    # 气泡 (独立窗口)
    # ═══════════════════════════════════════════════
    def _show_bubble(self, text):
        self._hide_bubble()
        wait_ms = max(len(text) * 280, 3000)
        self._bubble_win = ChatBubble(text=text, duration_ms=wait_ms)
        self._position_bubble(self._bubble_win)

    def _show_thinking(self, text="正在思考..."):
        self._hide_bubble()
        self._think_win = ChatBubble(text=text, thinking=True, duration_ms=0)
        self._position_bubble(self._think_win)

    def _hide_bubble(self):
        if self._think_win:
            self._think_win.close()
            self._think_win = None
        if self._bubble_win:
            self._bubble_win.close()
            self._bubble_win = None

    def _position_bubble(self, bubble):
        bx = self.x() + (self.width() - bubble.width()) // 2
        by = self.y() - bubble.height() - 8
        if by < 0:
            by = self.y() + self.height() + 8
        bubble.move(bx, by)
        bubble.show()

    # ═══════════════════════════════════════════════
    # 退出
    # ═══════════════════════════════════════════════
    def _quit(self):
        if self._is_companion:
            self._exit_companion()
        self._hide_bubble()
        self.close()
        sys.exit()
