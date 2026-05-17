"""
冬 · 亲密关系系统 (#31)
- 五级亲密度：陌生人(0)→认识(1)→普通朋友(2)→好朋友(3)→至交(4)
- 亲密度动态变化（正向/负向事件）
- 硬限制边界（专属行为仅159可用）
- 亲密度影响回复风格和话题范围
"""
import json
import os
import random
import threading
from datetime import datetime, timedelta

from .config import INTIMACY_FILE, INTIMACY_LEVELS, INTIMACY_HARD_BOUNDARY, MASTER_UID, ALLOWED_USERS
from .log import log
from .core.data_healing import FieldSpec, heal_any

# ============ 亲密度数据 ============
_intimacy = {}  # {uid: {"level": 0, "score": 0, "last_update": ..., "history": [...]}}
_intimacy_lock = threading.Lock()


def _init_user_locked(uid):
    """调用方须持有 _intimacy_lock"""
    if uid not in _intimacy:
        init_level = 4 if uid == MASTER_UID else 1
        init_score = _level_threshold(init_level)
        _intimacy[uid] = {
            "level": init_level,
            "score": init_score,
            "last_update": datetime.now().isoformat(),
            "history": [],
        }


def _level_threshold(level):
    """每级需要的分数"""
    thresholds = {0: 0, 1: 50, 2: 150, 3: 300, 4: 600}
    return thresholds.get(level, 0)


def _calc_level(score):
    """根据分数计算等级"""
    if score >= 600:
        return 4
    elif score >= 300:
        return 3
    elif score >= 150:
        return 2
    elif score >= 50:
        return 1
    return 0


