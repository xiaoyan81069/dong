"""Live2D 桌宠 — 使用 Pio 模型 (Cubism 2.1)，live2d.v2 纯 Python 实现"""
import sys
import os
import random
import math
import json
import ctypes

from PyQt5.QtCore import Qt, QTimer, QPoint, pyqtSignal
from PyQt5.QtGui import QSurfaceFormat, QCursor
from PyQt5.QtWidgets import (
    QOpenGLWidget, QMenu, QAction, QApplication,
)

# ── 导入 ChatBubble from pet ──
from pet import ChatBubble

# ── Live2D v2 ──
import live2d.v2 as live2d
from live2d.v2.lapp_define import MotionPriority
live2d.init()  # 必须在创建任何 live2d 对象之前调用

# ── 模型路径 (Pio Cubism 2 模型) ──
MODEL_DIR = os.path.join(os.path.dirname(__file__), "res", "role", "pio")
MODEL_JSON = os.path.join(MODEL_DIR, "model.json")

# 窗口尺寸
PET_W, PET_H = 200, 260
BIG_W, BIG_H = 220, 290
MIN_W, MIN_H = 100, 135
MAX_W, MAX_H = 350, 460


class Live2DPet(QOpenGLWidget):
    """Live2D 桌面宠物 — 透明窗口 + OpenGL 渲染"""
    companion_brain = None
    double_clicked = pyqtSignal()

    def __init__(self):
        # ── OpenGL 表面格式 ──
        fmt = QSurfaceFormat()
        fmt.setAlphaBufferSize(8)
        fmt.setSamples(0)
        QSurfaceFormat.setDefaultFormat(fmt)

        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        # ── 状态变量 ──
        self._is_big = False
        self._is_companion = False
        self._voice_enabled = True
        self._border_shown = False
        self._model_loaded = False
        self._model = None

        # ── 模型变换 ──
        self._model_scale = 1.0

        self.setFixedSize(PET_W, PET_H)

        # ── 聊天气泡 ──
        self._bubble_win = None
        self._think_win = None

        # ── 拖拽 & 长按检测 ──
        self._drag_start = QPoint()
        self._drag_active = False
        self._press_pos = QPoint()
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.timeout.connect(self._toggle_companion)
        self._press_moved = False

        # ── 漂浮动画 ──
        self._float_t = random.random() * 3000
        self._base_y = 0
        self._last_float_off = 0
        self._base_y_ready = False  # 首次 show 后再记录位置

        # ── 空闲动作 ──
        self._idle_timer = 0
        self._idle_interval = random.randint(5000, 15000)

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
        self._border_act = QAction("显示边框", self._menu)
        self._border_act.triggered.connect(self._toggle_border)
        self._menu.addAction(self._border_act)
        self._menu.addSeparator()
        quit_act = QAction("退出", self._menu)
        quit_act.triggered.connect(self._quit)
        self._menu.addAction(quit_act)

        # ── 主循环 60fps ──
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    # ═══════════════════════════════════════════════
    # OpenGL 初始化
    # ═══════════════════════════════════════════════
    def initializeGL(self):
        try:
            live2d.glInit()

            self._model = live2d.LAppModel()
            self._model.LoadModelJson(os.path.abspath(MODEL_JSON))
            self._model.SetAutoBreathEnable(True)
            self._model.SetAutoBlinkEnable(True)
            self._model.Resize(self.width(), self.height())

            # 获取可用动作组
            motion_names = self._model.modelSetting.getMotionNames()
            print(f"[Live2D] Pio 模型加载成功! 动作组: {motion_names}")

            # 根据画布大小适配缩放
            canvas_w, canvas_h = self._model.GetCanvasSizePixel()
            if canvas_w > 0 and canvas_h > 0:
                scale_w = self.width() / canvas_w
                scale_h = self.height() / canvas_h
                self._model_scale = min(scale_w, scale_h) * 0.85
                self._model.SetScale(self._model_scale)

            # 开始空闲动作
            self._start_random_idle()

            self._model_loaded = True
            self._update_hit_region()
            print("[Live2D] 初始化完成")

        except Exception as e:
            print(f"[Live2D] 初始化失败: {e}")
            import traceback
            traceback.print_exc()

    def paintGL(self):
        if not self._model_loaded:
            return
        live2d.clearBuffer(0.0, 0.0, 0.0, 0.0)
        self._model.Update()
        self._model.Draw()

    def resizeGL(self, w: int, h: int):
        if self._model_loaded:
            self._model.Resize(w, h)
        self._update_hit_region()

    # ═══════════════════════════════════════════════
    # 模型在窗口中的渲染区域 (窗口坐标)
    # ═══════════════════════════════════════════════
    def _model_rect(self):
        """返回模型渲染区域 (x, y, w, h)，窗口坐标，失败返回 None"""
        if not self._model_loaded:
            return None
        try:
            canvas_w, canvas_h = self._model.GetCanvasSizePixel()
            rw = max(1, int(canvas_w * self._model_scale))
            rh = max(1, int(canvas_h * self._model_scale))
            mx = (self.width() - rw) // 2
            my = (self.height() - rh) // 2
            return (mx, my, rw, rh)
        except Exception:
            return None

    def _update_hit_region(self):
        """尺寸变化时刷新 (当前用 SetWindowRgn 做点击穿透)"""
        if not self._model_loaded:
            return
        if self._border_shown:
            try:
                ctypes.windll.user32.SetWindowRgn(int(self.winId()), 0, True)
            except Exception:
                pass
            return
        rect = self._model_rect()
        if not rect:
            return
        mx, my, rw, rh = rect
        try:
            hwnd = int(self.winId())
            hRgn = ctypes.windll.gdi32.CreateRectRgn(mx, my, mx + rw, my + rh)
            ctypes.windll.user32.SetWindowRgn(hwnd, hRgn, True)
        except Exception:
            pass

    def _clear_hit_region(self):
        try:
            ctypes.windll.user32.SetWindowRgn(int(self.winId()), 0, True)
        except Exception:
            pass

    # ═══════════════════════════════════════════════
    # 动画循环
    # ═══════════════════════════════════════════════
    def _tick(self):
        dt = 16
        if not self._model_loaded:
            self.update()
            return

        # 空闲动作切换
        self._idle_timer += dt
        if self._idle_timer >= self._idle_interval:
            self._idle_timer = 0
            self._idle_interval = random.randint(5000, 15000)
            self._start_random_idle()

        # 漂浮
        if not self._border_shown and not self._is_companion and not self._drag_active:
            if not self._base_y_ready:
                self._base_y = self.y()
                self._base_y_ready = True
            self._float_t += dt
            off = int(math.sin(self._float_t / 3000 * 2 * math.pi) * 3)
            if off != self._last_float_off:
                self._last_float_off = off
                self.move(self.x(), self._base_y + off)

        self.update()

    def _get_available_groups(self):
        """返回可用的动作组列表"""
        if not self._model_loaded:
            return []
        try:
            return list(self._model.modelSetting.getMotionNames())
        except Exception:
            return []

    def _start_random_idle(self):
        """从 idle 相关动作组中随机选一个播放"""
        if not self._model_loaded:
            return
        groups = self._get_available_groups()
        # 优先选择 idle 组，回退到有空组的
        for g in ["idle", "sleepy", ""]:
            if g in groups:
                try:
                    self._model.StartRandomMotion(g, MotionPriority.IDLE)
                    return
                except Exception:
                    continue

    def _try_start_motion(self, group, priority=MotionPriority.FORCE):
        """尝试播放指定组动作，失败返回 False"""
        if not self._model_loaded:
            return False
        groups = self._get_available_groups()
        if group in groups:
            try:
                self._model.StartRandomMotion(group, priority)
                return True
            except Exception:
                pass
        return False

    def _in_model(self, pt):
        """检查点是否在模型渲染区域内，返回 rect 或 None"""
        r = self._model_rect()
        if not r:
            return None
        mx, my, rw, rh = r
        if mx <= pt.x() < mx + rw and my <= pt.y() < my + rh:
            return r
        return None

    # ═══════════════════════════════════════════════
    # 鼠标事件
    # ═══════════════════════════════════════════════
    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._companion_act.setText("结束陪伴" if self._is_companion else "开始陪伴")
            self._voice_act.setText("开启语音" if not self._voice_enabled else "关闭语音")
            self._border_act.setText("锁定位置" if self._border_shown else "显示边框")
            self._translate_act.setVisible(self._is_companion)
            self._talk_act.setVisible(self._is_companion)
            self._menu.popup(QCursor.pos())
            return
        if event.button() == Qt.LeftButton and not self._border_shown:
            self._press_pos = event.pos()
            self._press_moved = False
            self._drag_active = False
            self._drag_start = QCursor.pos()  # 全局坐标
            self._long_press_timer.start(600)

            # 只有点在模型范围内才触发交互动作
            inside = self._in_model(event.pos())
            if self._model_loaded and inside:
                rel_y = (event.y() - inside[1]) / inside[3]  # 模型内相对Y
                if rel_y < 0.4:
                    if not self._try_start_motion("flick_head"):
                        self._try_start_motion("idle", MotionPriority.FORCE)
                else:
                    if not self._try_start_motion("tap_body"):
                        self._try_start_motion("idle", MotionPriority.FORCE)

            event.accept()

    def mouseMoveEvent(self, event):
        if self._border_shown:
            return
        if event.buttons() & Qt.LeftButton:
            delta = QCursor.pos() - self._drag_start
            dist = delta.manhattanLength()
            # 移动超过 5px → 开始拖拽窗口
            if not self._drag_active and dist > 5:
                self._drag_active = True
                self._press_moved = True
                self._long_press_timer.stop()
            if self._drag_active:
                self.move(self.pos() + delta)
                self._drag_start = QCursor.pos()
                self._base_y = self.y()  # 拖拽后更新基准位置
                return

        # Live2D 物理拖拽交互 (模型内形变)
        if self._model_loaded and event.buttons() & Qt.LeftButton and not self._drag_active:
            try:
                self._model.Drag(float(event.x()), float(event.y()))
            except Exception:
                pass

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._long_press_timer.stop()
            self._drag_active = False

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        if self._is_companion or self._border_shown:
            return
        self._is_big = not self._is_big
        cx = self.x() + self.width() // 2
        cy = self.y() + self.height() // 2
        self.hide()
        if self._is_big:
            self.setFixedSize(BIG_W, BIG_H)
        else:
            self.setFixedSize(PET_W, PET_H)
        if self._model_loaded:
            self._model.Resize(self.width(), self.height())
            canvas_w, canvas_h = self._model.GetCanvasSizePixel()
            if canvas_w > 0 and canvas_h > 0:
                scale_w = self.width() / canvas_w
                scale_h = self.height() / canvas_h
                self._model_scale = min(scale_w, scale_h) * 0.85
                self._model.SetScale(self._model_scale)
        self.move(cx - self.width() // 2, cy - self.height() // 2)
        self._base_y = self.y()
        self._base_y_ready = True
        self.show()
        self.double_clicked.emit()
        self._float_t = 0
        self._last_float_off = 0
        self._update_hit_region()
        self.show()

    def wheelEvent(self, event):
        if self._border_shown or self._is_companion:
            return
        delta = event.angleDelta().y()
        factor = 1.15 if delta > 0 else 0.85
        new_w = max(MIN_W, min(MAX_W, int(self.width() * factor)))
        new_h = max(MIN_H, min(MAX_H, int(self.height() * factor)))
        cx = self.x() + self.width() // 2
        cy = self.y() + self.height() // 2
        self.setFixedSize(new_w, new_h)
        self.move(cx - new_w // 2, cy - new_h // 2)
        if self._model_loaded:
            self._model.Resize(new_w, new_h)
            canvas_w, canvas_h = self._model.GetCanvasSizePixel()
            if canvas_w > 0 and canvas_h > 0:
                scale_w = new_w / canvas_w
                scale_h = new_h / canvas_h
                self._model_scale = min(scale_w, scale_h) * 0.85
                self._model.SetScale(self._model_scale)
        self._update_hit_region()

    # ═══════════════════════════════════════════════
    # 边框拖拽
    # ═══════════════════════════════════════════════
    def _toggle_border(self):
        self._border_shown = not self._border_shown
        self.hide()
        if self._border_shown:
            self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
            self._border_act.setText("锁定位置")
        else:
            self.setWindowFlags(
                Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            )
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self._border_act.setText("显示边框")
            self._base_y = self.y()
            self._base_y_ready = True
            self._float_t = 0
            self._last_float_off = 0
        self._update_hit_region()
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
        self._base_y_ready = True
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

    # ── 气泡 ──
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

    def _quit(self):
        if self._is_companion:
            self._exit_companion()
        self._hide_bubble()
        self.close()
        sys.exit()
