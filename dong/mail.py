"""
冬 · 信件系统 — 跨次元桥梁，信件有2-5天延迟
数据文件: dong_mail.json（由 StateStore 管理）
"""
import os
import random
import threading
import time
from datetime import datetime
from .core.state_store import StateStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAIL_FILE = os.path.join(BASE_DIR, "dong_mail.json")
_lock = threading.Lock()

_store = StateStore(MAIL_FILE)
_store.register("letters", [])
_store.register("last_check_ts", time.time())


def load_mail():
    """兼容旧调用"""
    return {
        "letters": _store.get("letters", []),
        "last_check_ts": _store.get("last_check_ts", time.time()),
    }


def save_mail(data):
    with _lock:
        for k in ("letters", "last_check_ts"):
            if k in data:
                _store.set(k, data[k])
        _store.flush()


def send_letter(from_name, content, from_uid=None):
    """投递一封信给冬。信件进入'时空隧道'，2-5天后冬才会读到"""
    with _lock:
        delivery_delay_days = random.randint(2, 5)
        now = datetime.now()
        letter = {
            "id": f"L{int(time.time() * 1000)}",
            "from_name": from_name,
            "from_uid": str(from_uid) if from_uid else None,
            "content": content,
            "sent_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "deliver_at": (now.timestamp() + delivery_delay_days * 86400),
            "deliver_at_str": datetime.fromtimestamp(
                now.timestamp() + delivery_delay_days * 86400
            ).strftime("%Y-%m-%d %H:%M"),
            "read": False,
            "replied": False,
            "sent_status": "in_transit",
            "delay_days": delivery_delay_days,
        }
        _store.atomic_update("letters", lambda ls: ls + [letter])
        _store.set("last_check_ts", time.time())
        _store.flush()
        return {
            "success": True,
            "letter_id": letter["id"],
            "deliver_at": letter["deliver_at_str"],
            "message": f"信件已投递！预计{delivery_delay_days}天后冬会读到",
        }


def check_new_mail():
    """检查是否有已到达但未读的信件"""
    letters = _store.get("letters", [])
    now_ts = time.time()
    unread = [l for l in letters if not l.get("read") and l.get("deliver_at", 0) <= now_ts]
    return len(unread) > 0, unread


def mark_read(letter_id):
    with _lock:
        letters = _store.get("letters", [])
        for l in letters:
            if l["id"] == letter_id:
                l["read"] = True
                l["sent_status"] = "delivered"
                break
        _store.set("letters", letters)
        _store.flush()


def get_mail_snapshot():
    """获取信件系统快照"""
    letters = _store.get("letters", [])
    now_ts = time.time()
    in_transit = sum(1 for l in letters if not l.get("read") and l.get("deliver_at", 0) > now_ts)
    delivered_unread = sum(1 for l in letters if not l.get("read") and l.get("deliver_at", 0) <= now_ts)
    has_new = delivered_unread > 0
    return {
        "has_new_mail": has_new,
        "unread_count": delivered_unread,
        "in_transit_count": in_transit,
        "total_letters": len(letters),
        "sent_status": "new_mail" if has_new else "idle",
        "recent_letters": letters[-5:],
    }
