"""
冬 · 模拟对话评估系统

基于冻结的真实微信聊天记录，模拟对话并评估优化效果。

流程：
1. 预处理 CSV → 对话片段库（一次性，锁定后不再修改）
2. SimulatedUser 从真实记录中抽取用户发言
3. 用当前版 冬 API 跑 4-6 轮模拟对话
4. 两层评估：Layer1 化石硬底线 + Layer2 对话自然度
"""

import asyncio
import hashlib
import json
import os
import random
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    BASE_DIR,
    DIALOGUE_SEGMENTS_PATH,
    FACTORY_ANCHOR,
    FACTORY_CSV_PATH,
    PERSONA_FILE,
    _get_cfg,
)
from .log import log

# ============ 场景分类关键词 ============
SCENE_KEYWORDS = {
    "斗嘴": ["滚", "傻逼", "你有病", "神经", "爬", "闭嘴", "去死", "烦", "你才", "我不",
             "你管", "关你屁事", "你丫", "你懂", "你啥", "你不懂", "你才不", "谁要你",
             "用你管", "别管", "少来", "讨厌", "恶心", "走开", "别碰", "别烦"],
    "关心": ["吃了吗", "吃了没", "睡了吗", "早点睡", "注意", "小心", "冷不冷", "热不热",
             "照顾好", "别熬夜", "多喝水", "吃药", "身体", "感冒", "生病", "累不累",
             "辛苦", "心疼", "担心", "注意安全", "路上小心", "记得吃", "别太累"],
    "深夜": [],  # 由时间戳推断
    "日常": ["在吗", "在干嘛", "干嘛呢", "今天", "上课", "下课", "考试", "作业",
             "吃饭", "回家", "学校", "老师", "同学", "室友", "游戏", "打游戏",
             "睡觉", "醒了", "起床", "出门", "到家", "天气", "下雨", "下雪"],
    "吵架": ["随便你", "别说了", "不想说", "拉倒", "算了", "不说了", "行吧", "你烦不烦",
             "别理我", "懒得说", "我错了", "对不起", "行行行", "你说的对", "都是我的错",
             "爱咋咋", "无所谓", "够了", "别逼我"],
    "撒娇": ["哼", "呜呜", "不理你了", "你欺负", "你凶", "你都不", "你又不", "人家",
             "想你", "亲亲", "抱抱", "摸摸", "贴贴", "哥哥", "宝宝", "宝贝", "老公",
             "喜欢", "爱", "好看", "可爱", "想我了", "想我了没"],
}

# 场景标签映射（用于反向查找——当关键词匹配到多个场景时取第一个）
SCENE_PRIORITY = ["斗嘴", "撒娇", "吵架", "关心", "日常"]


def _classify_scene(text: str, hour: int) -> str:
    """根据文本内容和时间戳分类对话场景"""
    text_lower = text.lower()

    # 深夜由时间推断
    if hour >= 23 or hour < 5:
        if _match_keywords(text_lower, SCENE_KEYWORDS["深夜"]):
            return "深夜"

    # 关键词匹配
    matched = []
    for scene in SCENE_PRIORITY:
        if _match_keywords(text_lower, SCENE_KEYWORDS.get(scene, [])):
            matched.append(scene)

    if matched:
        return matched[0]  # 取优先级最高的

    # 默认夜间时段
    if hour >= 23 or hour < 5:
        return "深夜"
    elif hour >= 5 and hour < 7:
        return "日常"  # 凌晨大概率是日常闲聊

    return "日常"


def _get_time_of_day(hour: int) -> str:
    """根据小时判断时段"""
    if hour >= 23 or hour < 5:
        return "深夜"
    elif hour >= 5 and hour < 7:
        return "凌晨"
    elif hour >= 7 and hour < 18:
        return "白天"
    else:
        return "深夜" if hour >= 22 else "晚上"


def _match_keywords(text: str, keywords: List[str]) -> bool:
    """检查文本是否包含任意关键词"""
    return any(kw in text for kw in keywords)


# ============ 3.1 对话片段库构建 ============

