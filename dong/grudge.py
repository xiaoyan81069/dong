"""
冬 · 记仇系统
- mark_grudge() 标记一条未解决的情感遗留
- resolve_grudge() 解决记仇
- get_active_grudges() 获取未解决的记仇列表
- get_grudge_ammo() 吵架时检索记仇弹药
"""
import json
import os
from datetime import datetime, timedelta

from .config import GRUDGE_FILE
from .log import log


def _load_grudges():
    """加载记仇文件"""
    if not os.path.exists(GRUDGE_FILE):
        return {}
    try:
        with open(GRUDGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_grudges(data):
    """保存记仇文件"""
    with open(GRUDGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def mark_grudge(uid, reason, context="", threat_level=0):
    """标记一条记仇。
    uid: 用户ID
    reason: 原因（如 "敷衍回复"、"骂我"、"不理我"）
    context: 当时对话片段
    threat_level: 杏仁核威胁等级(0-3)，影响有效期和严重性
    """
    uid_str = str(uid)
    grudges = _load_grudges()

    if uid_str not in grudges:
        grudges[uid_str] = []

    # 威胁等级影响过期天数
    if threat_level >= 3:
        expire_days = 30
    elif threat_level >= 2:
        expire_days = 21
    else:
        expire_days = 14

    # 自动过期
    now = datetime.now()
    cutoff = (now - timedelta(days=expire_days)).isoformat()
    grudges[uid_str] = [g for g in grudges[uid_str] if g.get("created", "") > cutoff]

    entry = {
        "uid": uid_str,
        "reason": reason,
        "context": context[:100],
        "created": now.isoformat(),
        "resolved": False,
        "threat_level": threat_level,
    }

    # L3记仇不占用普通配额
    max_active = 7 if threat_level >= 3 else 5
    grudges[uid_str].append(entry)
    grudges[uid_str] = grudges[uid_str][-max_active:]

    _save_grudges(grudges)
    log(f"  记仇标记: uid={uid} | {reason} (L{threat_level}, 共{len(grudges[uid_str])}条, {expire_days}天过期)")


def resolve_grudge(uid, reason_hint=""):
    """解决记仇（对方道歉了/哄好了）"""
    uid_str = str(uid)
    grudges = _load_grudges()
    if uid_str not in grudges:
        return

    # 找最近一条未解决的
    for g in reversed(grudges[uid_str]):
        if not g.get("resolved"):
            if not reason_hint or reason_hint in g.get("reason", ""):
                g["resolved"] = True
                g["resolved_at"] = datetime.now().isoformat()
                _save_grudges(grudges)
                log(f"  记仇已解决: uid={uid} | {g['reason']}")
                return

    # 如果指定了reason_hint但没匹配，解决最后一次
    unresolved = [g for g in grudges[uid_str] if not g.get("resolved")]
    if unresolved and reason_hint:
        unresolved[-1]["resolved"] = True
        unresolved[-1]["resolved_at"] = datetime.now().isoformat()
        _save_grudges(grudges)
        log(f"  记仇已解决(模糊): uid={uid}")


def get_active_grudges(uid):
    """获取该用户所有未解决的记仇"""
    uid_str = str(uid)
    grudges = _load_grudges()
    if uid_str not in grudges:
        return []
    return [g for g in grudges[uid_str] if not g.get("resolved")]


def get_grudge_ammo(uid, mood):
    """吵架时检索记仇弹药。
    只在情绪<40时返回，格式化为system prompt片段。
    返回空字符串或弹药文本。
    """
    if mood >= 40:
        return ""

    active = get_active_grudges(uid)
    if not active:
        return ""

    # 取最近2-3条
    ammo = active[-3:]
    lines = ["【旧账】你还没翻篇的事（情绪差时可以自然翻出来说）："]
    for i, g in enumerate(ammo):
        lines.append(f"- 上次{g['reason']}: {g['context']}")

    return "\n".join(lines)


def get_grudge_prompt(uid, mood):
    """生成记仇system prompt片段"""
    return get_grudge_ammo(uid, mood)
