"""工具函数"""
import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "window_state.json")


def read_json(conf_file):
    with open(conf_file, "r", encoding="UTF-8") as file:
        return json.load(file)


def load_window_state():
    """读取窗口位置记忆"""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_window_state(data):
    """保存窗口位置"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
