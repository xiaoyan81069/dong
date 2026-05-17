"""
冬 · 风格指纹模块

从出厂数据（factory_archive.json + 原始微信CSV）建立冬的**不可变风格锚点**。
评估时始终对比"当前版本 vs 出厂锚点"，防止累积漂移。

数据源：
  1. dong_factory_archive.json — AI蒸馏出的结构化性格/风格描述
  2. 私聊_我的冬.csv — 原始微信导出，提供精确的回复长度/频率统计

核心基准（来自CSV统计）：
  - 平均回复长度: 6.9字
  - 短句率(≤7字): 65.7%
  - 最大回复长度: 34字（从未超过50字）
  - 1-3字回复: 23.9%, 4-7字: 41.9%, 8-15字: 29.0%
"""
import csv
import json
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .log import log


# ============ 数据结构 ============

@dataclass
class StyleFingerprint:
    """冬的量化风格指纹（不可变锚点）"""

    # ---- 从CSV统计的真实数据 ----
    avg_reply_length: float = 6.9
    median_reply_length: int = 6
    max_reply_length: int = 34
    reply_length_distribution: Dict[str, float] = field(default_factory=lambda: {
        "1-3": 0.239,
        "4-7": 0.419,
        "8-15": 0.290,
        "16-30": 0.051,
        "31+": 0.002,
    })
    single_char_reply_rate: float = 0.047
    short_reply_rate: float = 0.657  # ≤7字
    long_reply_rate: float = 0.002   # >30字
    total_messages: int = 2331

    # ---- 常用单字回复 ----
    common_single_chars: Dict[str, int] = field(default_factory=dict)

    # ---- 高频短语/口癖（从CSV提取） ----
    frequent_phrases: Dict[str, int] = field(default_factory=dict)
    # 例如: {"好饿": 23, "滚啊滚啊": 12, "加油": 18, ...}

    # ---- 句式模式 ----
    uses_question_back: float = 0.0   # 反问率
    uses_negation: float = 0.0        # 否定句式率（"不""没""才不"）
    uses_deflection: float = 0.0      # 回避/转移话题率

    # ---- 从factory_archive提取的风格标记 ----
    core_tone: str = ""              # "直接、略带防御但温柔，常用反问和否定句式"
    catchphrases: List[str] = field(default_factory=list)
    personality_core: str = ""
    communication_style: Dict = field(default_factory=dict)

    # ---- 元数据 ----
    generated_at: str = ""
    source: str = "factory_archive + CSV"


# ============ 指纹计算（一次性初始化） ============

def compute_fingerprint(csv_path: str, factory_path: str) -> StyleFingerprint:
    """
    从 CSV + factory_archive 提取出厂风格指纹。
    这是一次性操作——结果持久化到 style_fingerprint.json。
    """
    fp = StyleFingerprint()

    # ---- 1. 解析CSV ----
    messages = _parse_csv(csv_path)
    if messages:
        _extract_length_stats(fp, messages)
        _extract_single_chars(fp, messages)
        _extract_frequent_phrases(fp, messages)
        _extract_sentence_patterns(fp, messages)
        fp.total_messages = len(messages)
        log(f"[指纹] CSV解析: {len(messages)}条文本消息")

    # ---- 2. 提取factory_archive ----
    factory = _load_json(factory_path)
    if factory:
        _extract_factory_traits(fp, factory)
        log(f"[指纹] factory_archive加载完成")

    fp.generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return fp


def _parse_csv(csv_path: str) -> List[str]:
    """解析微信导出CSV，提取"我的冬"的文本消息。GBK编码。"""
    messages = []
    if not os.path.exists(csv_path):
        log(f"[指纹] CSV文件不存在: {csv_path}")
        return messages

    try:
        with open(csv_path, "r", encoding="gbk") as f:
            reader = csv.reader(f)
            rows = list(reader)

        # 找到数据开始行（跳过元数据头）
        data_start = 0
        for i, row in enumerate(rows):
            if len(row) >= 5:
                # 检查是否为数据行：第一列是数字序号
                try:
                    int(row[0])
                    data_start = i
                    break
                except ValueError:
                    continue

        for row in rows[data_start:]:
            if len(row) >= 5 and row[2] == "我的冬" and row[3] == "文本消息":
                text = row[4].strip()
                if text and len(text) >= 1:
                    messages.append(text)
    except Exception as e:
        log(f"[指纹] CSV解析失败: {e}")
    return messages