def load_intimacy():
    global _intimacy
    with _intimacy_lock:
        try:
            if os.path.exists(INTIMACY_FILE):
                with open(INTIMACY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    _intimacy = {str(k): v for k, v in data.items()}
                _USER_SCHEMA = {
                    "level": FieldSpec(int, 1, range=(0, 4)),
                    "score": FieldSpec(int, 50, range=(0, 9999)),
                    "last_update": FieldSpec(str, "", nullable=True),
                    "history": FieldSpec(list, []),
                }
                total_repairs = 0
                for uid_key in list(_intimacy.keys()):
                    healed, repairs = heal_any(_intimacy[uid_key], _USER_SCHEMA)
                    _intimacy[uid_key] = healed
                    total_repairs += len(repairs)
                master_key = str(MASTER_UID)
                if master_key in _intimacy:
                    if _intimacy[master_key].get("level", 0) < 4:
                        _intimacy[master_key]["level"] = 4
                        _intimacy[master_key]["score"] = max(_intimacy[master_key].get("score", 0), 600)
                        total_repairs += 1
                        log("  亲密度自愈: 主号强制 Level 4")
                if total_repairs:
                    log(f"亲密度自愈: {total_repairs} 处修复")
                    _save_intimacy_locked()
                else:
                    log(f"亲密度已加载: {len(_intimacy)} 个用户")
        except Exception as e:
            log(f"亲密度加载失败: {e}")
            _intimacy = {}

        for uid in ALLOWED_USERS:
            _init_user_locked(str(uid))


def _save_intimacy_locked():
    """调用方须持有 _intimacy_lock"""
    try:
        with open(INTIMACY_FILE, "w", encoding="utf-8") as f:
            json.dump(_intimacy, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"亲密度保存失败: {e}")

def save_intimacy():
    """公开保存接口"""
    with _intimacy_lock:
        _save_intimacy_locked()

def modify_intimacy(uid, delta, reason=""):
    """修改亲密度分数，返回(新等级, 是否升级/降级)"""
    uid_str = str(uid)
    with _intimacy_lock:
        _init_user_locked(uid_str)

        old_level = _intimacy[uid_str]["level"]
        if delta < 0:
            delta = int(delta * 1.3)

        _apply_decay(uid_str)

        _intimacy[uid_str]["score"] = max(0, _intimacy[uid_str]["score"] + delta)
        new_level = _calc_level(_intimacy[uid_str]["score"])

        _intimacy[uid_str]["level"] = new_level
        _intimacy[uid_str]["last_update"] = datetime.now().isoformat()
        _intimacy[uid_str]["history"].append({
            "delta": delta, "reason": reason, "time": datetime.now().isoformat(),
        })
        _intimacy[uid_str]["history"] = _intimacy[uid_str]["history"][-50:]

        changed = old_level != new_level
        if changed:
            direction = "↑" if new_level > old_level else "↓"
            log(f"亲密度: uid={uid} {INTIMACY_LEVELS[old_level].name}→{INTIMACY_LEVELS[new_level].name} {reason}")

        _save_intimacy_locked()
        return new_level, changed


def _apply_decay(uid_str):
    """超过7天无互动，每天-1分"""
    last = _intimacy[uid_str].get("last_update")
    if not last:
        return
    try:
        last_dt = datetime.fromisoformat(last)
        days = (datetime.now() - last_dt).days
        if days > 7:
            decay = (days - 7) * 1
            _intimacy[uid_str]["score"] = max(0, _intimacy[uid_str]["score"] - decay)
            new_level = _calc_level(_intimacy[uid_str]["score"])
            if new_level != _intimacy[uid_str]["level"]:
                log(f"亲密度衰减: uid={uid_str} lv{_intimacy[uid_str]['level']}→lv{new_level}")
                _intimacy[uid_str]["level"] = new_level
    except Exception:
        pass


def get_intimacy_level(uid):
    """获取亲密度等级(0-4)"""
    uid_str = str(uid)
    with _intimacy_lock:
        _init_user_locked(uid_str)
        return _intimacy[uid_str]["level"]


def get_intimacy_score(uid):
    """获取亲密度分数"""
    uid_str = str(uid)
    with _intimacy_lock:
        _init_user_locked(uid_str)
        return _intimacy[uid_str]["score"]


def check_hard_boundary(uid, behavior):
    """检查行为是否超出硬限制边界。True=允许，False=禁止"""
    if uid == MASTER_UID:
        return True
    for b in INTIMACY_HARD_BOUNDARY:
        if b.name == behavior:
            return False
    return True


def get_intimacy_prompt(uid):
    """根据亲密度等级生成行为引导prompt"""
    if uid == MASTER_UID:
        return ""

    level = get_intimacy_level(uid)
    level_name = INTIMACY_LEVELS[level].name

    prompts = {
        0: f"【亲密度：{level_name}】对方是陌生人。回复礼貌简短，保持距离感。不要主动聊私事。",
        1: f"【亲密度：{level_name}】你们见过面/聊过几次。可以友好但不要过于亲近。",
        2: f"【亲密度：{level_name}】可以聊日常，偶尔关心。但不要太亲密。",
        3: f"【亲密度：{level_name}】关系不错，可以分享日常琐事、吐槽、稍微走心。但不能用对159的专属称呼和撒娇。",
        4: f"【亲密度：{level_name}】你信任对方，可以倾诉。但仍然不能使用对159的专属称呼和表达。",
    }
    return prompts.get(level, prompts[0])


def auto_intimacy_change(uid, text):
    """根据消息内容自动调整亲密度"""
    score_delta = 0

    positive = ["想你", "谢谢", "辛苦了", "抱抱", "真好", "爱你", "喜欢你", "你最好了"]
    if any(w in text for w in positive):
        score_delta += 2

    deep_topics = ["难过", "分手", "家里", "过去", "前任", "秘密", "跟你说个事"]
    if any(w in text for w in deep_topics):
        score_delta += 3

    negative_phrases = ["不想理你", "你烦不烦", "别说了", "滚"]
    if "烦" in text and len(text.strip()) <= 3 and not any(kw in text for kw in ["麻烦", "烦恼"]):
        score_delta -= 5
    elif any(w in text for w in negative_phrases):
        score_delta -= 5

    if score_delta != 0:
        modify_intimacy(uid, score_delta, f"消息触发({score_delta:+d})")
