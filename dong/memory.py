"""
冬 · 记忆系统
- 长期记忆（摘要+关键词 → 检索式注入 → 替代全量注入）
- 短期视觉记忆（图片识别后保留3-5轮）
- 会话历史（最近N轮对话）
- 聊天记录持久化
- 记忆缓存（减少文件IO）
- 自动摘要（分析模型异步生成）
- 记忆合并（防止条目膨胀）
- 软过期（降权替代硬删除）
"""
import asyncio
import json
import os
import random
import threading
import time
from datetime import datetime, timedelta

from .config import MEMORY_FILE, CHAT_HISTORY_FILE, MEDIA_DIR, BASE_DIR, \
    MEMORY_RETRIEVAL_COUNT, MEMORY_MAX_PER_USER, MEMORY_SOFT_DELETE_DAYS
from .log import log
from .core.data_healing import heal_any, FieldSpec
from .persona import strip_emoji, strip_nicknames, get_persona


# ============ 记忆缓存（减少文件IO）============
_MEMORY_CACHE = None
_MEMORY_CACHE_TTL = 30  # 缓存有效期（秒）
_MEMORY_CACHE_TIME = None


def _get_memory_cache():
    """获取带缓存的记忆数据"""
    global _MEMORY_CACHE, _MEMORY_CACHE_TIME
    now = datetime.now()
    if _MEMORY_CACHE is None or _MEMORY_CACHE_TIME is None:
        _MEMORY_CACHE = load_memory()
        _MEMORY_CACHE_TIME = now
        return _MEMORY_CACHE
    if (now - _MEMORY_CACHE_TIME).total_seconds() > _MEMORY_CACHE_TTL:
        _MEMORY_CACHE = load_memory()
        _MEMORY_CACHE_TIME = now
    return _MEMORY_CACHE


def _invalidate_memory_cache():
    """使记忆缓存失效"""
    global _MEMORY_CACHE, _MEMORY_CACHE_TIME
    _MEMORY_CACHE = None
    _MEMORY_CACHE_TIME = None


# ============ 长期记忆 ============
def load_memory():
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log(f"记忆加载失败: {e}")
    return {}


def save_memory(memory):
    try:
        tmp = MEMORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)
        os.replace(tmp, MEMORY_FILE)
    except Exception as e:
        log(f"记忆保存失败: {e}")


def should_remember(text):
    important_keywords = [
        "生病", "感冒", "发烧", "不舒服", "考试", "比赛",
        "重要", "难过", "心情不好", "吵架", "生气",
        "过敏", "医院", "吃药", "手术", "生日",
        "约会", "约定", "承诺", "想你", "喜欢你",
    ]
    return any(kw in text for kw in important_keywords)


def add_memory(uid, content, is_important=False, amygdala_salient=False):
    """添加记忆——新结构：id/summary/keywords/importance(float)/clarity/access_count/hormone_snapshot
    若 amygdala_salient=True → 杏仁核直接标记为高重要性，跳过激素计算
    """
    memory = _get_memory_cache()

    uid_key = str(uid)
    if uid_key not in memory:
        memory[uid_key] = {"memories": []}

    # 先做软过期降权
    _apply_soft_expiry(memory[uid_key]["memories"])

    today = datetime.now().strftime("%m-%d")
    now_iso = datetime.now().isoformat()
    mem_id = f"mem_{uid}_{int(time.time() * 1000)}"

    # 初始关键词：从 should_remember 关键词表匹配
    init_keywords = _extract_basic_keywords(content)

    # 抓取当前激素快照
    try:
        from .status import get_hormone_snapshot
        snap = get_hormone_snapshot()
    except Exception:
        snap = {}

    # 抓取情境元数据
    context_metadata = {}
    try:
        from .config import describe_time_of_day, is_weekend as _is_weekend, MASTER_UID as _mu
        from .status import _status_manager
        context_metadata["time_of_day"] = describe_time_of_day()
        context_metadata["fatigue"] = _status_manager._status.fatigue
        context_metadata["mood"] = _status_manager._status.mood
        context_metadata["is_weekend"] = _is_weekend()
        context_metadata["is_late_night"] = datetime.now().hour >= 23 or datetime.now().hour < 5
        context_metadata["is_master"] = uid == _mu
        # 亲密度
        try:
            from .intimacy import get_intimacy_level
            context_metadata["intimacy_level"] = get_intimacy_level(uid)
        except Exception:
            context_metadata["intimacy_level"] = 0
    except Exception:
        pass

    # importance 计算
    if amygdala_salient:
        # 杏仁核直接标记：高重要性
        importance_val = 0.85
    elif snap and all(k in snap for k in ["dopamine", "adrenaline", "cortisol", "oxytocin", "serotonin"]):
        baselines = {"dopamine": 50, "adrenaline": 30, "cortisol": 20, "oxytocin": 50, "serotonin": 60}
        deviation = sum(abs(snap[k] - baselines[k]) for k in baselines) / 200
        importance_val = max(0.05, min(1.0, 0.3 + deviation))
    else:
        importance_val = 0.8 if is_important else 0.4

    memory[uid_key]["memories"].append({
        "id": mem_id,
        "summary": content[:30],  # 初始摘要=content前30字，后续auto_summarize更新
        "keywords": init_keywords,
        "content": content[:200],  # 保留原文（增大到200字）
        "date": today,
        "importance": importance_val,
        "expires": (datetime.now() + timedelta(days=7 if importance_val >= 0.7 else 1)).strftime("%m-%d"),
        "clarity": 100,
        "emotional_intensity": min(1.0, importance_val * 0.9),  # 初始情感强度 ≈ 重要度 × 0.9
        "distortion_count": 0,
        "original": content[:200],
        "access_count": 0,
        "updated": now_iso,
        "hormone_snapshot": snap,  # 存储时的激素快照
        "context_metadata": context_metadata,  # 情境元数据（时间/疲劳/心情/亲密度等）
    })

    save_memory(memory)
    _invalidate_memory_cache()


