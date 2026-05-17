"""
L2-L4 游戏窗口 — 400x600 无边框 QWidget，内嵌 QWebEngineView
Phase 9: 圆角阴影 + 位置记忆 + 弹出动画
"""
import os
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QApplication, QGraphicsDropShadowEffect
from PyQt5.QtCore import Qt, QUrl, pyqtSignal, QPropertyAnimation, QPoint, QEasingCurve
from PyQt5.QtGui import QColor
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWebChannel import QWebChannel

from utils import load_window_state, save_window_state


class GameWindow(QWidget):
    close_requested = pyqtSignal()
    minimize_requested = pyqtSignal()

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setFixedSize(400, 600)
        self.setAttribute(Qt.WA_TranslucentBackground)

        # 窗口阴影
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 80))
        shadow.setOffset(0, 5)
        self.setGraphicsEffect(shadow)

        # 布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Web 引擎视图
        self.webview = QWebEngineView()
        self.webview.page().setBackgroundColor(Qt.transparent)

        # QWebChannel
        self.channel = QWebChannel()
        self.channel.registerObject("bridge", self.bridge)
        self.webview.page().setWebChannel(self.channel)

        # 加载本地 HTML
        html_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
        self.webview.load(QUrl.fromLocalFile(os.path.abspath(html_path)))
        layout.addWidget(self.webview)

        # 初始位置：读取记忆，否则居中
        saved = load_window_state().get("game_pos")
        if saved:
            self.move(saved[0], saved[1])
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move((screen.width() - 400) // 2, (screen.height() - 600) // 2)

        self._drag_pos = None

    def show_with_animation(self, pet_pos=None):
        """从小人位置弹出"""
        self.show()
        if pet_pos:
            start = QPoint(pet_pos.x() - 160, pet_pos.y() - 300)
            end = self.pos()
            self.anim = QPropertyAnimation(self, b"pos")
            self.anim.setDuration(300)
            self.anim.setStartValue(start)
            self.anim.setEndValue(end)
            self.anim.setEasingCurve(QEasingCurve.OutCubic)
            self.anim.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self._drag_pos:
            self.move(event.globalPos() - self._drag_pos)

    def moveEvent(self, event):
        super().moveEvent(event)
        state = load_window_state()
        state["game_pos"] = [self.pos().x(), self.pos().y()]
        save_window_state(state)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close_requested.emit()
        elif event.key() == Qt.Key_M:
            self.minimize_requested.emit()
        super().keyPressEvent(event)
