"""
桥接对象 — QWebChannel 双向通信核心
暴露给 JavaScript 的 Python 槽方法，管理后端连接和状态同步。
Phase 10: + 音效反馈
"""
import json
import os
import requests
from PyQt5.QtCore import QObject, pyqtSlot, pyqtSignal, QTimer
from PyQt5.QtMultimedia import QSound

SOUNDS_DIR = os.path.join(os.path.dirname(__file__), "sounds")


class PetBridge(QObject):
    """QWebChannel 桥接对象，注册为 'bridge' 供 JS 调用"""

    # 每秒推送最新状态
    state_updated = pyqtSignal(str)
    # 连接状态变化
    connection_changed = pyqtSignal(bool)

    def __init__(self, backend_url="http://127.0.0.1:5120", parent=None):
        super().__init__(parent)
        self.backend_url = backend_url
        self._state_cache = {}
        self._connected = False

        # 轮询定时器
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_backend)

    def start_polling(self, interval_ms=1000):
        self._poll_timer.start(interval_ms)

    def stop_polling(self):
        self._poll_timer.stop()

    def check_backend(self):
        """健康检查"""
        try:
            r = requests.get(f"{self.backend_url}/api/ping", timeout=1.5)
            return r.status_code == 200
        except Exception:
            return False

    def _poll_backend(self):
        """轮询后端全量状态，有变化时发射 state_updated"""
        try:
            r = requests.get(f"{self.backend_url}/api/full", timeout=0.8)
            if r.status_code == 200:
                new_state = r.json()

                # 只比较数据部分（排除 timestamp）
                data_hash = json.dumps(
                    {k: v for k, v in new_state.items() if k != "timestamp"},
                    sort_keys=True, ensure_ascii=False
                )

                if data_hash != self._state_cache.get("_hash"):
                    self._state_cache = new_state
                    self._state_cache["_hash"] = data_hash
                    self.state_updated.emit(json.dumps(new_state, ensure_ascii=False))

                if not self._connected:
                    self._connected = True
                    self.connection_changed.emit(True)

        except Exception:
            if self._connected:
                self._connected = False
                self.connection_changed.emit(False)

    # ===== 槽方法：JS 调用 → HTTP POST 到后端 =====

    def _play_sound(self, filename):
        filepath = os.path.join(SOUNDS_DIR, filename)
        if os.path.exists(filepath):
            QSound.play(filepath)

    @pyqtSlot(str, result=str)
    def buy_item(self, item_id):
        result = self._post("/api/game/buy", {"item_id": item_id})
        try:
            if json.loads(result).get("success"):
                self._play_sound("buy.wav")
        except Exception:
            pass
        return result

    @pyqtSlot(str, result=str)
    def equip_item(self, item_id):
        result = self._post("/api/game/equip", {"item_id": item_id})
        try:
            if json.loads(result).get("success"):
                self._play_sound("equip.wav")
        except Exception:
            pass
        return result

    @pyqtSlot(str, str, result=str)
    def send_letter(self, to_name, content):
        result = self._post("/api/mail/send", {"to_name": to_name, "content": content})
        try:
            if json.loads(result).get("success"):
                self._play_sound("mail.wav")
        except Exception:
            pass
        return result

    @pyqtSlot(int, result=str)
    def recharge(self, amount):
        return self._post("/api/finance/recharge", {"amount": amount})

    @pyqtSlot(str, result=str)
    def trigger_item_detail(self, item_id):
        return self._post("/api/game/item_detail", {"item_id": item_id})

    @pyqtSlot(result=str)
    def get_cached_state(self):
        """立即返回缓存状态（无网络请求）"""
        return json.dumps(self._state_cache, ensure_ascii=False)

    def _post(self, endpoint, data):
        """同步 POST 到后端"""
        try:
            r = requests.post(
                f"{self.backend_url}{endpoint}",
                json=data,
                timeout=2
            )
            if r.status_code == 200:
                # 强制立即轮询以反映变化
                self._poll_backend()
                return json.dumps(r.json(), ensure_ascii=False)
            return json.dumps({"error": True, "message": r.text})
        except Exception as e:
            return json.dumps({"error": True, "message": str(e)})
