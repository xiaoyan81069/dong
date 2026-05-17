"""
冬 · 更新日志模块
- 每次代码更新记录版本号、时间、改动内容
- 启动时自动记录，测试时回溯对话对应的版本
- JSONL 格式，逐行追加，高效读取
"""
import json
import os
from datetime import datetime
from typing import List, Dict, Optional

from .config import BASE_DIR
from .log import log as _log

UPDATE_LOG_FILE = os.path.join(BASE_DIR, "dong_update_log.jsonl")


def _read_all() -> List[Dict]:
    """读取所有更新记录"""
    entries = []
    if os.path.exists(UPDATE_LOG_FILE):
        with open(UPDATE_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return entries


def get_latest_version() -> int:
    """获取当前版本号"""
    entries = _read_all()
    return entries[-1]["version"] if entries else 0


def log_update(description: str, files_changed: Optional[List[str]] = None,
               update_type: str = "update"):
    """
    记录一次更新。

    Args:
        description: 更新内容描述
        files_changed: 改动的文件列表
        update_type: 类型 — "update"(功能更新) / "hotfix"(修复) / "startup"(启动) / "note"(备注)

    Returns:
        int: 新版本号
    """
    version = get_latest_version() + 1
    files_changed = files_changed or []

    entry = {
        "version": version,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": update_type,
        "description": description,
        "files_changed": files_changed,
    }

    with open(UPDATE_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    _log(f"更新日志 v{version}: {description}")
    return version


def get_recent_updates(n: int = 10) -> List[Dict]:
    """获取最近 N 条更新记录"""
    entries = _read_all()
    return entries[-n:]


def get_update_info() -> Dict:
    """获取仪表盘需要的更新信息"""
    entries = _read_all()
    if not entries:
        return {"version": 0, "last_update": "--", "last_desc": "", "total_updates": 0, "recent": []}

    latest = entries[-1]
    recent = []
    for e in reversed(entries[-5:]):
        recent.append({
            "version": e["version"],
            "time": e["time"],
            "description": e["description"][:50],
            "type": e["type"],
        })

    return {
        "version": latest["version"],
        "last_update": latest["time"],
        "last_desc": latest["description"],
        "total_updates": len(entries),
        "recent": recent,
    }


# ---- 命令行入口 ----
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        desc = " ".join(sys.argv[1:])
        log_update(desc)
        print(f"已记录更新 v{get_latest_version()}: {desc}")
    else:
        updates = get_recent_updates(5)
        for u in updates:
            print(f"  v{u['version']} [{u['time']}] {u['type']}: {u['description']}")
        print(f"\n总更新次数: {len(_read_all())}")
