"""
冬 · 人设与语言指纹
- 人设配置加载
- 称呼摇摆
- 真错别字 / 卖萌口误 / 输入法官留
- 标点乱打
- 撒娇语追加
- 表情替换 / emoji处理

重构优化：语言规则配置外部化到数据类
"""
import json
import os
import re
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Tuple, Optional

from .config import PERSONA_FILE
from .log import log


# ============ 语言规则配置类 ============
@dataclass
class LanguageRules:
    """语言风格规则容器"""
    typo_hand: Dict[str, Tuple[str, float]] = field(default_factory=dict)
    typo_cute: Dict[str, str] = field(default_factory=dict)
    call_weights: List[Tuple[str, float]] = field(default_factory=list)
    love_hate_suffixes: List[str] = field(default_factory=list)
    probing_suffixes: List[str] = field(default_factory=list)
    dependence_suffixes: List[str] = field(default_factory=list)
    emotion_map: Dict[str, List[str]] = field(default_factory=dict)
    
    @classmethod
    def from_defaults(cls) -> "LanguageRules":
        """从默认配置创建规则集"""
        return cls(
            typo_hand={
                "好听": ("豪庭", 0.03), "舒服": ("书服", 0.03), "知道了": ("知道勒", 0.03),
                "是的": ("是滴", 0.03), "没有": ("没又", 0.03), "怎么了": ("怎么乐", 0.03),
                "可以": ("可一", 0.03), "回来了": ("回来乐", 0.03), "好的": ("好滴", 0.03),
                "就这样": ("就着样", 0.03), "那好吧": ("那好把", 0.03),
            },
            typo_cute={
                "不要": "补药", "对不起": "斯密马赛", "难受": "难受香菇",
                "没有": "木有", "不行": "8行", "晚安": "晚安安",
                "求你了": "求求你啦", "讨厌": "讨腌", "知道啦": "知道惹",
                "坏蛋": "坏坏", "受不了": "俺不中嘞", "怎么办": "咋整",
                "太好了": "巴适",
},
            call_weights=[
                ("小哥", 0.55),
                ("", 0.33), ("快乐小狗", 0.08), ("徐娇娇", 0.04),
            ],
            love_hate_suffixes=[
                "我恨你", "讨厌", "哼", "不理你了", "你给我等着",
                "我不管", "哼！", "讨厌死了", "你完蛋了你",
            ],
            probing_suffixes=[
                "你懂吗", "你对吗", "你是不是...算了当我没说",
                "你是不是有毛病", "你正常一点", "你不要这样", "你干嘛对我这么好",
            ],
            dependence_suffixes=[
                "我想听你说话", "能听你说吗", "你怎么不说话", "你在干嘛呢",
            ],
            emotion_map={
                "happy": ["哈哈", "嘿嘿", "开心", "真好", "太好", "棒", "高兴", "笑"],
                "shy": ["害羞", "不好意思", "别说了", "讨厌", "不要说"],
                "angry": ["生气", "烦", "滚", "无语", "够了", "别烦"],
                "upset": ["难受", "伤心", "不开心", "不好", "唉"],
                "cute": ["撒娇", "喵", "呜呜", "贴贴", "蹭蹭"],
                "love": ["喜欢", "想你了", "爱你", "亲亲", "抱抱"],
                "speechless": ["...", "……", "无语", "栓q", "服了"],
                "tsundere": ["哼", "才不是", "随便", "谁要", "不管你", "你管我", "谁稀罕", "爱咋咋地"],
                "sleepy": ["困", "睡", "晚安", "早点休息"],
                "laugh": ["笑死", "绷不住", "草", "哈哈哈"],
                "bye": ["晚安", "拜拜", "再见", "睡了"],
                "eyeroll": ["白眼", "离谱", "麻了"],
            },
        )


# 全局语言规则实例
_language_rules: Optional[LanguageRules] = None


def _get_language_rules() -> LanguageRules:
    """获取语言规则（延迟初始化）"""
    global _language_rules
    if _language_rules is None:
        _language_rules = LanguageRules.from_defaults()
    return _language_rules


# ============ PERSONA 分层 ============
def load_persona_config(filepath=None):
    """从外部配置文件加载人设。
    格式: ===SECTION=== 后跟内容，支持 BASE / 159 / OTHERS 三个section。
    """
    filepath = filepath or PERSONA_FILE
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        log(f"人设配置加载失败: {e}，使用内置降级配置")
        return _fallback_persona()

    sections = {}
    current_section = None
    current_lines = []

    for line in content.split('\n'):
        stripped = line.strip()
        if stripped.startswith('===') and stripped.endswith('==='):
            if current_section and current_lines:
                sections[current_section.strip('=').strip().upper()] = '\n'.join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)
    if current_section and current_lines:
        sections[current_section.strip('=').strip().upper()] = '\n'.join(current_lines).strip()

    base = sections.get("BASE", "")
    persona_159 = base + "\n\n" + sections.get("159", "")
    persona_others = base + "\n\n" + sections.get("OTHERS", "")

    return {"base": base, "159": persona_159, "others": persona_others}


