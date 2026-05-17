"""
冬 · 配置模块
- API密钥、模型路由、切换/熔断
- 所有文件路径
- 全局常量

重构优化：使用数据类组织配置结构
"""
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any

from .log import log
from .core.config_naming import registry as _provider_registry

# ============ .env 文件加载（纯标准库，无需 python-dotenv） ============
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            # 移除行内注释（# 及其后内容），但保留值中的 #
            _val_part = _line.split("=", 1)[1]
            _comment_idx = _val_part.find(" #")
            if _comment_idx >= 0:
                _line = _line.split("=", 1)[0] + "=" + _val_part[:_comment_idx]
            _key, _, _val = _line.partition("=")
            _key, _val = _key.strip(), _val.strip().strip('"').strip("'")
            if _key and _val and _key not in os.environ:
                os.environ[_key] = _val


# ============ 数据类配置 ============
@dataclass
class PathConfig:
    """路径配置"""
    base_dir: str
    log_file: str
    memory_file: str
    status_file: str
    last_day_file: str
    cycle_file: str
    chat_history_file: str
    persona_file: str
    media_dir: str
    image_dir: str
    audio_received_dir: str
    audio_generated_dir: str
    image_index_file: str
    emoji_dir: str
    emoji_index_file: str
    cloned_voice_path: str
    intimacy_file: str


@dataclass
class APIConfig:
    """单个API配置"""
    name: str
    model: str
    api_key: str
    api_base: str
    task: str
    max_failures: int


@dataclass
class ExternalFollow:
    """关注的外部对象"""
    name: str
    type: str
    check_url: str
    keywords: List[str]


@dataclass
class IntimacyLevel:
    """亲密等级"""
    level: int
    name: str


@dataclass
class IntimacyHardBoundary:
    """硬限制行为"""
    name: str
    start_hour: Optional[int]
    end_hour: Optional[int]


@dataclass
class ConfigState:
    """运行时配置状态"""
    current_chat: int = 1  # doubao 429中，从备用启动
    consecutive_failures: int = 0
    cooldown_until: Optional[datetime] = None


# ============ 路径配置实例 ============
_DONG_DIR = os.path.dirname(os.path.abspath(__file__))
PATH = PathConfig(
    base_dir=_DONG_DIR,
    log_file=os.path.join(_DONG_DIR, "dong_master.log"),
    memory_file=os.path.join(_DONG_DIR, "dong_memory.json"),
    status_file=os.path.join(_DONG_DIR, "dong_status.json"),
    last_day_file=os.path.join(_DONG_DIR, "dong_lastday.json"),
    cycle_file=os.path.join(_DONG_DIR, "dong_cycle.json"),
    chat_history_file=os.path.join(_DONG_DIR, "chat_history.txt"),
    persona_file=os.path.join(_DONG_DIR, "characters", "dong.persona.txt"),
    media_dir=os.path.join(_DONG_DIR, "data", "media"),
    image_dir=os.path.join(_DONG_DIR, "data", "media", "images", "received"),
    audio_received_dir=os.path.join(_DONG_DIR, "data", "media", "audio", "received"),
    audio_generated_dir=os.path.join(_DONG_DIR, "data", "media", "audio", "generated"),
    image_index_file=os.path.join(_DONG_DIR, "data", "media", "image_index.json"),
    emoji_dir=os.path.join(os.path.expanduser("~"), "Downloads", "emojis"),
    emoji_index_file=os.path.join(os.path.expanduser("~"), "Downloads", "emojis", "emoji_index.json"),
    cloned_voice_path=os.path.join(os.path.expanduser("~"), "Downloads", "voices", "voice_wxid_f22pwzjfe8lg22_5002_1731325618_6001701757487865039.wav"),
    intimacy_file=os.path.join(_DONG_DIR, "dong_intimacy.json"),
)