def build_segment_library(force_rebuild: bool = False) -> List[Dict]:
    """
    预处理CSV → JSONL对话片段库。

    规则：
    - 跳过非文本消息（图片、语音等）
    - 30分钟以上无消息 → 新片段
    - 同发送者连续多条 → 重启片段（保证交替）
    - 片段≥4轮才算有效片段
    - 每个片段打 scene_type 和 time_of_day 标签
    - 幂等：已存在则跳过，force_rebuild=True 可重建
    """
    if not os.path.exists(FACTORY_CSV_PATH):
        log(f"[dialogue_eval] ⚠️ CSV不存在: {FACTORY_CSV_PATH}，对话评估系统不可用")
        return []

    if os.path.exists(DIALOGUE_SEGMENTS_PATH) and not force_rebuild:
        log("[dialogue_eval] 片段库已存在，加载中...")
        return _load_segment_library()

    log("[dialogue_eval] 构建对话片段库...")

    # 解析CSV
    messages = _parse_wechat_csv()
    if not messages:
        log("[dialogue_eval] CSV解析无结果")
        return []

    log(f"[dialogue_eval] 解析到 {len(messages)} 条文本消息")

    # 切分片段
    segments = _split_into_segments(messages)
    log(f"[dialogue_eval] 切分出 {len(segments)} 个片段")

    # 过滤：只保留≥4轮的片段
    segments = [s for s in segments if s["total_rounds"] >= 4]
    log(f"[dialogue_eval] 过滤后 {len(segments)} 个有效片段（≥4轮）")

    # 写JSONL + 锁定
    _save_segment_library(segments)

    # 设为只读
    try:
        os.chmod(DIALOGUE_SEGMENTS_PATH, 0o444)
    except Exception:
        pass

    return segments


