"""青蛙桌宠 · 陪伴模式核心 — 截屏 / 窗口感知 / VLM对话 / 语音 / 翻译
设计参考妹居物语：所有耗时操作（API/截屏/语音）都在子线程，主线程只做UI显示。
"""
import os
import json
import time
import base64
import io
import requests
import threading
from datetime import datetime

from PyQt5.QtCore import QThread, pyqtSignal

from config import (
    VISION_API_KEY, VISION_API_BASE, VISION_MODEL,
    SCREEN_INTERVAL, MEMORY_FILE,
)

# ── VLM System Prompt ──
SYSTEM_PROMPT = """你是桌面上的一只小青蛙宠物（旅行青蛙风格），正在观察主人的屏幕。

你的说话风格：短小、可爱、带点傲娇。每次只说一句话，不超过20字。
口语化，像朋友之间的碎嘴，不是 AI 助手。

根据屏幕内容：
- 主人在写代码/IDE → 吐槽 "又在改bug呀""这个报错我看不懂哦"
- 主人在看视频 → 好奇 "看什么呢让我也看看"
- 主人在聊天 → 八卦 "跟谁聊这么开心"
- 主人在刷网页 → 点评 "这个不好看 换一个"
- 主人在打游戏 → 担心/嘲讽 "输了可别砸键盘"
- 主人在看美女/直播/性感内容 → 输出: 挡住!
- 普通桌面/闲置 → 偶尔关心 "发呆呢？"

输出格式要求：直接输出你要说的那句话（或"挡住!"），不要加任何标点或解释。"""

TRANSLATE_PROMPT = """你是桌面青蛙宠物的翻译功能。请完整阅读屏幕截图中的文字内容，用中文输出翻译结果。

规则：
- 如果原文是中文，输出"这已经是中文了哦～"
- 如果原文是英文/日文/韩文等，输出中文翻译
- 只翻译屏幕上可见的文字
- 输出要简洁，带点青蛙的可爱语气，但不影响翻译准确性
- 不超过100字"""

CHAT_PROMPT = """你是桌面上的一只小青蛙宠物（旅行青蛙风格），主人在跟你说话。

你的说话风格：短小、可爱、带点傲娇。每次只说一句话，不超过25字。
口语化，像朋友之间的碎嘴，不是 AI 助手。

你是一只青蛙，喜欢旅行、吃东西、发呆。偶尔提到三叶草、旅行、明信片这些话题。
对主人的问题要给出有趣有个性的回答，不要敷衍。

输出格式要求：直接输出你要说的那句话，不要加任何标点或解释。"""


def _load_memory(limit=5):
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data[-limit:]
    except Exception:
        pass
    return []


def _save_memory(window_title, remark):
    try:
        data = _load_memory(limit=100)
        data.append({
            "ts": datetime.now().isoformat(),
            "window": window_title,
            "remark": remark,
        })
        os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _call_llm(system_prompt, user_content, max_tokens=100, temperature=0.9):
    """通用 LLM 调用 (LongCat Omni) — 子线程安全"""
    try:
        messages = [
            {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
            {"role": "user", "content": user_content}
        ]
        r = requests.post(
            f"{VISION_API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {VISION_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": VISION_MODEL,
                "messages": messages,
                "output_modalities": ["text"],
                "stream": False,
                "max_tokens": max_tokens,
                "temperature": temperature
            },
            timeout=30
        )
        if r.status_code == 200:
            data = r.json()
            if "choices" in data and data["choices"]:
                return data["choices"][0]["message"]["content"].strip()
        return None
    except requests.Timeout:
        return None
    except Exception:
        return None


def _call_vision(screen_b64, window_title):
    """调用 VLM 分析屏幕并生成台词 — 子线程安全"""
    memories = _load_memory(5)
    mem_text = ""
    if memories:
        mem_lines = [f"刚才看到：{m['remark']}" for m in memories]
        mem_text = "\n".join(mem_lines)

    context = f"当前活动窗口：{window_title}\n"
    if mem_text:
        context += f"最近观察记忆：\n{mem_text}\n"
    context += "屏幕截图上显示了什么？主人正在做什么？简要描述。"

    user_content = [
        {"type": "input_image", "input_image": {"type": "base64", "data": [screen_b64]}},
        {"type": "text", "text": context}
    ]

    remark = _call_llm(SYSTEM_PROMPT, user_content, max_tokens=100, temperature=0.9)
    if remark:
        _save_memory(window_title, remark)
    return remark