def _fallback_persona():
    """降级人设：配置文件缺失时使用"""
    base = "你是冬，QQ昵称无尽夏，在呼市上大学，大四。用QQ和人聊天。性格嘴硬心软，有点丧但可爱。"
    p159 = base + "\n现在和你聊天的是你很在意的人，你对他有暧昧好感但嘴硬不承认。"
    poth = base + "\n现在和你聊天的是普通网友，回复简短礼貌，保持距离。"
    return {"base": base, "159": p159, "others": poth}


_persona_cache = None


def get_persona(uid):
    """根据用户ID返回对应的系统人设"""
    global _persona_cache
    if _persona_cache is None:
        _persona_cache = load_persona_config()
    if uid == 1592741204:
        return _persona_cache["159"]
    return _persona_cache["others"]


def _get_personas():
    global _persona_cache
    if _persona_cache is None:
        _persona_cache = load_persona_config()
    return _persona_cache


# ============ 表情识别 ============
FACE_MAP = {
    344: "委屈的表情", 138: "偷笑的表情", 69: "呲牙笑的表情",
    178: "微笑", 104: "可怜巴巴的表情", 167: "坏笑",
    318: "撇嘴", 279: "得意的表情", 280: "害羞的表情",
    174: "尴尬的表情", 189: "困了的表情", 195: "疑问的表情",
    316: "叹气", 282: "委屈巴巴", 75: "大拇指",
    277: "鄙视", 263: "鼓掌", 285: "惊讶", 282: "流汗",
}


def replace_cq_faces(text):
    """替换CQ码中的表情为文字描述"""
    pattern = r'\[CQ:face,id=(\d+),.*?\]'
    def replacer(match):
        face_id = int(match.group(1))
        return FACE_MAP.get(face_id, f"表情{face_id}")
    return re.sub(pattern, replacer, text)


# ============ Emoji / Nickname 清理 ============
def strip_emoji(text):
    """去除emoji字符，用于聊天记录保存"""
    result = []
    for ch in text:
        cp = ord(ch)
        if 0x1F600 <= cp <= 0x1FAFF:
            continue
        if 0x1F000 <= cp <= 0x1F02F:
            continue
        if 0x1F0A0 <= cp <= 0x1F0FF:
            continue
        if 0x1F780 <= cp <= 0x1F8FF:
            continue
        if 0x2600 <= cp <= 0x27BF:
            continue
        if 0x2300 <= cp <= 0x23FF:
            continue
        if 0xFE00 <= cp <= 0xFE0F or cp == 0x200D or cp == 0x20E3:
            continue
        if 0xD800 <= cp <= 0xDFFF:
            continue
        result.append(ch)
    return ''.join(result)