def get_memory_text(uid):
    """保留旧接口：返回所有记忆文本（不再全量注入，内部兼容用）"""
    return retrieve_relevant_memories(uid, "", top_k=999)


def _get_importance(m):
    """兼容获取 importance 浮点值（旧 str / 新 float）"""
    imp = m.get("importance", 0.4)
    if isinstance(imp, str):
        return 0.8 if imp == "important" else 0.4
    return max(0.0, min(1.0, float(imp)))


def _extract_basic_keywords(text):
    """从文本中提取基本关键词（作为初始关键词，后续由分析模型覆盖）"""
    # 简单方法：用关键词匹配 + 2-4字分词
    kw_patterns = ["生病", "感冒", "发烧", "不舒服", "考试", "比赛",
                   "重要", "难过", "心情不好", "吵架", "生气",
                   "过敏", "医院", "吃药", "手术", "生日",
                   "约会", "约定", "承诺", "想你", "喜欢你",
                   "胃疼", "头疼", "累", "困", "忙", "烦",
                   "吃饭", "睡觉", "上课", "练琴", "排练"]
    found = [kw for kw in kw_patterns if kw in text]
    if not found:
        # 简单2字分词兜底
        for i in range(len(text) - 1):
            chunk = text[i:i+2]
            if any('\u4e00' <= c <= '\u9fff' for c in chunk):
                found.append(chunk)
        found = list(set(found))[:5]
    return found[:5]


def _apply_soft_expiry(memories):
    """软过期：过期记忆降权而非删除。仅 importance<0.05 且超 MEMORY_SOFT_DELETE_DAYS 天真删"""
    now = datetime.now()
    now_str = now.strftime("%m-%d")
    for m in memories[:]:
        exp = m.get("expires", "")
        if not exp:
            continue
        try:
            exp_dt = datetime.strptime(exp, "%m-%d").replace(year=now.year)
            if exp_dt > now:
                exp_dt = exp_dt.replace(year=now.year - 1)
        except Exception:
            continue

        if exp_dt.strftime("%m-%d") < now_str:
            # 已过期 → 降权
            old_imp = _get_importance(m)
            m["importance"] = max(0.01, old_imp * 0.3)
            m["updated"] = now.isoformat()
            # 如果 importance < 0.05 且距今超过 MEMORY_SOFT_DELETE_DAYS → 真删
            if old_imp < 0.05 and (now - exp_dt).days > MEMORY_SOFT_DELETE_DAYS:
                memories.remove(m)
                log(f"  记忆真删除(clar<5%): {m.get('summary', m.get('content', ''))[:20]}")