# 向后兼容的路径常量
BASE_DIR = PATH.base_dir
LOG_FILE = PATH.log_file
MEMORY_FILE = PATH.memory_file
STATUS_FILE = PATH.status_file
LAST_DAY_FILE = PATH.last_day_file
CYCLE_FILE = PATH.cycle_file
CHAT_HISTORY_FILE = PATH.chat_history_file
PERSONA_FILE = PATH.persona_file
MEDIA_DIR = PATH.media_dir
IMAGE_DIR = PATH.image_dir
AUDIO_RECEIVED_DIR = PATH.audio_received_dir
AUDIO_GENERATED_DIR = PATH.audio_generated_dir
IMAGE_INDEX_FILE = PATH.image_index_file
EMOJI_DIR = PATH.emoji_dir
EMOJI_INDEX_FILE = PATH.emoji_index_file
CLONED_VOICE_PATH = PATH.cloned_voice_path
INTIMACY_FILE = PATH.intimacy_file


# ============ OneBot / NapCat ============
SENDBOT_API = os.environ.get("SENDBOT_API", "http://127.0.0.1:3000")
NAPCAT_DIR = os.environ.get("NAPCAT_DIR", "")


# ============ 用户白名单 ============
ALLOWED_USERS = [int(u.strip()) for u in os.environ.get("DONG_ALLOWED_USERS", "").split(",") if u.strip()]
MASTER_UID = int(os.environ.get("DONG_MASTER_UID", "0"))
NAPCAT_QQ = os.environ.get("NAPCAT_QQ", "")
AUTO_TOOL_MODE = False  # Cherry全自动模式：主人聊天中自动注入工具，启用改True

# ============ 启动时API Key校验 ============
def _check_api_keys():
    required = {"DONG_API_KEY_ARK": "主力chat", "DONG_API_KEY_LONGCAT": "备用chat/分析"}
    missing = [f"{name}({desc})" for name, desc in required.items() if not os.environ.get(name)]
    if missing:
        log(f"WARNING: 以下API Key未配置，对应Provider将不可用: {', '.join(missing)}")

_check_api_keys()

# ============ 多API配置 + 模型路由 ============
API_CONFIGS: List[APIConfig] = [
    APIConfig(  # 配置1 主力聊天
        name="主力",
        model="doubao-seed-character-251128",
        api_key=os.environ.get("DONG_API_KEY_ARK", ""),
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        task="chat",
        max_failures=3,
    ),
    APIConfig(  # 配置2 备用聊天
        name="备用",
        model="longcat-flash-chat",
        api_key=os.environ.get("DONG_API_KEY_LONGCAT", ""),
        api_base="https://api.longcat.chat/openai",
        task="chat",
        max_failures=3,
    ),
    APIConfig(  # 配置3 分析任务 — LongCat-Flash-Lite 50M/天 不抢主力配额
        name="分析",
        model="LongCat-Flash-Lite",
        api_key=os.environ.get("DONG_API_KEY_LONGCAT", ""),
        api_base="https://api.longcat.chat/openai",
        task="analysis",
        max_failures=2,
    ),
    APIConfig(  # 配置4 多模态 — 千问Qwen3.5-Omni-Flash 视觉+JSON Mode
        name="多模态",
        model="qwen3.5-omni-flash",
        api_key=os.environ.get("DONG_API_KEY_QWEN", ""),
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        task="vision",
        max_failures=2,
    ),
]

# deprecated: 旧版按索引路由，新代码应走 _provider_registry + api_gateway
_api_state = ConfigState()
_api_migrated = False


def migrate_to_registry():
    """将旧的 API_CONFIGS 索引列表迁移到 ConfigRegistry（启动时调用一次）"""
    global _api_migrated
    if _api_migrated:
        return
    _api_migrated = True
    registered = _provider_registry.migrate_from_indexed(API_CONFIGS)
    log(f"API配置迁移到Registry: {', '.join(registered)}")


def _chat_configs() -> List[int]:
    """返回所有chat任务配置的索引列表"""
    return [i for i, c in enumerate(API_CONFIGS) if c.task == "chat"]