def _extract_length_stats(fp: StyleFingerprint, messages: List[str]):
    """从消息列表提取长度统计"""
    if not messages:
        return
    lengths = [len(m) for m in messages]
    fp.avg_reply_length = sum(lengths) / len(lengths)
    sorted_lens = sorted(lengths)
    fp.median_reply_length = sorted_lens[len(sorted_lens) // 2]
    fp.max_reply_length = max(lengths)

    # 分布
    buckets = {"1-3": 0, "4-7": 0, "8-15": 0, "16-30": 0, "31+": 0}
    for l in lengths:
        if l <= 3:
            buckets["1-3"] += 1
        elif l <= 7:
            buckets["4-7"] += 1
        elif l <= 15:
            buckets["8-15"] += 1
        elif l <= 30:
            buckets["16-30"] += 1
        else:
            buckets["31+"] += 1

    total = len(messages)
    fp.reply_length_distribution = {k: v / total for k, v in buckets.items()}
    fp.short_reply_rate = (buckets["1-3"] + buckets["4-7"]) / total
    fp.long_reply_rate = buckets["31+"] / total


def _extract_single_chars(fp: StyleFingerprint, messages: List[str]):
    """统计单字回复频率"""
    single_chars = Counter()
    for m in messages:
        stripped = m.strip()
        if len(stripped) == 1:
            single_chars[stripped] += 1
    # 取前20个
    fp.common_single_chars = dict(single_chars.most_common(20))
    fp.single_char_reply_rate = sum(single_chars.values()) / len(messages) if messages else 0


def _extract_frequent_phrases(fp: StyleFingerprint, messages: List[str]):
    """提取高频2-4字短语"""
    phrases = Counter()
    for m in messages:
        stripped = re.sub(r"[^\u4e00-\u9fff\w]", "", m)
        for l in [2, 3, 4]:
            for i in range(0, len(stripped) - l + 1):
                phrase = stripped[i:i + l]
                # 过滤纯数字/标点
                if re.match(r"^[\u4e00-\u9fff]+$", phrase):
                    phrases[phrase] += 1
    # 保留出现>=3次的，取前40个
    fp.frequent_phrases = dict({k: v for k, v in phrases.most_common(40) if v >= 3})


def _extract_sentence_patterns(fp: StyleFingerprint, messages: List[str]):
    """提取句式模式"""
    if not messages:
        return
    total = len(messages)

    # 反问句率（以"？"或"?"结尾的短句）
    question_back = sum(1 for m in messages
                        if (m.rstrip().endswith("？") or m.rstrip().endswith("?"))
                        and len(m) <= 15)
    fp.uses_question_back = question_back / total

    # 否定句式率（含"不""没""才不"的句子）
    negation = sum(1 for m in messages
                   if re.search(r"不|没|才不|哪有", m))
    fp.uses_negation = negation / total

    # 回避/转移话题率（"随便""算了""不说了"等）
    deflect = sum(1 for m in messages
                  if any(w in m for w in ["随便", "算了", "不说了", "你管我", "不知道", "无所谓"]))
    fp.uses_deflection = deflect / total


def _extract_factory_traits(fp: StyleFingerprint, factory: dict):
    """从factory_archive提取风格描述"""
    comm = factory.get("communication_style", {})
    fp.core_tone = comm.get("tone", "直接、略带防御但温柔")
    fp.catchphrases = comm.get("catchphrases", [])
    fp.communication_style = comm

    pers = factory.get("personality_traits", {})
    fp.personality_core = pers.get("core", "")


def _load_json(path: str) -> Optional[dict]:
    """加载JSON文件"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"[指纹] JSON加载失败 {path}: {e}")
        return None


# ============ 匹配度计算 ============

def match_score(fp: StyleFingerprint, new_messages: List[str]) -> Tuple[float, Dict[str, float]]:
    """
    计算新版回复 vs 出厂锚点的匹配度。

    Returns:
        (总分: 0-1, 各项得分明细)
    """
    if not new_messages:
        return 0.0, {}
    if not fp:
        return 0.5, {}  # 无基准时中庸分

    scores = {}

    # --- 1. 长度分布匹配度 (JS散度) ---
    new_dist = _compute_length_distribution(new_messages)
    scores["length_match"] = _distribution_similarity(fp.reply_length_distribution, new_dist)

    # --- 2. 长句惩罚 ---
    scores["long_sentence_penalty"] = _compute_long_penalty(new_messages, fp.max_reply_length)

    # --- 3. AI套话率 ---
    scores["cliche_rate"] = compute_cliche_rate(new_messages)

    # --- 4. AI味评分 ---
    scores["ai_language_score"] = compute_ai_language_score(new_messages)

    # --- 5. 语气匹配 ---
    scores["tone_match"] = _compute_tone_match(fp, new_messages)

    # --- 6. 傲娇特征 ---
    scores["tsundere_presence"] = _compute_tsundere_density(new_messages)

    # 加权总分
    from .config import OPTIMIZER_METRIC_WEIGHTS
    total_weight = 0
    weighted_sum = 0
    for metric, score in scores.items():
        w = OPTIMIZER_METRIC_WEIGHTS.get(metric, 1.0)
        weighted_sum += score * w
        total_weight += w

    overall = weighted_sum / total_weight if total_weight > 0 else 0.5
    return max(0.0, min(1.0, overall)), scores


def _compute_length_distribution(messages: List[str]) -> Dict[str, float]:
    """计算消息长度分布"""
    buckets = {"1-3": 0, "4-7": 0, "8-15": 0, "16-30": 0, "31+": 0}
    for m in messages:
        l = len(m)
        if l <= 3:
            buckets["1-3"] += 1
        elif l <= 7:
            buckets["4-7"] += 1
        elif l <= 15:
            buckets["8-15"] += 1
        elif l <= 30:
            buckets["16-30"] += 1
        else:
            buckets["31+"] += 1
    total = len(messages)
    return {k: v / total for k, v in buckets.items()}


def _distribution_similarity(dist_a: Dict[str, float], dist_b: Dict[str, float]) -> float:
    """
    计算两个长度分布的相似度。使用1 - (Hellinger距离近似)。
    返回0(完全不同)到1(完全相同)。
    """
    keys = set(list(dist_a.keys()) + list(dist_b.keys()))
    diff_sum = 0
    for k in keys:
        a = dist_a.get(k, 0)
        b = dist_b.get(k, 0)
        diff_sum += (math.sqrt(a) - math.sqrt(b)) ** 2
    hellinger = math.sqrt(diff_sum) / math.sqrt(2)
    return 1.0 - hellinger


def _compute_long_penalty(messages: List[str], max_baseline: int) -> float:
    """
    计算长句惩罚。真实冬最长34字，从未超50字。
    >50字=严重扣分(直接回滚级别), >30字=轻微扣分。
    得分范围0(完美无长句)到-1(极度惩罚)。
    """
    if not messages:
        return 1.0
    long_count = sum(1 for m in messages if len(m) > 30)
    very_long_count = sum(1 for m in messages if len(m) > 50)

    # >50字致命扣分
    if very_long_count > 0:
        return 0.0  # 严重扣分（决策层会直接回滚）

    long_ratio = long_count / len(messages)
    # 出厂基线0.2% (>30字), 超出越多分数越低
    if long_ratio <= 0.002:
        return 1.0
    elif long_ratio <= 0.01:
        return 0.8
    elif long_ratio <= 0.05:
        return 0.5
    else:
        return 0.2


def _compute_tone_match(fp: StyleFingerprint, messages: List[str]) -> float:
    """计算语气匹配：新版是否保持出厂口癖和句式风格"""
    if not messages:
        return 0.0

    total = len(messages)
    catchphrase_hits = 0
    tone_signal = 0

    # 口癖命中
    for m in messages:
        for phrase in fp.catchphrases:
            if phrase in m:
                catchphrase_hits += 1
                break

    # 语气信号：反问、否定、防御
    for m in messages:
        if "？" in m or "?" in m:
            tone_signal += 0.5  # 反问/疑问
        if any(w in m for w in ["不", "没", "才不", "哪有", "谁知道"]):
            tone_signal += 0.5  # 否定/防御

    phrase_score = min(1.0, catchphrase_hits / max(total * 0.1, 1))
    tone_score = min(1.0, tone_signal / max(total * 0.5, 1))
    return phrase_score * 0.4 + tone_score * 0.6


# ============ 独立评估函数 ============

AI_CLICHES = [
    "很高兴", "为您服务", "当然可以", "没问题", "让我来帮你",
    "很高兴见到你", "我很乐意", "请问有什么", "根据我的理解",
    "首先", "其次", "最后", "综上所述", "总的来说",
    "不仅可以", "而且", "同时也要注意", "需要注意的是",
    "建议您", "您可以尝试", "祝您", "希望这能帮到",
    "不用客气", "欢迎随时", "如果你有任何问题",
    "作为一个AI", "我是AI", "帮你分析", "给你一些建议",
    "听起来很不错", "确实是这样的", "我完全理解",
]

AI_PATTERNS = [
    (r"(首先|其次|最后|第一|第二|第三).*?(其次|最后|另外)", 3, "结构化列举"),
    (r"当然.*?我很乐意", 3, "过度热情"),
    (r"让我.*?帮你", 3, "客服语气"),
    (r"根据.*?理解", 3, "AI分析语气"),
    (r"希望.*?能.*?(帮助|帮到)", 2, "服务性结尾"),
    (r"(可以|能够).*?(帮助|协助)", 2, "能力展示"),
]

TS_UNDERE_PATTERNS = {
    "denial": re.compile(r"才没有|不是|没有啦|哪有|我才没"),
    "deflection": re.compile(r"随便你|那咋了|不说了|算了|不儿"),
    "reluctant_affection": re.compile(r"哼|少来|你管我|不用你|谁要你|别管我"),
    "hidden_care": re.compile(r"你还知道|终于|你还活着|知道来"),
    "tough_love": re.compile(r"你走|别理我|烦|滚|讨厌"),
    "struggle_voice": re.compile(r"不知道|嗯|没啥|就那样|还好吧|一般"),
}


def compute_cliche_rate(messages: List[str]) -> float:
    """
    计算AI套话率。出厂基线: 0（真人不说套话）。
    返回0(无套话)到1(全是套话)。
    """
    if not messages:
        return 0.0
    hit = sum(1 for m in messages for c in AI_CLICHES if c in m)
    return min(1.0, hit / len(messages))


def compute_ai_language_score(messages: List[str]) -> float:
    """
    AI味评分。检测过度礼貌、客服语气、模板化结构。
    返回0(像真人)到1(非常AI)。
    """
    if not messages:
        return 0.0
    total_score = 0
    for m in messages:
        msg_score = 0
        for pattern, weight, _ in AI_PATTERNS:
            if re.search(pattern, m):
                msg_score += weight
        # 累计扣分
        if len(m) > 50:  # 长文→AI味
            msg_score += 2
        if m.count("！") + m.count("!") > 3:  # 过多感叹号
            msg_score += 1
        if "~" in m and len(m) > 20:  # 长句+波浪线=过度可爱
            msg_score += 1
        total_score += msg_score
    return min(1.0, total_score / max(len(messages) * 3, 1))


def _compute_tsundere_density(messages: List[str]) -> float:
    """
    计算傲娇特征密度。
    返回0(无傲娇特征)到1(非常傲娇)。
    出厂基线约为0.2-0.3（不是每条消息都傲娇，但整体有特征）。
    """
    if not messages:
        return 0.0
    total_hits = 0
    for m in messages:
        for pattern_name, pattern in TS_UNDERE_PATTERNS.items():
            if pattern.search(m):
                total_hits += 0.5  # 每个模式类型0.5分
    # 归一化：理想情况每条消息1-2个模式命中
    return min(1.0, total_hits / max(len(messages), 1))


# ============ 持久化 ============

def load_fingerprint(path: Optional[str] = None) -> Optional[StyleFingerprint]:
    """从JSON加载风格指纹。如果文件不存在，返回None。"""
    from .config import STYLE_FINGERPRINT_PATH
    filepath = path or STYLE_FINGERPRINT_PATH
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return fingerprint_from_dict(data)
    except Exception as e:
        log(f"[指纹] 加载失败: {e}")
        return None


def save_fingerprint(fp: StyleFingerprint, path: Optional[str] = None) -> str:
    """保存风格指纹到JSON。返回路径。"""
    from .config import STYLE_FINGERPRINT_PATH
    filepath = path or STYLE_FINGERPRINT_PATH
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(fingerprint_to_dict(fp), f, ensure_ascii=False, indent=2)
    log(f"[指纹] 已保存到 {filepath}")
    return filepath


def fingerprint_to_dict(fp: StyleFingerprint) -> dict:
    """序列化为字典"""
    return {
        "avg_reply_length": fp.avg_reply_length,
        "median_reply_length": fp.median_reply_length,
        "max_reply_length": fp.max_reply_length,
        "reply_length_distribution": fp.reply_length_distribution,
        "single_char_reply_rate": fp.single_char_reply_rate,
        "short_reply_rate": fp.short_reply_rate,
        "long_reply_rate": fp.long_reply_rate,
        "total_messages": fp.total_messages,
        "common_single_chars": fp.common_single_chars,
        "frequent_phrases": fp.frequent_phrases,
        "uses_question_back": fp.uses_question_back,
        "uses_negation": fp.uses_negation,
        "uses_deflection": fp.uses_deflection,
        "core_tone": fp.core_tone,
        "catchphrases": fp.catchphrases,
        "personality_core": fp.personality_core,
        "communication_style": fp.communication_style,
        "generated_at": fp.generated_at,
        "source": fp.source,
    }


def fingerprint_from_dict(d: dict) -> StyleFingerprint:
    """从字典反序列化"""
    return StyleFingerprint(
        avg_reply_length=d.get("avg_reply_length", 6.9),
        median_reply_length=d.get("median_reply_length", 6),
        max_reply_length=d.get("max_reply_length", 34),
        reply_length_distribution=d.get("reply_length_distribution", {}),
        single_char_reply_rate=d.get("single_char_reply_rate", 0.047),
        short_reply_rate=d.get("short_reply_rate", 0.657),
        long_reply_rate=d.get("long_reply_rate", 0.002),
        total_messages=d.get("total_messages", 2331),
        common_single_chars=d.get("common_single_chars", {}),
        frequent_phrases=d.get("frequent_phrases", {}),
        uses_question_back=d.get("uses_question_back", 0.0),
        uses_negation=d.get("uses_negation", 0.0),
        uses_deflection=d.get("uses_deflection", 0.0),
        core_tone=d.get("core_tone", ""),
        catchphrases=d.get("catchphrases", []),
        personality_core=d.get("personality_core", ""),
        communication_style=d.get("communication_style", {}),
        generated_at=d.get("generated_at", ""),
        source=d.get("source", ""),
    )


# ============ CLI入口（一次性初始化） ============

if __name__ == "__main__":
    import sys

    from .config import FACTORY_ARCHIVE_PATH, FACTORY_CSV_PATH, STYLE_FINGERPRINT_PATH

    if len(sys.argv) > 1 and sys.argv[1] == "--init":
        print("正在从 CSV + factory_archive 生成风格指纹...")
        csv_path = sys.argv[2] if len(sys.argv) > 2 else FACTORY_CSV_PATH
        factory_path = sys.argv[3] if len(sys.argv) > 3 else FACTORY_ARCHIVE_PATH
        fp = compute_fingerprint(csv_path, factory_path)
        save_fingerprint(fp)
        print(f"\n✓ 指纹已生成:")
        print(f"  样本数: {fp.total_messages}")
        print(f"  平均长度: {fp.avg_reply_length:.1f}字")
        print(f"  短句率(≤7字): {fp.short_reply_rate:.1%}")
        print(f"  最长回复: {fp.max_reply_length}字")
        print(f"  口癖数量: {len(fp.catchphrases)}")
        print(f"  高频短语: {len(fp.frequent_phrases)}个")
        print(f"  常见单字回复: {list(fp.common_single_chars.keys())[:10]}")
    else:
        # 验证已存在的指纹
        fp = load_fingerprint()
        if fp:
            print(f"已加载风格指纹 (生成于 {fp.generated_at})")
            print(f"  样本数: {fp.total_messages}, 平均长度: {fp.avg_reply_length:.1f}字")
        else:
            print("未找到风格指纹，请先运行: python -m dong.style_fingerprint --init")