def retrieve_relevant_memories(uid, current_msg, top_k=None):
    """检索与当前消息最相关的记忆（关键词命中计数 × importance × clarity/100）
    替代全量注入，每次只返回 top_k 条最相关的记忆。
    """
    if top_k is None:
        top_k = MEMORY_RETRIEVAL_COUNT

    memory = _get_memory_cache()
    uid_key = str(uid)
    if uid_key not in memory or not memory[uid_key].get("memories"):
        return ""

    # 软过期降权
    _apply_soft_expiry(memory[uid_key]["memories"])

    memories = memory[uid_key]["memories"]
    if not memories:
        return ""

    # 应用雾化
    for m in memories:
        apply_memory_fog(m)

    # 应用再加工
    try:
        from .status import _status
        mood = _status.get("mood", 50)
    except Exception:
        mood = 50
    for m in memories:
        apply_memory_distortion(m, mood)

    # 过滤重要性过低
    valid = [m for m in memories if _get_importance(m) >= 0.05]
    if not valid:
        return ""

    # 获取当前激素快照用于状态一致性打分
    current_snap = None
    try:
        from .status import get_hormone_snapshot
        current_snap = get_hormone_snapshot()
    except Exception:
        pass

    def _hormone_state_bonus(mem, cur_snap) -> float:
        """状态一致性加分：记忆存储时的激素状态和当前相似 → 更容易被记起"""
        if not cur_snap:
            return 0
        snap = mem.get("hormone_snapshot", {})
        if not snap or "dominant" not in snap:
            return 0
        # 主导情绪匹配 → +25%
        if snap.get("dominant") == cur_snap.get("dominant"):
            return 0.25
        # 计算数值相似度
        keys = ["dopamine", "adrenaline", "cortisol", "oxytocin", "serotonin"]
        if not all(k in snap for k in keys) or not all(k in cur_snap for k in keys):
            return 0
        diff = sum(abs(snap[k] - cur_snap[k]) for k in keys) / 500
        similarity = 1 - diff
        return similarity * 0.3  # 最多+30%

    # 无消息 → 返回全部（兼容 get_memory_text）
    if not current_msg or not current_msg.strip():
        scored = [(m, _get_importance(m) * max(0.01, m.get("clarity", 100) / 100)) for m in valid]
    else:
        # 关键词倒排打分 + 状态一致性加分
        msg_lower = current_msg.lower()
        scored = []
        for m in valid:
            keywords = m.get("keywords", [])
            summary = m.get("summary", m.get("content", ""))
            content = m.get("content", "")

            if keywords:
                # 关键词命中数：当前消息包含记忆关键词即命中
                hits = sum(1 for kw in keywords if kw in msg_lower or kw in current_msg)
            else:
                # 兼容旧格式：简单分词匹配
                hits = _simple_match_score(content, current_msg)

            imp = _get_importance(m)
            clarity = max(0.01, m.get("clarity", 100) / 100)
            state_bonus = _hormone_state_bonus(m, current_snap)
            score = hits * imp * clarity * (1 + state_bonus)
            scored.append((m, score))

    # 按分数降序排序，取 top_k
    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:top_k]

    # 情绪回响：被提取的记忆轻微影响当前激素
    if top and current_snap:
        _apply_hormone_echo(top, uid)

    # 更新 access_count + 情感强度衰减（每次提取回忆，感动就淡一点）
    need_save = False
    for m, _ in top:
        old_count = m.get("access_count", 0)
        m["access_count"] = old_count + 1
        ei = m.get("emotional_intensity", 0.5)
        new_ei = round(max(0.0, ei * 0.95), 2)
        if m["emotional_intensity"] != new_ei or m["access_count"] != old_count:
            m["emotional_intensity"] = new_ei
            need_save = True

    # 格式化输出
    memory_lines = ["[你的记忆]"]
    for m, score in top:
        if score < 0.05:
            continue
        tag = "【重要】" if _get_importance(m) >= 0.7 else ""
        display = m.get("summary", m.get("content", ""))
        memory_lines.append(f"- {m['date']} {tag}{display}")

    if need_save:
        save_memory(memory)  # 只在有变更时保存
    return "\n".join(memory_lines) if len(memory_lines) > 1 else ""


def _simple_match_score(content, msg):
    """简单匹配打分：2字及以上子串命中计数"""
    if not content or not msg:
        return 0
    hits = 0
    for i in range(len(msg) - 1):
        chunk = msg[i:i+2]
        if chunk in content:
            hits += 1
    return hits


def _apply_hormone_echo(top_memories, uid):
    """情绪回响：被提取的记忆的激素快照轻微拉回当前激素（±3~5点）"""
    try:
        from .status import update_hormones, _status_manager
        # 取权重最高的记忆的激素快照
        top_memory = top_memories[0][0]
        snap = top_memory.get("hormone_snapshot", {})
        if not snap:
            return

        # 主导情绪匹配 → 强化当前激素
        current = _status_manager.hormones._h
        dom = snap.get("dominant", "")
        if dom in ("亲密温暖", "开心黏人"):
            current.oxytocin = min(100, current.oxytocin + 3)
            current.dopamine = min(100, current.dopamine + 2)
        elif dom in ("焦虑烦躁", "烦躁低落"):
            current.cortisol = min(100, current.cortisol + 3)
        elif dom == "紧张焦虑":
            current.adrenaline = min(100, current.adrenaline + 3)
        elif dom == "兴奋不安":
            current.dopamine = min(100, current.dopamine + 2)
            current.cortisol = min(100, current.cortisol + 2)

        # 同步mood
        from .status import _status_manager as sm
        sm._status.mood = sm.hormones.derive_mood()
    except Exception:
        pass  # 情绪回响失败不影响记忆检索