def _switch_api() -> bool:
    """切换到下一个聊天配置，返回True=切换成功 False=全部不可用进入冷却"""
    chats = _chat_configs()
    cur = _api_state.current_chat
    try:
        pos = chats.index(cur)
    except ValueError:
        pos = -1
    if pos + 1 < len(chats):
        _api_state.current_chat = chats[pos + 1]
        cfg = API_CONFIGS[chats[pos + 1]]
        log(f"API切换: → {cfg.name}({cfg.model})")
        _api_state.consecutive_failures = 0
        return True
    else:
        _api_state.current_chat = chats[0]
        _api_state.cooldown_until = datetime.now() + timedelta(minutes=30)
        log("API全部不可用，进入30分钟冷却期")
        return False


def _get_cfg(task: str = "chat") -> APIConfig:
    """获取当前任务对应的API配置——优先从 Registry 查询，fallback 旧索引"""
    if _api_migrated:
        pc = _provider_registry.get_primary(task)
        if pc:
            return APIConfig(
                name=pc.name, model=pc.model,
                api_key=pc.api_key, api_base=pc.api_base,
                task=task, max_failures=pc.max_failures,
            )
    # fallback：旧硬编码索引
    if task == "chat":
        return API_CONFIGS[_api_state.current_chat]
    elif task == "analysis":
        return API_CONFIGS[2]
    elif task == "vision":
        return API_CONFIGS[3]
    return API_CONFIGS[_api_state.current_chat]


# ============ 时间/关系常量 ============
FIRST_MET_DATE = "2024-09-01"
SEASONS_CN = {
    1: "深冬", 2: "早春", 3: "春天", 4: "春天", 5: "初夏", 6: "盛夏",
    7: "盛夏", 8: "夏末", 9: "初秋", 10: "深秋", 11: "初冬", 12: "深冬"
}

# ============ 主号城市配置（用于天气关心）============
MASTER_CITY = {"name": "你家那边", "lat": 30.57, "lon": 114.27}  # 默认武汉
MASTER_CITY_NAMES = {
    "武汉": (30.57, 114.27),
    "北京": (39.90, 116.40),
    "上海": (31.23, 121.47),
    "广州": (23.13, 113.26),
    "深圳": (22.54, 114.06),
    "成都": (30.57, 104.07),
    "杭州": (30.27, 120.15),
    "南京": (32.06, 118.80),
    "重庆": (29.56, 106.55),
    "长沙": (28.23, 112.94),
    "郑州": (34.76, 113.65),
    "西安": (34.26, 108.94),
}


# ============ 时间感知工具 ============
def describe_time_gap(minutes_ago: float) -> str:
    """将分钟数转为模糊时间描述。不精确到分，给大致感觉。"""
    if minutes_ago < 1:
        return "刚刚"
    elif minutes_ago < 5:
        return "几分钟前"
    elif minutes_ago < 15:
        return "十几分钟前"
    elif minutes_ago < 30:
        return "不到半小时"
    elif minutes_ago < 60:
        return "大概一个小时不到"
    elif minutes_ago < 120:
        return "差不多一两个小时"
    elif minutes_ago < 180:
        return "两三个小时吧"
    elif minutes_ago < 360:
        return "几个小时了"
    elif minutes_ago < 720:
        return "半天了"
    elif minutes_ago < 1440:
        return "快一天了"
    elif minutes_ago < 2880:
        return "一天多了"
    elif minutes_ago < 4320:
        return "两三天了"
    elif minutes_ago < 10080:
        return "好几天了"
    else:
        weeks = minutes_ago / 10080
        if weeks < 2:
            return "一个多星期了"
        elif weeks < 4:
            return "好几周了"
        else:
            return "挺久了"


