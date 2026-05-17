"""
冬 · 零花钱系统 — 独立账户，余额仅冬可支配
数据文件: dong_finance.json（由 StateStore 管理）
"""
import os
import threading
from datetime import datetime
from .core.state_store import StateStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FINANCE_FILE = os.path.join(BASE_DIR, "dong_finance.json")
_lock = threading.Lock()

_store = StateStore(FINANCE_FILE)
_store.register("balance", 500.0)
_store.register("currency_name", "雪花币")
_store.register("transactions", [])


def load_finance():
    """兼容旧调用：返回完整状态 dict"""
    return {
        "balance": _store.get("balance", 500.0),
        "currency_name": _store.get("currency_name", "雪花币"),
        "transactions": _store.get("transactions", []),
    }


def save_finance(data):
    with _lock:
        for k in ("balance", "currency_name", "transactions"):
            if k in data:
                _store.set(k, data[k])
        _store.flush()


def get_balance():
    return _store.get("balance", 500.0)


def add_transaction(desc, amount, tx_type="expense"):
    """记录一笔交易。amount>0为收入，<0为支出"""
    with _lock:
        new_balance = _store.atomic_update("balance", lambda b: round(b + amount, 2))
        tx = {
            "desc": desc,
            "amount": round(amount, 2),
            "type": "income" if amount > 0 else tx_type if tx_type == "expense" else tx_type,
            "color": "blue" if amount > 0 else ("black" if amount >= -50 else "red"),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _store.atomic_update("transactions", lambda t: t + [tx])
        _store.flush()
    return new_balance


def get_recent_transactions(n=10):
    return _store.get("transactions", [])[-n:]


def get_finance_snapshot():
    return {
        "balance": _store.get("balance", 500.0),
        "currency_name": _store.get("currency_name", "雪花币"),
        "transactions": _store.get("transactions", [])[-20:],
    }