# ============ 短期视觉记忆 ============
_visual_memory = {}  # {uid: {"caption": str, "turns": int, "timestamp": str}}


def set_visual_memory(uid, caption):
    _visual_memory[uid] = {
        "caption": caption,
        "turns": 5,
        "timestamp": datetime.now().strftime("%H:%M:%S")
    }
    log(f"  视觉记忆设定: {caption[:30]} (5轮)")


def tick_visual_memory(uid):
    if uid in _visual_memory:
        _visual_memory[uid]["turns"] -= 1
        if _visual_memory[uid]["turns"] <= 0:
            del _visual_memory[uid]
            log(f"  视觉记忆过期")


def get_visual_memory_prompt(uid):
    if uid not in _visual_memory:
        return ""
    v = _visual_memory[uid]
    return f"【短期视觉记忆-第{v['turns']}轮】对方刚才发了一张图片：{v['caption']}。你可以围绕这个图片内容进行对话，直到忘记。"


# ============ 会话历史 ============
MAX_HISTORY = 20
conversation_history = {}
last_active_time = {}


def get_history(uid):
    return conversation_history.get(uid, [])


def add_history(uid, q, a):
    if uid not in conversation_history:
        conversation_history[uid] = []
    conversation_history[uid].append((q, a, datetime.now()))
    if len(conversation_history[uid]) > MAX_HISTORY:
        conversation_history[uid].pop(0)


def build_messages_with_history(uid, new_text, extra_system=None):
    persona_type = "159专属" if uid == 1592741204 else "普通"
    log(f"  PERSONA选择: {persona_type}")
    system_content = get_persona(uid)
    if extra_system:
        system_content = extra_system + "\n\n" + system_content
    messages = [{"role": "system", "content": system_content}]
    history = get_history(uid)
    for q, a, _ in history:
        messages.append({"role": "user", "content": q})
        messages.append({"role": "assistant", "content": a})
    messages.append({"role": "user", "content": new_text[:200]})
    return messages


# ============ 聊天记录持久化 ============
_saved_cache = {}
_chat_history_lock = threading.Lock()  # 防止多线程交错写坏行