def describe_time_of_day(hour: int = None) -> str:
    """将小时转为模糊时段描述。"""
    if hour is None:
        hour = datetime.now().hour
    if hour < 3:
        return "大半夜"
    elif hour < 5:
        return "凌晨"
    elif hour < 7:
        return "大清早"
    elif hour < 9:
        return "早上"
    elif hour < 11:
        return "上午"
    elif hour < 13:
        return "中午"
    elif hour < 17:
        return "下午"
    elif hour < 19:
        return "傍晚"
    elif hour < 22:
        return "晚上"
    elif hour < 23:
        return "深夜"
    else:
        return "大半夜"


def get_current_time_baseline() -> str:
    """获取当前时间基准描述：知道现在是几点但不用精确到分"""
    now = datetime.now()
    h, m = now.hour, now.minute
    rough_m = ""
    if m < 10:
        rough_m = ""
    elif m < 20:
        rough_m = "刚过一点"
    elif m < 40:
        rough_m = "半"
    elif m < 50:
        rough_m = "快"
    else:
        rough_m = "快"
        h = (h + 1) % 24
    if rough_m == "半":
        return f"大概{h}点半"
    elif rough_m:
        hour_str = f"快{h}点"
        return f"大概{hour_str}"
    else:
        hour_cn = "零点" if h == 0 else f"{h}点"
        return f"大概{hour_cn}左右"


# ============ 关注的外部对象 ============
FOLLOWED_EXTERNAL: List[ExternalFollow] = [
    ExternalFollow(
        name="许嵩",
        type="音乐人",
        check_url="https://music.163.com/artist/album?id=5771",
        keywords=["新歌", "专辑", "演唱会", "巡演"]
    ),
    ExternalFollow(
        name="第五人格",
        type="游戏",
        check_url="https://id5.163.com/news/",
        keywords=["更新", "新赛季", "新角色", "联动"]
    ),
]


# ============ 亲密关系系统配置 ============
INTIMACY_LEVELS: List[IntimacyLevel] = [
    IntimacyLevel(0, "陌生人"),
    IntimacyLevel(1, "认识的人"),
    IntimacyLevel(2, "普通朋友"),
    IntimacyLevel(3, "好朋友"),
    IntimacyLevel(4, "至交"),
]

INTIMACY_HARD_BOUNDARY: List[IntimacyHardBoundary] = [
    IntimacyHardBoundary("深夜深度交流", 22, 6),
    IntimacyHardBoundary("专属称呼", None, None),
    IntimacyHardBoundary("撒娇/嘴硬", None, None),
    IntimacyHardBoundary("表达好感", None, None),
    IntimacyHardBoundary("主动语音", None, None),
]

# ============ 记忆系统配置 ============
MEMORY_RETRIEVAL_COUNT = 5   # 每次检索注入的记忆条数
MEMORY_MAX_PER_USER = 50     # 触发记忆合并的阈值
MEMORY_SOFT_DELETE_DAYS = 30  # 软删除天数（importance<0.05且超此天数才真删）

# ============ 出厂记忆/增量蒸馏/记仇 路径配置 ============
FACTORY_ARCHIVE_FILE = os.path.join(BASE_DIR, "dong_factory_archive.json")
FACTORY_ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")
CHAT_SESSIONS_FILE = os.path.join(BASE_DIR, "chat_sessions.json")
DAILY_SUMMARY_FILE = os.path.join(BASE_DIR, "dong_daily_summary.json")
GRUDGE_FILE = os.path.join(BASE_DIR, "dong_grudges.json")
AMYGDALA_FILE = os.path.join(BASE_DIR, "dong_amygdala_memory.json")
FACTORY_HASHES_PATH = os.path.join(BASE_DIR, "dong_factory_hashes.json")

# ============ 人格化石文件路径清单 ============
FOSSIL_PATHS = {
    "persona": os.path.join(BASE_DIR, "characters", "dong.persona.txt"),
    "ex_skill": os.path.join(BASE_DIR, "characters", "dong.ex-skill.json"),
    "factory_archive": os.path.join(BASE_DIR, "dong_factory_archive.json"),
    "style_fingerprint": os.path.join(BASE_DIR, "dong_style_fingerprint.json"),
}