def strip_nicknames(uid, text):
    """清理对外人（非159）的昵称"""
    if uid == 1592741204:
        return text
    text = text.replace('徐松', '').replace('小哥', '').replace('外币', '')
    text = re.sub(r'^哥\s+', '', text)
    text = re.sub(r'([，。！？,!.?\n])哥\s+', r'\1', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


# ============ A. 真错别字 ============
def apply_typo_hand(rep):
    rules = _get_language_rules()
    for correct, (wrong, prob) in rules.typo_hand.items():
        if correct in rep and random.random() < prob:
            rep = rep.replace(correct, wrong, 1)
            return rep, True
    return rep, False


# ============ B. 卖萌口误 ============
def apply_typo_cute(rep):
    """卖萌口误 10%"""
    rules = _get_language_rules()
    for correct, wrong in rules.typo_cute.items():
        if correct in rep and random.random() < 0.10:
            rep = rep.replace(correct, wrong, 1)
            return rep, True
    return rep, False


# ============ C. 输入法官留 ============
def apply_input_mistakes(rep):
    """8%概率触发输入法官留"""
    if random.random() > 0.08:
        return rep, None
    mistake_type = random.choice(["typo", "extra"])
    if mistake_type == "typo":
        chars = list(rep)
        if len(chars) > 3:
            idx = random.randint(0, len(chars) - 2)
            chars[idx] = random.choice(["的", "了", "啊", "呢", "呀"])
            rep = "".join(chars) + " 打错了"
    elif mistake_type == "extra":
        rep = rep + " haode"
    return rep, mistake_type


# ============ 称呼摇摆 ============
def _is_serious_context(rep):
    """检测回复是否严肃语境——只有这时才用真名'徐松'"""
    serious_markers = [
        "认真", "说真的", "正经", "我没开玩笑", "别闹",
        "你给我听好", "听我说", "你听好", "说正事",
        "你过来", "过来谈", "坦白", "老实说",
        "我很认真", "不是在玩", "我问真的",
    ]
    return any(m in rep for m in serious_markers)

def apply_nickname(uid, rep):
    if uid != 1592741204:
        return rep
    if random.random() > 0.30:
        return rep
    rules = _get_language_rules()
    # 严肃语境：40%概率用真名"徐松"
    if _is_serious_context(rep) and random.random() < 0.40:
        rep = "徐松 " + rep
        return rep
    r = random.random()
    cumulative = 0
    nickname = ""
    for name, weight in rules.call_weights:
        cumulative += weight
        if r < cumulative:
            nickname = name
            break
    if nickname:
        rep = nickname + " " + rep
    return rep


# ============ 标点乱打 ============
def punctuation_chaos(rep):
    if random.random() > 0.05:
        return rep
    if '!' in rep or '！' in rep:
        if random.random() < 0.5:
            rep = rep.replace('!', '1', 1) if '!' in rep else rep.replace('！', '1', 1)
    if '…' in rep:
        rep = rep.replace('…', '。。。', 1)
    elif '...' in rep:
        rep = rep.replace('...', '。。。', 1)
    if ('?' in rep or '？' in rep) and random.random() < 0.2:
        rep = rep.replace('?', '', 1) if '?' in rep else rep.replace('？', '', 1)
    return rep


# ============ 撒娇语追加 ============
def add_love_hate_suffix(rep, user_text, is_late=False):
    """对回复追加撒娇语"""
    rules = _get_language_rules()
    was_cared = any(kw in user_text.lower() for kw in [
        "睡吧", "早点", "注意", "好点了", "好好", "乖", "心疼", "担心"
    ])
    is_probing = any(kw in user_text for kw in ["你是不是", "你懂吗", "真的吗", "对吗", "你在干嘛"])
    is_missing = any(kw in user_text.lower() for kw in ["你在哪", "人呢", "怎么不说话", "不理我"])

    base_prob = 0.15
    if is_late:
        base_prob = 0.25
    if was_cared:
        base_prob = 0.30
    if is_probing:
        base_prob = 0.35
    if is_missing:
        base_prob = 0.40

    if random.random() < 0.05:
        rep = rep + " " + random.choice(rules.probing_suffixes)
        return rep
    if is_late and random.random() < 0.05:
        rep = rep + " " + random.choice(rules.dependence_suffixes)
        return rep
    if random.random() < base_prob:
        suffix = random.choice(rules.love_hate_suffixes)
        if "我恨你" not in rep and "讨厌" not in rep and "哼" not in rep:
            rep = rep + " " + suffix
    return rep


# ============ #16 语言风格渐进演化 ============
# 记录对方常用词，长期对话中冬会无意识吸收
_user_lang_features = {}  # {uid: {"words": {词: 频率}, "phrases": [...]}}
LANG_ABSORB_FILE = os.path.join(os.path.dirname(__file__), "dong_lang_features.json")


def _load_lang_features():
    global _user_lang_features
    if _user_lang_features:
        return
    try:
        if os.path.exists(LANG_ABSORB_FILE):
            with open(LANG_ABSORB_FILE, "r", encoding="utf-8") as f:
                _user_lang_features = json.load(f)
    except Exception:
        _user_lang_features = {}


def _save_lang_features():
    try:
        with open(LANG_ABSORB_FILE, "w", encoding="utf-8") as f:
            json.dump(_user_lang_features, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def absorb_user_language(uid, text):
    """从用户消息中提取语言特征"""
    if not text or len(text) < 3:
        return

    _load_lang_features()
    uid_str = str(uid)
    if uid_str not in _user_lang_features:
        _user_lang_features[uid_str] = {"words": {}, "phrases": []}

    # 提取2-5字的口癖短语
    import re as _re
    cleaned = _re.sub(r'[^\u4e00-\u9fff\w]', '', text)
    for i in range(len(cleaned) - 1):
        for l in [2, 3, 4]:
            if i + l <= len(cleaned):
                phrase = cleaned[i:i+l]
                if phrase not in _user_lang_features[uid_str]["words"]:
                    _user_lang_features[uid_str]["words"][phrase] = 0
                _user_lang_features[uid_str]["words"][phrase] += 1

    # 只保留出现3次以上的词
    _user_lang_features[uid_str]["words"] = {
        k: v for k, v in _user_lang_features[uid_str]["words"].items() if v >= 3
    }
    # 最多保留30个
    _user_lang_features[uid_str]["words"] = dict(
        sorted(_user_lang_features[uid_str]["words"].items(), key=lambda x: -x[1])[:30]
    )
    _save_lang_features()


def maybe_absorb_word(uid, rep):
    """3%概率在回复中使用对方的习惯用语"""
    if random.random() > 0.03:
        return rep, False

    _load_lang_features()
    uid_str = str(uid)
    if uid_str not in _user_lang_features:
        return rep, False

    words = _user_lang_features[uid_str].get("words", {})
    if not words:
        return rep, False

    # 选一个高频词
    candidates = [(w, f) for w, f in words.items() if f >= 5 and len(w) <= 4 and w not in rep]
    if not candidates:
        return rep, False

    word, freq = random.choice(candidates)
    log(f"  语言吸收: 用了\"{word}\"(对方用了{freq}次)")
    return rep, word  # 返回吸收的词，让process_reply决定怎么加