def save_chat_history(uid, text):
    try:
        key = f"{uid}:{text}"
        now = datetime.now().timestamp()
        if key in _saved_cache and (now - _saved_cache[key]) < 10:
            return
        _saved_cache[key] = now
        # 清理过期缓存
        expired = [k for k, v in _saved_cache.items() if now - v > 60]
        for k in expired:
            del _saved_cache[k]
        timestamp = datetime.now().strftime("%m-%d %H:%M:%S")
        line = f"[{timestamp}] QQ{uid}: {text}\n"
        with _chat_history_lock:
            with open(CHAT_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        log(f"保存聊天记录失败: {e}")


def save_bot_reply(uid, text):
    try:
        clean = strip_emoji(text)
        clean = strip_nicknames(uid, clean)
        timestamp = datetime.now().strftime("%m-%d %H:%M:%S")
        line = f"[{timestamp}] 冬 → QQ{uid}: {clean}\n"
        with _chat_history_lock:
            with open(CHAT_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        log(f"保存回复记录失败: {e}")


# ============ #11 记忆雾化与闪回 ============
def apply_memory_fog(memory_entry):
    """记忆随时间衰减：
    - clarity(清晰度): 每天按重要性衰减（重要-8，普通-15），最低0
    - emotional_intensity(情感强度): 每天衰减2%，最低0
      两件事完全分开：清晰度是记忆还清不清晰，情感强度是想起来还激不激动。
    """
    if "clarity" not in memory_entry:
        memory_entry["clarity"] = 100
    if "original" not in memory_entry:
        memory_entry["original"] = memory_entry.get("content", "")
    if "emotional_intensity" not in memory_entry:
        memory_entry["emotional_intensity"] = _get_importance(memory_entry) * 0.5

    try:
        mem_date = datetime.strptime(memory_entry.get("date", ""), "%m-%d")
        now = datetime.now()
        mem_date = mem_date.replace(year=now.year)
        if mem_date > now:
            mem_date = mem_date.replace(year=now.year - 1)
        days_passed = (now - mem_date).days
    except Exception:
        days_passed = 0

    imp = _get_importance(memory_entry)
    decay_rate = 8 if imp >= 0.7 else 15
    memory_entry["clarity"] = max(0, 100 - days_passed * decay_rate)

    # 情感强度随天数衰减：每天-2%，最低0
    memory_entry["emotional_intensity"] = round(
        max(0.0, memory_entry["emotional_intensity"] * (1 - days_passed * 0.02)), 2
    )


def maybe_memory_flashback(uid):
    """深夜有概率触发记忆闪回——随机从旧记忆中提取一条主动提起。
    返回: flashback_text 或 None"""
    if random.random() > 0.06:
        return None

    h = datetime.now().hour
    if not (h >= 23 or h < 5):
        return None

    memory = load_memory()
    if uid not in memory or not memory[uid].get("memories"):
        return None

    # 选一条较早的记忆（至少2天前）
    valid = [m for m in memory[uid]["memories"] if m.get("date")]
    if not valid:
        return None

    chosen = random.choice(valid)
    apply_memory_fog(chosen)

    clarity = chosen.get("clarity", 50)
    content = chosen.get("content", "")

    # 低清晰度可能记错
    if clarity < 40 and random.random() < 0.4:
        distortions = [
            f"我记得好像...你说过{content}？是不是？",
            f"突然想起来，你是不是之前{content}来着。不太确定是不是你。",
            f"刚才想起一件事，你是不是{content}？我可能记岔了。",
        ]
        return random.choice(distortions)

    intro = random.choice([
        "突然想起来", "不知道为什么突然想起", "刚才发呆的时候想到",
        "就是突然...", "嗯...想起一个事",
    ])
    log(f"  记忆闪回: clar={clarity}% {content[:20]}")
    return f"{intro}，你说过{content}"


# ============ #12 记忆再加工 ============
def apply_memory_distortion(memory_entry, current_mood):
    """按当前情绪对记忆内容进行微调。不安期记忆可能被'加工'得更负面。"""
    if "distortion_count" not in memory_entry:
        memory_entry["distortion_count"] = 0
    if "original" not in memory_entry:
        memory_entry["original"] = memory_entry.get("content", "")

    # 只在负面情绪时触发再加工
    if current_mood >= 40:
        return

    # 每条记忆最多再加工2次
    if memory_entry["distortion_count"] >= 2:
        return

    # 低情绪+低清晰度更容易触发
    clarity = memory_entry.get("clarity", 50)
    trigger_chance = (100 - clarity) / 100 * (50 - current_mood) / 50 * 0.15
    if random.random() > trigger_chance:
        return

    original = memory_entry.get("original", memory_entry.get("content", ""))
    # 微调：添加不确定性或负面解读
    distortions = [
        lambda t: t.replace("开心", "还可以").replace("好", "还行"),
        lambda t: t + "（不过那时候好像也不完全是那样...）",
        lambda t: "可能" + t + "吧...也可能不是",
        lambda t: t.replace("说了", "好像说了").replace("做了", "好像做了"),
    ]
    distort = random.choice(distortions)
    memory_entry["content"] = distort(original)
    memory_entry["distortion_count"] += 1

    memory = load_memory()
    # 找到并更新
    for uid_key in memory:
        for m in memory[uid_key].get("memories", []):
            if m.get("original") == memory_entry.get("original"):
                m["content"] = memory_entry["content"]
                m["distortion_count"] = memory_entry["distortion_count"]
                save_memory(memory)
                log(f"  记忆再加工: clar={clarity}% mood={current_mood} \"{original[:20]}\"→\"{memory_entry['content'][:20]}\"")
                return


# ============ #13 内部独白长期记忆 ============
INTERNAL_MONOLOGUE_FILE = os.path.join(os.path.dirname(MEMORY_FILE), "dong_monologues.json")


def store_internal_monologue(text, trigger_type="auto"):
    """存储内部独白到长期记忆"""
    try:
        monos = []
        if os.path.exists(INTERNAL_MONOLOGUE_FILE):
            with open(INTERNAL_MONOLOGUE_FILE, "r", encoding="utf-8") as f:
                monos = json.load(f)
        monos.append({
            "text": text,
            "type": trigger_type,
            "timestamp": datetime.now().isoformat(),
            "mood": 50,  # 会被调用者覆盖
        })
        # 保留最近50条
        monos = monos[-50:]
        with open(INTERNAL_MONOLOGUE_FILE, "w", encoding="utf-8") as f:
            json.dump(monos, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"独白存储失败: {e}")


def maybe_recall_monologue(uid):
    """有概率从旧独白中提取一条，在对话中自然说出来。
    返回: monologue_text 或 None"""
    if random.random() > 0.04:
        return None

    try:
        from .status import _status
        mood = _status.get("mood", 50)
    except Exception:
        mood = 50

    # 情绪极端时更容易触发独白提取
    if abs(mood - 50) < 20 and random.random() > 0.3:
        return None

    try:
        if not os.path.exists(INTERNAL_MONOLOGUE_FILE):
            return None
        with open(INTERNAL_MONOLOGUE_FILE, "r", encoding="utf-8") as f:
            monos = json.load(f)
        if not monos:
            return None
        # 选一条旧独白（至少1天前）
        old_monos = []
        for m in monos:
            try:
                ts = datetime.fromisoformat(m["timestamp"])
                if (datetime.now() - ts).days >= 1:
                    old_monos.append(m)
            except Exception:
                continue
        if not old_monos:
            return None
        chosen = random.choice(old_monos)
        log(f"  独白提取: {chosen['text'][:30]}")
        return f"之前有想过...{chosen['text']}"
    except Exception:
        return None


# ============ #14 记忆合并（防止条目膨胀）============
def consolidate_memories(uid):
    """当用户记忆超过阈值时，合并低重要性旧记忆为虚化印象。
    条件：keywords 交集≥2、importance<0.5、距今>7天"""
    memory = _get_memory_cache()
    uid_key = str(uid)
    if uid_key not in memory:
        return

    mems = memory[uid_key].get("memories", [])
    if not mems:
        return

    # 先做软过期
    _apply_soft_expiry(mems)

    # 更新有效列表
    mems = [m for m in mems if _get_importance(m) >= 0.05]
    if len(mems) <= MEMORY_MAX_PER_USER:
        return

    now = datetime.now()
    # 筛选可合并的记忆：低重要性、较旧
    mergeable = []
    keep = []
    for m in mems:
        imp = _get_importance(m)
        if imp >= 0.5 or imp < 0.05:
            keep.append(m)
            continue
        try:
            mem_date = datetime.strptime(m.get("date", ""), "%m-%d").replace(year=now.year)
            if mem_date > now:
                mem_date = mem_date.replace(year=now.year - 1)
        except Exception:
            keep.append(m)
            continue
        if (now - mem_date).days > 7:
            mergeable.append(m)
        else:
            keep.append(m)

    if len(mergeable) < 3:
        return  # 太少不值得合并

    # 按 keywords 交集分组合并
    merged = []
    used = set()
    for i, m1 in enumerate(mergeable):
        if m1.get("id", i) in used:
            continue
        group = [m1]
        used.add(m1.get("id", i))
        kw1 = set(m1.get("keywords", []))

        for j, m2 in enumerate(mergeable):
            if j <= i or m2.get("id", j) in used:
                continue
            kw2 = set(m2.get("keywords", []))
            if len(kw1 & kw2) >= 2:
                group.append(m2)
                used.add(m2.get("id", j))
                kw1 |= kw2

        if len(group) >= 2:
            # 合并组
            summaries = [m.get("summary", m.get("content", ""))[:20] for m in group]
            merged_imp = max(_get_importance(m) for m in group) * 0.6
            merged_summary = "那段时间好像..." + "、".join(summaries[:3])

            merged.append({
                "id": f"mem_{uid}_merged_{int(time.time() * 1000)}",
                "summary": merged_summary[:40],
                "keywords": list(kw1)[:5],
                "content": merged_summary,
                "date": min(m.get("date", "") for m in group),
                "importance": merged_imp,
                "expires": (now + timedelta(days=30)).strftime("%m-%d"),
                "clarity": min(70, int(sum(m.get("clarity", 100) for m in group) / len(group))),
                "distortion_count": 0,
                "original": merged_summary,
                "access_count": 0,
                "updated": now.isoformat(),
            })
            log(f"  记忆合并: {len(group)}条→1条 \"{merged_summary[:30]}\"")

    # 未被合并的保留
    for m in mergeable:
        if m.get("id", "") not in used:
            keep.append(m)

    memory[uid_key]["memories"] = keep + merged
    save_memory(memory)
    _invalidate_memory_cache()


# ============ #15 自动摘要（分析模型异步生成）============
_last_summary_time = None
_SUMMARY_COOLDOWN = 10  # 两次摘要最小间隔(秒)
_SUPPRESS_UNTIL = None  # 429限流暂停到何时


# ============ #6 日程经历记忆化 ============
_EXPERIENCE_LOG = []  # 运行时缓存的今日经历片段
_EXPERIENCE_MAX = 50


def log_experience(description, exp_type="daily"):
    """运行时记录一条经历片段。如: log_experience('给159发了猫的照片', 'proactive')"""
    _EXPERIENCE_LOG.append({
        "text": description,
        "type": exp_type,
        "time": datetime.now().isoformat()
    })
    if len(_EXPERIENCE_LOG) > _EXPERIENCE_MAX:
        _EXPERIENCE_LOG[:] = _EXPERIENCE_LOG[-_EXPERIENCE_MAX:]


def archive_daily_experiences():
    """跨日时: 把今日经历打包成记忆条目，存入 dong_memory.json (uid='dong_self')"""
    global _EXPERIENCE_LOG
    if not _EXPERIENCE_LOG:
        return

    events = list(_EXPERIENCE_LOG)
    _EXPERIENCE_LOG = []

    now = datetime.now()
    summary_parts = []
    for e in events:
        summary_parts.append(e["text"])
    combined = "；".join(summary_parts[-10:])[:200]  # 最多10条

    if not combined:
        return

    keywords = list(set(
        kw for e in events[-5:]
        for kw in e.get("text", "").replace("，", ",").split(",")[:2]
        if kw.strip()
    ))[:5]

    memory = _get_memory_cache()
    dong_key = "dong_self"
    if dong_key not in memory:
        memory[dong_key] = {"memories": []}

    entry = {
        "id": f"mem_dong_self_{int(now.timestamp() * 1000)}",
        "summary": combined[:30],
        "keywords": keywords,
        "content": combined,
        "date": now.strftime("%m-%d"),
        "importance": 0.5,
        "expires": (now + timedelta(days=7)).strftime("%m-%d"),
        "clarity": 100,
        "distortion_count": 0,
        "original": combined,
        "access_count": 0,
        "updated": now.isoformat(),
    }
    memory[dong_key]["memories"].append(entry)

    # 清理旧经历，保留最近50条
    memory[dong_key]["memories"] = memory[dong_key]["memories"][-50:]

    save_memory(memory)
    _invalidate_memory_cache()
    log(f"  经历记忆化: {combined[:50]}")


# ============ #7 昨日小结 ============
def generate_daily_summary():
    """读取昨日独白+经历，调分析API生成冬风格的昨日小结。
    存储到 dong_daily_summary.json，返回文本或None。"""
    from .config import DAILY_SUMMARY_FILE

    # 收集昨日数据
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    monologue_file = os.path.join(BASE_DIR, "dong_monologues.json")

    yesterday_monologues = []
    if os.path.exists(monologue_file):
        try:
            with open(monologue_file, "r", encoding="utf-8") as f:
                monos = json.load(f)
            for m in monos:
                ts = m.get("timestamp", "")
                if ts.startswith(yesterday):
                    yesterday_monologues.append(m.get("text", ""))
        except Exception:
            pass

    if not yesterday_monologues:
        return None

    combined = "；".join(yesterday_monologues[-5:])[:200]
    if not combined.strip():
        return None

    try:
        from .config import _get_cfg
        cfg = _get_cfg("analysis")

        prompt = f"""你是冬，一个20岁女生，傲娇、嘴硬。用你的口气写一句昨天的小结，20字以内。

你昨天的内心活动：
{combined}

只输出一句话，不要加任何引号或解释。"""

        r = requests.post(
            f"{cfg.api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": cfg.model,
                "temperature": 0.5,
                "max_tokens": 60,
                "messages": [
                    {"role": "system", "content": "你是冬。只输出一句话。"},
                    {"role": "user", "content": prompt}
                ]
            },
            timeout=10
        )

        if r.status_code != 200:
            log(f"  昨日小结API失败: {r.status_code}")
            return None

        resp = r.json()
        if "choices" not in resp or not resp["choices"]:
            return None

        summary = resp["choices"][0]["message"]["content"].strip()[:30]

        # 读取现有小结
        summaries = {}
        if os.path.exists(DAILY_SUMMARY_FILE):
            try:
                with open(DAILY_SUMMARY_FILE, "r", encoding="utf-8") as f:
                    summaries = json.load(f)
            except Exception:
                pass

        today = datetime.now().strftime("%m-%d")
        summaries[today] = summary
        # 保留最近30天
        keys = sorted(summaries.keys())[-30:]
        summaries = {k: summaries[k] for k in keys}

        with open(DAILY_SUMMARY_FILE, "w", encoding="utf-8") as f:
            json.dump(summaries, f, ensure_ascii=False, indent=2)

        log(f"  昨日小结: {summary}")
        return summary

    except Exception as e:
        log(f"  昨日小结异常: {e}")
        return None


def get_today_summary():
    """获取今天的小结（如果有的话）"""
    from .config import DAILY_SUMMARY_FILE
    if not os.path.exists(DAILY_SUMMARY_FILE):
        return ""
    try:
        with open(DAILY_SUMMARY_FILE, "r", encoding="utf-8") as f:
            summaries = json.load(f)
        today = datetime.now().strftime("%m-%d")
        return summaries.get(today, "")
    except Exception:
        return ""


async def auto_summarize_turn(uid, user_msg, bot_reply):
    """异步调用分析模型，将一轮对话压缩为摘要+关键词。不阻塞主回复。"""
    global _last_summary_time, _SUPPRESS_UNTIL

    # 429限流：暂停60秒
    if _SUPPRESS_UNTIL and datetime.now() < _SUPPRESS_UNTIL:
        return

    # 调用间隔保护
    if _last_summary_time:
        elapsed = (datetime.now() - _last_summary_time).total_seconds()
        if elapsed < _SUMMARY_COOLDOWN:
            return

    try:
        from .config import _get_cfg
        cfg = _get_cfg("analysis")

        prompt = (
            "你是一个记忆摘要助手。用一句话（不超过30字）概括以下对话中"
            "最可能在未来被提及的关键信息（如承诺、事件、情绪、决定）。"
            "如果是纯闲聊寒暄，摘要写\"闲聊\"。\n"
            "同时输出3-5个关键词。\n"
            "严格按此格式回复：摘要|关键词1,关键词2,关键词3\n\n"
            f"用户消息：{user_msg[:100]}\n"
            f"冬的回复：{bot_reply[:100]}"
        )

        messages = [
            {"role": "system", "content": "你是一个精确的记忆摘要助手。只输出要求的格式，不输出其他内容。"},
            {"role": "user", "content": prompt},
        ]

        loop = asyncio.get_event_loop()
        r = await loop.run_in_executor(
            None,
            lambda: requests.post(
                f"{cfg.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": cfg.model,
                    "temperature": 0.3,
                    "max_tokens": 150,
                    "messages": messages
                },
                timeout=15
            )
        )

        if r.status_code != 200:
            log(f"  摘要API失败: {r.status_code}")
            if r.status_code == 429:
                _SUPPRESS_UNTIL = datetime.now() + timedelta(seconds=60)
                log(f"  摘要429限流: 暂停60秒")
            return

        _last_summary_time = datetime.now()

        resp = r.json()
        if "choices" not in resp or not resp["choices"]:
            return

        result = resp["choices"][0]["message"]["content"].strip()
        log(f"  摘要生成: {result[:50]}")

        # 解析 "摘要|kw1,kw2,kw3"
        if "|" in result:
            parts = result.split("|", 1)
            summary = parts[0].strip()[:30]
            keywords = [kw.strip() for kw in parts[1].split(",") if kw.strip()][:5]
        else:
            summary = result[:30]
            keywords = _extract_basic_keywords(user_msg + bot_reply)

        if not summary or summary == "闲聊":
            return  # 闲聊不值得存储

        # 存入记忆
        memory = _get_memory_cache()
        uid_key = str(uid)
        if uid_key not in memory:
            memory[uid_key] = {"memories": []}

        # 做软过期
        _apply_soft_expiry(memory[uid_key]["memories"])

        now = datetime.now()
        mem_id = f"mem_{uid}_{int(time.time() * 1000)}"

        memory[uid_key]["memories"].append({
            "id": mem_id,
            "summary": summary,
            "keywords": keywords,
            "content": f"用户：{user_msg[:100]}\n冬：{bot_reply[:100]}",
            "date": now.strftime("%m-%d"),
            "importance": 0.5,  # 自动摘要默认中等重要性
            "expires": (now + timedelta(days=5)).strftime("%m-%d"),
            "clarity": 100,
            "distortion_count": 0,
            "original": summary,
            "access_count": 0,
            "updated": now.isoformat(),
        })

        save_memory(memory)
        _invalidate_memory_cache()
        log(f"  摘要记忆已保存: {summary[:30]} [{','.join(keywords[:3])}]")

        # 检查是否需要合并
        if len(memory[uid_key]["memories"]) > MEMORY_MAX_PER_USER:
            consolidate_memories(uid)

    except Exception as e:
        log(f"  摘要任务失败: {e}")