# ============ 出厂锚点硬约束 ============
FACTORY_ANCHOR = {
    "avg_reply_length": 6.9,     # 平均回复长度（字）
    "short_rate": 65.7,          # ≤7字比例（%）
    "max_single_reply": 34,      # 最长回复（字）
    "hard_max": 50,              # 绝对上限（字）— 超过直接回滚
}

# ============ 对话评估配置 ============
DIALOGUE_SEGMENTS_PATH = os.path.join(BASE_DIR, "dong_dialogue_segments.jsonl")


# ============ 通用工具函数 ============
def is_weekend(d: datetime = None) -> bool:
    d = d or datetime.now()
    return d.weekday() >= 5


def is_late_night() -> bool:
    hour = datetime.now().hour
    return hour >= 23 or hour < 6


def get_season() -> str:
    m = datetime.now().month
    return SEASONS_CN.get(m, "")


def get_days_since(date_str: str) -> Optional[int]:
    """计算从指定日期到今天的天数"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return (datetime.now() - d).days
    except Exception:
        return None


def get_api_stats() -> Dict[str, Any]:
    """获取API状态统计"""
    return {
        "current_chat": API_CONFIGS[_api_state.current_chat].name,
        "consecutive_failures": _api_state.consecutive_failures,
        "in_cooldown": _api_state.cooldown_until is not None and datetime.now() < _api_state.cooldown_until,
        "cooldown_remaining": (_api_state.cooldown_until - datetime.now()).total_seconds() if _api_state.cooldown_until and datetime.now() < _api_state.cooldown_until else 0,
    }


# ============ 优化代理配置 ============

OPTIMIZER_ENABLED = True                        # 是否启用每日自动优化
OPTIMIZER_TEST_GROUP_ID = 0                     # 测试用QQ群号（0=未配置）
OPTIMIZER_TEST_DURATION_MIN = 45                # 测试bot运行时长（分钟）
OPTIMIZER_MIN_INTERVAL_HOURS = 4               # 两次优化最小间隔（小时）
OPTIMIZER_BACKUP_KEEP_DAYS = 7                 # 备份保留天数
OPTIMIZER_ANALYSIS_SAMPLE_COUNT = 50           # 分析采样的回复数量

# 风格锚点数据源
FACTORY_CSV_PATH = os.path.join(BASE_DIR, "dong_factory_input.csv")  # 微信原始导出
FACTORY_ARCHIVE_PATH = os.path.join(BASE_DIR, "dong_factory_archive.json")
STYLE_FINGERPRINT_PATH = os.path.join(BASE_DIR, "dong_style_fingerprint.json")

# 优化日志与状态
OPTIMIZER_LOG_FILE = os.path.join(BASE_DIR, "dong_optimizer.log")
OPTIMIZER_STATE_FILE = os.path.join(BASE_DIR, "dong_optimizer_state.json")
BACKUPS_DIR = os.path.join(BASE_DIR, "dong_backups")

# 评估权重（用于决定上线/回滚，总和不重要，看比例）
OPTIMIZER_METRIC_WEIGHTS = {
    "length_match": 2.0,            # 长度分布匹配（高权重：出厂基线24%/42%/29%很明确）
    "long_sentence_penalty": 3.0,   # 长句惩罚（最高权重：真实冬从未超50字）
    "cliche_rate": 1.5,             # AI套话率
    "ai_language_score": 2.0,       # AI味评分
    "tone_match": 2.0,              # 语气匹配（反问/否定/防御风格）
    "tsundere_presence": 1.5,       # 傲娇特征
}

# 决策阈值：加权获胜比例超过此值则上线
OPTIMIZER_WIN_THRESHOLD = 0.4       # 因为long_sentence_penalty有否决权(>50字直接回滚)，阈值可适当降低