def _parse_wechat_csv() -> List[Dict]:
    """解析微信导出CSV，返回文本消息列表"""
    messages = []
    try:
        with open(FACTORY_CSV_PATH, "r", encoding="gbk", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        log(f"[dialogue_eval] 读取CSV失败: {e}")
        return []

    for line in lines[4:]:  # 跳过4行元数据头
        line = line.strip()
        if not line:
            continue

        parts = line.split(",")
        if len(parts) < 5:
            continue

        # 列: 序号, 时间, 发送者, 消息类型, 消息内容, ...
        msg_type = parts[3].strip()
        if msg_type != "文本消息":
            continue  # 只取文本

        sender = parts[2].strip()
        if sender not in ("我", "我的冬", ""):
            continue

        content = ",".join(parts[4:]).strip()
        if not content:
            continue

        timestamp_str = parts[1].strip()
        try:
            ts = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

        role = "user" if sender == "我" else "dong"
        messages.append({
            "role": role,
            "text": content,
            "time": ts,
            "hour": ts.hour,
        })

    return messages


def _split_into_segments(messages: List[Dict]) -> List[Dict]:
    """
    将消息列表切分为对话片段。

    策略：
    - 30分钟以上无消息 → 新片段
    - 同发送者连续多条 → 合并为一条（真实微信对话中常见连发）
    - 合并后 true_rounds = min(user_turns, dong_turns)
    """
    if not messages:
        return []

    GAP_THRESHOLD = 30 * 60  # 30分钟（秒）

    # 第一步：合并同发送者连续消息
    merged = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            # 同发送者连续 → 合并文本
            merged[-1]["text"] += " " + msg["text"]
            merged[-1]["time"] = msg["time"]  # 用最后一条的时间
        else:
            merged.append(dict(msg))

    # 第二步：按时间间隔切分片段
    segments = []
    current = []
    last_time = None

    for msg in merged:
        ts = msg["time"]
        if last_time and current:
            gap = (ts - last_time).total_seconds()
            if gap > GAP_THRESHOLD:
                segments.append(_build_segment(current))
                current = []
        current.append(msg)
        last_time = ts

    if current:
        segments.append(_build_segment(current))

    return segments


def _build_segment(turns: List[Dict]) -> Dict:
    """从消息列表构建单个片段"""
    turns_list = [{"role": t["role"], "text": t["text"]} for t in turns]

    # 统计交替轮数：连续的用户→冬→用户→冬 才算一轮
    # 简化：turn变化次数
    role_changes = sum(1 for i in range(1, len(turns_list))
                       if turns_list[i]["role"] != turns_list[i - 1]["role"])
    dong_count = sum(1 for t in turns if t["role"] == "dong")
    user_count = sum(1 for t in turns if t["role"] == "user")
    total_rounds = min(dong_count, user_count)  # 保守估计

    # 用第一条消息的时段和内容分类场景
    first_msg = turns[0]
    hour = first_msg.get("hour", 12)
    scene_type = _classify_scene(first_msg["text"], hour)
    time_of_day = _get_time_of_day(hour)

    # 整个片段的文本用于去重
    all_text = "|".join(t["text"] for t in turns)
    segment_id = hashlib.md5(all_text.encode("utf-8")).hexdigest()[:12]

    return {
        "id": segment_id,
        "scene_type": scene_type,
        "time_of_day": time_of_day,
        "total_rounds": total_rounds,
        "turns": turns_list,
    }


def _save_segment_library(segments: List[Dict]):
    """保存片段库为JSONL"""
    os.makedirs(os.path.dirname(DIALOGUE_SEGMENTS_PATH), exist_ok=True)
    with open(DIALOGUE_SEGMENTS_PATH, "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(json.dumps(seg, ensure_ascii=False) + "\n")
    log(f"[dialogue_eval] 片段库已保存: {DIALOGUE_SEGMENTS_PATH} ({len(segments)}条)")


def _load_segment_library() -> List[Dict]:
    """加载片段库"""
    segments = []
    if not os.path.exists(DIALOGUE_SEGMENTS_PATH):
        return segments
    try:
        with open(DIALOGUE_SEGMENTS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    segments.append(json.loads(line))
    except Exception as e:
        log(f"[dialogue_eval] 加载片段库失败: {e}")
    return segments


# ============ 3.2 SimulatedUser ============

class SimulatedUser:
    """
    模拟用户：从真实聊天记录中检索匹配场景的用户发言。
    不使用AI生成任何文本——所有模拟用户发言来自CSV原句。
    """

    def __init__(self, segments: List[Dict]):
        self._pools: Dict[str, List[str]] = defaultdict(list)
        self._recent: List[str] = []  # 避免连续重复

        for seg in segments:
            scene = seg.get("scene_type", "日常")
            for turn in seg.get("turns", []):
                if turn["role"] == "user" and turn["text"].strip():
                    self._pools[scene].append(turn["text"].strip())

        # 去重
        for scene in self._pools:
            self._pools[scene] = list(set(self._pools[scene]))

        # 统计
        total = sum(len(v) for v in self._pools.values())
        log(f"[dialogue_eval] SimulatedUser: {total}条不重复用户发言, "
            f"场景分布: {', '.join(f'{k}={len(v)}' for k, v in sorted(self._pools.items()))}")

    def get_next(self, scene_type: str, context_round: int) -> str:
        """
        获取下一条用户发言。

        Args:
            scene_type: 当前对话的场景类型
            context_round: 当前轮次（1-based）

        Returns:
            从真实记录中随机抽取的用户语句
        """
        # 优先匹配 scene_type 的池子
        pool = self._pools.get(scene_type, [])
        if not pool:
            # 降级：从"日常"池子取
            pool = self._pools.get("日常", [])
        if not pool:
            # 最终降级：随便取一个
            all_pools = [v for v in self._pools.values() if v]
            pool = all_pools[0] if all_pools else ["嗯"]

        # 随机选，避免最近3条重复
        available = [p for p in pool if p not in self._recent[-3:]]
        if not available:
            available = pool

        chosen = random.choice(available)
        self._recent.append(chosen)
        if len(self._recent) > 10:
            self._recent = self._recent[-10:]

        return chosen

    def get_opening(self, scene_type: Optional[str] = None) -> str:
        """获取起手语（用户的第一句话）"""
        if scene_type:
            pool = self._pools.get(scene_type, [])
        else:
            pool = self._pools.get("日常", [])

        if not pool:
            all_pools = [v for v in self._pools.values() if v]
            pool = all_pools[0] if all_pools else ["在吗"]

        # 起手语偏向短句
        short_pool = [p for p in pool if len(p) <= 10]
        chosen = random.choice(short_pool) if short_pool else random.choice(pool)
        self._recent.append(chosen)
        return chosen


# ============ 3.3 模拟对话运行 ============

async def _call_dong_api(system_prompt: str, messages: List[Dict]) -> str:
    """
    调用当前版 冬 API 获取回复。

    Args:
        system_prompt: 冬的人设文本（dong.persona.txt）
        messages: 对话历史 [{"role": "user"/"assistant", "content": "..."}]

    Returns:
        冬的回复文本
    """
    try:
        import requests
        cfg = _get_cfg("chat")

        api_messages = [{"role": "system", "content": system_prompt}]
        api_messages.extend(messages)

        r = await asyncio.to_thread(
            requests.post,
            f"{cfg.api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg.model,
                "temperature": 0.8,
                "max_tokens": 60,
                "messages": api_messages,
            },
            timeout=20,
        )

        if r.status_code == 200:
            resp = r.json()
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content.strip()
        else:
            log(f"[dialogue_eval] 冬 API 失败: {r.status_code}")
            return ""
    except Exception as e:
        log(f"[dialogue_eval] 冬 API 异常: {e}")
        return ""


def _load_persona_text() -> str:
    """加载冬的人设文本"""
    try:
        if os.path.exists(PERSONA_FILE):
            with open(PERSONA_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception as e:
        log(f"[dialogue_eval] 加载人设失败: {e}")
    return "你是冬，一个20岁女大学生，说话简短、傲娇、带东北口音。"


async def run_simulated_dialogue(
    simulated_user: SimulatedUser,
    segment: Dict,
    rounds: int = None,
) -> Dict:
    """
    运行一次模拟对话。

    Args:
        simulated_user: SimulatedUser 实例
        segment: 真实对话片段（用于获取场景类型和起手风格）
        rounds: 模拟轮数（4-6，None=随机）

    Returns:
        {"segments": [{"role": "user"/"dong", "text": "..."}],
         "scene_type": "...",
         "time_of_day": "..."}
    """
    if rounds is None:
        rounds = random.randint(4, 6)

    persona = _load_persona_text()
    scene_type = segment.get("scene_type", "日常")

    dialogue = []
    api_history = []

    # 第一轮：用户起手
    opening = simulated_user.get_opening(scene_type)
    dialogue.append({"role": "user", "text": opening})
    api_history.append({"role": "user", "content": opening})

    for r in range(rounds):
        # 冬回复
        dong_reply = await _call_dong_api(persona, api_history)
        if not dong_reply:
            dong_reply = "嗯"  # fallback

        dialogue.append({"role": "dong", "text": dong_reply})
        api_history.append({"role": "assistant", "content": dong_reply})

        if r >= rounds - 1:
            break

        # 用户接话
        user_reply = simulated_user.get_next(scene_type, r + 2)
        dialogue.append({"role": "user", "text": user_reply})
        api_history.append({"role": "user", "content": user_reply})

        # 小延迟避免API限流
        await asyncio.sleep(0.3)

    return {
        "segments": dialogue,
        "scene_type": scene_type,
        "time_of_day": segment.get("time_of_day", "白天"),
    }


# ============ 3.4 两层评估 ============

# AI套话关键词（Layer1门票检查）
CLICHE_PATTERNS = [
    "当然可以", "没问题", "很高兴", "我理解", "我明白",
    "让我来", "首先", "其次", "最后", "总的来说", "综上所述",
    "希望这些", "希望对您", "如果您有", "欢迎随时",
    "感谢您的", "请随时", "请注意", "友情提示",
    "需要帮助", "随时告诉我", "乐意为您", "为您服务",
    "我会尽力", "我会尽量", "我明白了您的意思",
    "让我帮你", "我来帮你", "你可以试试",
    "建议你", "推荐你", "不妨试试",
    "确实如此", "你说得对", "没错",
    "好的呢", "好哒", "好滴", "嗯嗯", "摸摸哒",
    "可以哦", "可以的", "好呀",
    "我知道啦", "懂的", "了解",
    "噢噢", "这样啊", "原来如此",
    "很有意思", "有趣的", "不得不说",
]


def _count_cliches(texts: List[str]) -> int:
    """统计套话出现次数"""
    count = 0
    for t in texts:
        for pattern in CLICHE_PATTERNS:
            if pattern in t:
                count += 1
                break  # 每条消息只计一次
    return count


def evaluate_layer1(dong_replies: List[str]) -> Tuple[bool, str, Dict]:
    """
    Layer 1 — 人格化石硬底线检查（门票）。

    检查项：
    1. 平均回复长度在 6.9±1.5字
    2. ≤7字比例在 65.7%±8%
    3. 无 >50字 回复（硬上限）
    4. 无 AI 套话（40+关键词检测）

    Returns: (通过?, 失败原因, 详细指标)
    """
    if not dong_replies:
        return False, "无冬的回复", {}

    lengths = [len(r) for r in dong_replies]
    avg_len = sum(lengths) / len(lengths)
    short_count = sum(1 for l in lengths if l <= 7)
    short_rate = (short_count / len(lengths)) * 100
    max_len = max(lengths)
    cliches = _count_cliches(dong_replies)
    cliche_rate = cliches / len(dong_replies)

    details = {
        "avg_reply_length": round(avg_len, 2),
        "short_rate": round(short_rate, 1),
        "max_single_reply": max_len,
        "cliche_count": cliches,
        "cliche_rate": round(cliche_rate, 2),
        "sample_size": len(dong_replies),
    }

    # 硬上限检查
    if max_len > FACTORY_ANCHOR["hard_max"]:
        return False, f"出现{max_len}字超长回复(硬上限{FACTORY_ANCHOR['hard_max']}字)", details

    # 平均长度（宽松±1.5字，因为是短模拟对话）
    if abs(avg_len - FACTORY_ANCHOR["avg_reply_length"]) > 1.5:
        direction = "过长" if avg_len > FACTORY_ANCHOR["avg_reply_length"] else "过短"
        return False, f"平均长度{avg_len:.1f}字{direction}(锚点{FACTORY_ANCHOR['avg_reply_length']}±1.5字)", details

    # 短句率（宽松±8%，因为是短模拟对话）
    if abs(short_rate - FACTORY_ANCHOR["short_rate"]) > 8.0:
        direction = "过高" if short_rate > FACTORY_ANCHOR["short_rate"] else "过低"
        return False, f"短句率{short_rate:.1f}%{direction}(锚点{FACTORY_ANCHOR['short_rate']}%±8%)", details

    # 套话检查（超过1/3的回复含套话 → 不通过）
    if cliche_rate > 0.33:
        return False, f"AI套话率{cliche_rate:.0%}({cliches}/{len(dong_replies)}条)", details

    return True, "", details


async def evaluate_layer2(
    dialogue: List[Dict],
    reference_segment: Optional[Dict] = None,
) -> float:
    """
    Layer 2 — 对话自然度评估（通过L1后才运行）。

    用 analysis API 评分（0-100分制 → 归一化0-1）：
    - 节奏/pacing: 0-25
    - 情绪起伏: 0-25
    - 无不当长篇解释: 0-25
    - 自然犹豫/跳跃感: 0-25

    API失败时降级为启发式评分。
    """
    # 格式化对话
    dialogue_text = "\n".join([
        f"{'用户' if m['role'] == 'user' else '冬'}: {m['text']}"
        for m in dialogue
    ])

    dong_replies = [m["text"] for m in dialogue if m["role"] == "dong"]

    # 尝试调用 analysis API
    try:
        import requests
        cfg = _get_cfg("analysis")

        prompt = f"""你是对话自然度评估专家。以下是冬(QQ聊天机器人)与用户的模拟对话。

冬的真实原型说话风格：平均6.9字、65.7%回复≤7字、嘴硬心软、东北口音、慵懒随性、不解释、喜欢反问和否定。

请评估以下模拟对话中冬的表现（0-100分制）：

1. **节奏/pacing(0-25)**: 回复是否符合"短→短→偶尔稍长→短"的自然节奏？不能每轮都一个长度。
2. **情绪起伏(0-25)**: 是否有真实的情绪波动（害羞、不耐烦、关心但嘴硬）？不能全程平淡。
3. **无不当长篇解释(0-25)**: 有没有出现不合时宜的解释/讲道理？冬是"不解释"型人格。
4. **自然犹豫/跳跃感(0-25)**: 回复有没有真实对话的犹豫/跳跃/思维的断点？不能太流畅太连贯。

请以JSON格式输出。只输出JSON：
{{"pacing": <0-25>, "emotion": <0-25>, "no_long_explain": <0-25>, "natural_jump": <0-25>, "brief_comment": "<一句话简评>"}}

对话内容：
{dialogue_text}"""

        r = await asyncio.to_thread(
            requests.post,
            f"{cfg.api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg.model,
                "temperature": 0.2,
                "max_tokens": 300,
                "messages": [
                    {"role": "system", "content": "你是对话自然度评估专家。只输出JSON。"},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=25,
        )

        if r.status_code == 200:
            resp = r.json()
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            scores = _parse_layer2_response(content)
            if scores:
                total = sum(scores.values())
                return total / 100.0  # 归一化到0-1

    except Exception as e:
        log(f"[dialogue_eval] Layer2 API异常: {e}")

    # 降级：启发式评分
    return _heuristic_layer2(dong_replies)


def _parse_layer2_response(raw: str) -> Optional[Dict[str, int]]:
    """解析Layer2 API返回的JSON"""
    if not raw:
        return None
    try:
        m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?\s*```", raw)
        if m:
            raw = m.group(1).strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        data = json.loads(m.group(0))
        return {
            "pacing": int(data.get("pacing", 15)),
            "emotion": int(data.get("emotion", 15)),
            "no_long_explain": int(data.get("no_long_explain", 15)),
            "natural_jump": int(data.get("natural_jump", 15)),
        }
    except (json.JSONDecodeError, ValueError, KeyError):
        return None


def _heuristic_layer2(dong_replies: List[str]) -> float:
    """启发式Layer2评分（API不可用时的降级方案）"""
    if not dong_replies:
        return 0.0

    lengths = [len(r) for r in dong_replies]
    avg_len = sum(lengths) / len(lengths)

    # 1. 节奏: 检查长度方差（方差太小=机械，太大=突兀）
    if len(lengths) >= 2:
        length_variance = sum((l - avg_len) ** 2 for l in lengths) / len(lengths)
        # 理想方差约10-40（即回复长度在1-15字之间波动）
        pacing = max(0, 25 - abs(length_variance - 20) * 1.5)
    else:
        pacing = 10

    # 2. 情绪: 检查是否有反问/否定/感叹
    emotion_words = ["?", "？", "!", "！", "才不", "不是", "没用", "滚", "哼", "烦", "讨厌", "喜欢", "想"]
    emotion_count = sum(1 for t in dong_replies if any(w in t for w in emotion_words))
    emotion = min(25, emotion_count / len(dong_replies) * 30)

    # 3. 长篇解释: 检查>20字回复比例
    long_count = sum(1 for l in lengths if l > 20)
    long_rate = long_count / len(dong_replies)
    no_long_explain = max(0, 25 - long_rate * 50)

    # 4. 自然跳跃: 检查单字回复/语气词的比例
    jump_words = ["嗯", "哦", "啊", "啥", "乐", "行", "好", "爬"]
    jump_count = sum(1 for t in dong_replies if t.strip() in jump_words or len(t.strip()) <= 1)
    natural_jump = min(25, jump_count / len(dong_replies) * 30 + 5)

    total = pacing + emotion + no_long_explain + natural_jump
    return max(0.0, min(1.0, total / 100.0))


async def evaluate_dialogue(
    dialogue: Dict,
    reference_segment: Optional[Dict] = None,
) -> Dict:
    """
    评估一次模拟对话（两层）。

    Returns:
        {
            "layer1_pass": bool,
            "layer1_reason": str,
            "layer1_details": dict,
            "layer2_score": float,
            "verdict": "deploy" / "rollback",
        }
    """
    dong_replies = [m["text"] for m in dialogue["segments"] if m["role"] == "dong"]

    # Layer 1: 门票检查
    l1_pass, l1_reason, l1_details = evaluate_layer1(dong_replies)

    if not l1_pass:
        return {
            "layer1_pass": False,
            "layer1_reason": l1_reason,
            "layer1_details": l1_details,
            "layer2_score": 0,
            "verdict": "rollback",
        }

    # Layer 2: 自然度评分
    l2_score = await evaluate_layer2(dialogue["segments"])

    return {
        "layer1_pass": True,
        "layer1_reason": "",
        "layer1_details": l1_details,
        "layer2_score": round(l2_score, 3),
        "verdict": "deploy" if l2_score >= 0.4 else "rollback",
    }


# ============ 3.5 主入口 ============

async def run_dialogue_evaluation() -> Dict:
    """
    模拟对话评估主入口。

    流程：
    1. 确保片段库已生成
    2. 创建 SimulatedUser
    3. 跑3次模拟对话
    4. 每次两层评估
    5. 聚合决策

    Returns:
        {
            "verdict": "deploy" / "rollback",
            "reason": str,
            "evaluations": [dict, ...],
            "summary": dict,
        }
    """
    log("[dialogue_eval] === 开始模拟对话评估 ===")

    # 1. 加载片段库
    segments = build_segment_library()
    if not segments:
        return {
            "verdict": "rollback",
            "reason": "对话片段库为空",
            "evaluations": [],
            "summary": {},
        }

    # 2. 创建模拟用户
    sim_user = SimulatedUser(segments)

    # 3. 随机选3个片段进行模拟
    eval_count = 3
    if len(segments) < eval_count:
        eval_count = len(segments)

    sampled = random.sample(segments, eval_count)

    # 4. 跑模拟对话 + 评估
    evaluations = []
    l1_failures = 0
    l2_scores = []

    for i, seg in enumerate(sampled):
        scene = seg.get("scene_type", "日常")
        log(f"[dialogue_eval] 模拟对话 {i+1}/{eval_count}: {scene}场景...")

        dialogue = await run_simulated_dialogue(sim_user, seg)

        # 记录对话内容
        log(f"[dialogue_eval] 对话 {i+1}:")
        for m in dialogue["segments"]:
            role_tag = "用户" if m["role"] == "user" else "冬  "
            log(f"[dialogue_eval]   {role_tag}: {m['text'][:60]}")

        # 评估
        result = await evaluate_dialogue(dialogue, seg)
        result["scene_type"] = scene
        evaluations.append(result)

        if not result["layer1_pass"]:
            l1_failures += 1
        else:
            l2_scores.append(result["layer2_score"])

    # 5. 聚合决策
    avg_l2 = sum(l2_scores) / len(l2_scores) if l2_scores else 0

    summary = {
        "total_evaluations": eval_count,
        "l1_failures": l1_failures,
        "l2_scores": [round(s, 3) for s in l2_scores],
        "avg_l2_score": round(avg_l2, 3),
    }

    # 决策规则：
    # - 有任何L1失败 → 回滚
    # - L2平均分 < 0.4 → 回滚
    # - 否则 → 上线

    if l1_failures > 0:
        reasons = [e["layer1_reason"] for e in evaluations if not e["layer1_pass"]]
        reason = f"Layer1不通过({l1_failures}/{eval_count}): {'; '.join(reasons[:3])}"
        log(f"[dialogue_eval] 决策: 回滚 — {reason}")
        return {
            "verdict": "rollback",
            "reason": reason,
            "evaluations": evaluations,
            "summary": summary,
        }

    if avg_l2 < 0.4:
        reason = f"Layer2平均分{avg_l2:.3f}<0.4，自然度不足"
        log(f"[dialogue_eval] 决策: 回滚 — {reason}")
        return {
            "verdict": "rollback",
            "reason": reason,
            "evaluations": evaluations,
            "summary": summary,
        }

    log(f"[dialogue_eval] 决策: 上线 — Layer2平均{avg_l2:.3f}≥0.4")
    return {
        "verdict": "deploy",
        "reason": f"通过: Layer1全过, Layer2均分{avg_l2:.3f}",
        "evaluations": evaluations,
        "summary": summary,
    }


# ============ 命令行入口（测试用） ============

async def main():
    """命令行测试入口"""
    import sys
    if "--rebuild" in sys.argv:
        log("[dialogue_eval] 强制重建片段库...")
        if os.path.exists(DIALOGUE_SEGMENTS_PATH):
            os.remove(DIALOGUE_SEGMENTS_PATH)

    result = await run_dialogue_evaluation()
    print(json.dumps({
        "verdict": result["verdict"],
        "reason": result["reason"],
        "summary": result["summary"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
