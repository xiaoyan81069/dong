"""青蛙桌宠 · 陪伴模式配置"""
import os

# ── 路径 ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(BASE_DIR, "companion_memory.json")

# ── 加载 dong 的 .env（和主项目共享 API 密钥）──
_ENV_PATH = os.path.join(os.path.dirname(BASE_DIR), "dong", ".env")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _val = _line.partition("=")
            _key, _val = _key.strip(), _val.strip().strip('"').strip("'")
            if _key and _val and _key not in os.environ:
                os.environ[_key] = _val

# ── 陪伴 API ── (LongCat Omni 多模态)
VISION_API_KEY = os.environ.get("DONG_API_KEY_LONGCAT", "")
VISION_API_BASE = "https://api.longcat.chat/openai"
VISION_MODEL = "LongCat-Flash-Omni-2603"

# ── 语音 TTS ── (edge-tts, 免费免Key)
# 可选: zh-CN-XiaoxiaoNeural(成熟女声) zh-CN-XiaoyiNeural(年轻女声)
#       zh-CN-YunxiNeural(热门短视频男声) zh-CN-YunjianNeural(大型纪录片男声)
TTS_VOICE = "zh-CN-XiaoxiaoNeural"

# ── 时间常数 ──
SCREEN_INTERVAL = 30       # 截屏间隔(秒)
BUBBLE_DURATION = 6000     # 气泡默认显示(毫秒), 实际按文本长度算
LONG_PRESS_MS = 1000       # 长按触发(毫秒)
DRAG_THRESHOLD = 5         # 拖拽阈值(像素)