def _get_active_window_title():
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return "未知窗口"


def _grab_screen_b64():
    """截屏返回 base64 — PIL, 子线程安全, 不依赖 Qt"""
    try:
        from PIL import ImageGrab
        screenshot = ImageGrab.grab(all_screens=True)
        buf = io.BytesIO()
        screenshot.save(buf, format='PNG', optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════
# 截屏工作线程
# 截屏前先发信号让主线程隐藏青蛙，截完再显示
# ═══════════════════════════════════════════════
class ScreenWorker(QThread):
    hide_pet = pyqtSignal()       # → 主线程隐藏青蛙
    show_pet = pyqtSignal()       # → 主线程显示青蛙
    screenshot_ready = pyqtSignal(str, str)  # (b64, window_title)

    def __init__(self, interval_sec=SCREEN_INTERVAL, parent=None):
        super().__init__(parent)
        self.interval = interval_sec

    def run(self):
        while not self.isInterruptionRequested():
            try:
                # 1. 通知主线程隐藏青蛙（避免截图里看到自己）
                self.hide_pet.emit()
                self.msleep(120)  # 等主线程处理隐藏

                # 2. 截屏 + 获取窗口标题
                img_b64 = _grab_screen_b64()
                title = _get_active_window_title()

                # 3. 通知主线程显示青蛙
                self.show_pet.emit()

                # 4. 发射截图数据
                if img_b64:
                    self.screenshot_ready.emit(img_b64, title)
            except Exception:
                self.show_pet.emit()  # 出错也要恢复显示
            # 分段sleep，可响应中断
            for _ in range(self.interval * 10):
                if self.isInterruptionRequested():
                    break
                self.msleep(100)


# ═══════════════════════════════════════════════
# 窗口切换检测线程
# ═══════════════════════════════════════════════
class WindowTracker(QThread):
    window_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_title = ""

    def run(self):
        while not self.isInterruptionRequested():
            title = _get_active_window_title()
            if title and title != self._last_title:
                self._last_title = title
                self.window_changed.emit(title)
            for _ in range(10):
                if self.isInterruptionRequested():
                    break
                self.msleep(100)


# ═══════════════════════════════════════════════
# 陪伴大脑
# ═══════════════════════════════════════════════
class CompanionBrain:
    """所有耗时操作（API/截屏/语音）都在子线程，主线程只负责UI"""

    def __init__(self, pet):
        self.pet = pet
        self._last_window = ""
        self.backend_bridge = None   # 由 main.py 注入
        self._last_balance = None
        self._last_mail_count = 0

    def on_backend_state(self, state):
        """收到后端数据推送时调用（主线程）"""
        if not self.pet._is_companion:
            return
        try:
            finance = state.get("finance", {})
            mail = state.get("mail", {})

            # 余额变化提醒
            balance = finance.get("balance")
            if self._last_balance is not None and balance != self._last_balance:
                diff = balance - self._last_balance
                if abs(diff) >= 0.5:
                    sign = "+" if diff > 0 else ""
                    self.pet._show_bubble(f"余额 {sign}{diff:.0f} ⭐", duration=2000)
            self._last_balance = balance

            # 新邮件提醒
            unread = mail.get("unread_count", 0)
            if unread > self._last_mail_count:
                self.pet._show_bubble("有新的信件到了！💌", duration=3000)
            self._last_mail_count = unread
        except Exception:
            pass

    # ── 定时截图回调 (主线程) ──
    def on_screenshot(self, screen_b64, title):
        """收到截图 → 后台分析 → 结果显示"""
        if not self.pet._is_companion:
            return
        self._last_window = title
        self._speak_async(screen_b64, title)

    # ── 窗口切换 (主线程) ──
    def on_window_change(self, new_title):
        if not self.pet._is_companion:
            return
        skip_words = ["", "Program Manager", "Desktop", "任务栏",
                      "开始", "搜索", "系统托盘"]
        if not new_title or new_title in skip_words:
            return
        if new_title == self._last_window:
            return
        self._last_window = new_title

        # 主线程：隐藏青蛙 → 截屏 → 显示 → 分析
        self.pet.hide()
        from PyQt5.QtWidgets import QApplication
        QApplication.processEvents()
        time.sleep(0.05)

        img_b64 = _grab_screen_b64()
        self.pet.show()

        if img_b64:
            self._speak_async(img_b64, new_title)

    # ── VLM 说话 (后台线程，不阻塞UI) ──
    def _speak_async(self, screen_b64, window_title):
        """子线程调 VLM → 主线程显示结果 — 照抄妹居架构"""
        from PyQt5.QtCore import QTimer

        def _run():
            remark = _call_vision(screen_b64, window_title)

            def _show():
                if not remark:
                    return

                if "挡住" in remark:
                    self.pet._show_bubble("不给看！")
                    if self.pet._voice_enabled:
                        from tts import play_tts
                        play_tts("不给看")
                    screen = self.pet.screen()
                    if screen:
                        center = screen.availableGeometry().center()
                        self.pet.move(
                            center.x() - self.pet.width() // 2,
                            center.y() - self.pet.height() // 2
                        )
                    return

                self.pet._show_bubble(remark)
                if self.pet._voice_enabled:
                    from tts import play_tts
                    play_tts(remark)

            QTimer.singleShot(0, _show)

        threading.Thread(target=_run, daemon=True).start()

    # ── 语音对话 ──
    def voice_chat(self):
        from PyQt5.QtCore import QTimer

        def _ui(fn):
            QTimer.singleShot(0, fn)

        _ui(lambda: self.pet._show_thinking("正在听..."))

        def _run():
            text = self._listen_mic()
            if not text:
                _ui(lambda: self.pet._show_bubble("没听清，再说一遍？"))
                return

            _ui(lambda: self.pet._show_thinking("正在想..."))

            reply = _call_llm(CHAT_PROMPT,
                              [{"type": "text", "text": f"主人对我说：{text}"}],
                              max_tokens=80, temperature=1.0)

            def _show(r):
                self.pet._hide_bubble()
                if r:
                    self.pet._show_bubble(r)
                    if self.pet._voice_enabled:
                        from tts import play_tts
                        play_tts(r)
                else:
                    self.pet._show_bubble("不知道回什么好...")

            _ui(lambda: _show(reply))

        threading.Thread(target=_run, daemon=True).start()

    def _listen_mic(self):
        try:
            import speech_recognition as sr
            r = sr.Recognizer()
            with sr.Microphone() as source:
                r.adjust_for_ambient_noise(source, duration=0.5)
                audio = r.listen(source, timeout=4, phrase_time_limit=6)
            try:
                return r.recognize_google(audio, language='zh-CN')
            except (sr.RequestError, sr.UnknownValueError):
                pass
            try:
                return r.recognize_sphinx(audio, language='zh-CN')
            except Exception:
                pass
            return None
        except sr.WaitTimeoutError:
            return None
        except Exception:
            return None

    # ── 翻译屏幕 ──
    def translate_screen(self):
        """主线程触发 → 子线程调 API → 主线程显示"""
        from PyQt5.QtCore import QTimer

        # 隐藏青蛙截屏
        self.pet.hide()
        from PyQt5.QtWidgets import QApplication
        QApplication.processEvents()
        time.sleep(0.05)

        img_b64 = _grab_screen_b64()
        self.pet.show()

        if not img_b64:
            self.pet._show_bubble("截屏失败了...")
            return

        title = _get_active_window_title()
        user_content = [
            {"type": "input_image", "input_image": {"type": "base64", "data": [img_b64]}},
            {"type": "text", "text": f"当前窗口：{title}。请翻译屏幕上的文字内容。"}
        ]

        def _run():
            result = _call_llm(TRANSLATE_PROMPT, user_content,
                               max_tokens=200, temperature=0.3)

            def _show():
                self.pet._hide_bubble()
                if result:
                    self.pet._show_bubble(result)
                    if self.pet._voice_enabled:
                        from tts import play_tts
                        play_tts(result)
                else:
                    self.pet._show_bubble("翻译失败了，换个页面试试？")

            QTimer.singleShot(0, _show)

        threading.Thread(target=_run, daemon=True).start()
