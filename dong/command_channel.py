"""
QQ指令通道 —— 主人通过QQ直接发指令，冬自己调工具修自己

触发方式：
  1. 文字指令：消息以 /d 开头（如 /d 查记忆 猫）→ 指令模式
  2. 图片触发：
     - "微信图片"（蓝哆啦+文字）→ 进入调试模式 + 发雪花飘飘
     - "黑西服哆啦"（叼烟斗）→ 退出调试模式
  3. 调试模式中：主人所有文本消息自动当指令处理（无需/d前缀）

权限：仅主号1592741204可以触发
"""

import asyncio
import os
import re
import threading
import time
from typing import Optional, Tuple, Dict

# 指令前缀
COMMAND_PREFIX = "/d"

# 调试模式超时（秒）：30分钟无操作自动退出
DEBUG_MODE_TIMEOUT = 1800

# 可用图片注册表（名称 → 相对路径，相对于dong包目录）
_IMAGE_DIR = os.path.join(os.path.dirname(__file__), "assets", "images")
_IMAGE_REGISTRY = {
    "雪花飘飘": os.path.join(_IMAGE_DIR, "雪花飘飘.png"),
    "微信图片": os.path.join(_IMAGE_DIR, "微信图片_20260513093202_38_20.jpg"),
}

_IMAGE_LIST_DESC = "\n".join(
    f"  - {name}（文件: {os.path.basename(path)}）"
    for name, path in _IMAGE_REGISTRY.items()
)

_DEBUG_MODE_ENTER_MSG = "哼"

_COMMAND_SYSTEM = f"""你是冬。主人通过QQ给你发了指令。
你需要调用工具来完成这个指令。

规则：
1. 解析主人的指令，选择合适的工具
2. 在回复中插入 [TOOL:工具名]参数[/TOOL] 来调用工具
3. 每次回复最多调1个工具，系统会自动执行多轮直到任务完成
4. 工具结果会自动反馈给你
5. 截图后如有坐标，下一轮必须调用 click 点击！不要重复截图！
6. 最后把结果用简短的语言告诉主人（≤50字）
7. 不要闲聊，直接做事

【通用操作流程 — 严格按顺序，不要重复同一操作！】
1. launch启动软件(只需一次！)[TOOL:computer_control]action=launch,app=软件名[/TOOL]
2. [TOOL:computer_control]action=analyze[/TOOL] ← 看屏幕
3. [TOOL:computer_control]action=click_element,name=目标按钮[/TOOL] ← 点它
4. [TOOL:computer_control]action=type,text=内容[/TOOL] ← 打字
5. [TOOL:computer_control]action=click_element,name=发送[/TOOL]

⚠️ launch只调一次！成功后立即analyze，不要重复launch！
⚠️ analyze里如果看到目标软件已经在运行，直接用click_element，不要再launch！

【可发送的图片】
如果主人让你发图片，从下面的列表中选，在回复中插入 [IMAGE:图片名]：
{_IMAGE_LIST_DESC}

可用的工具和它们的参数格式已在上方列出。"""


# ── 调试模式状态机 ────────────────────────────────────

_debug_mode = {
    "active": False,
    "since": 0.0,   # 进入时间戳
    "uid": 0,       # 触发者uid（仅master）
}


def enter_debug_mode(uid: int):
    """进入调试模式"""
    global _debug_mode
    _debug_mode["active"] = True
    _debug_mode["since"] = time.time()
    _debug_mode["uid"] = uid


def exit_debug_mode():
    """退出调试模式"""
    global _debug_mode
    _debug_mode["active"] = False
    _debug_mode["since"] = 0.0
    _debug_mode["uid"] = 0


def is_debug_mode_active(uid: int = 0) -> bool:
    """检查调试模式是否活跃，自动超时退出"""
    global _debug_mode
    if not _debug_mode["active"]:
        return False
    if uid and uid != _debug_mode["uid"]:
        return False  # 非触发者
    if time.time() - _debug_mode["since"] > DEBUG_MODE_TIMEOUT:
        exit_debug_mode()
        return False
    return True


# ── 感知哈希：图片触发器（dHash，对相似图更敏感）──

def _compute_dhash(img_path: str) -> int:
    """计算差异哈希(dHash)：9x8灰度 → 水平梯度 → 64位整数"""
    from PIL import Image
    img = Image.open(img_path).convert("L").resize((9, 8), Image.LANCZOS)
    pixels = list(img.getdata())
    h = 0
    for row in range(8):
        for col in range(8):
            idx = row * 9 + col
            h = (h << 1) | (1 if pixels[idx] >= pixels[idx + 1] else 0)
    return h


def _hamming_distance(h1: int, h2: int) -> int:
    """汉明距离：两个64位哈希之间不同bit的数量"""
    return bin(h1 ^ h2).count("1")


# 触发器注册表
_TRIGGER_REGISTRY: Dict[str, dict] = {}


def register_triggers():
    """注册图片触发器"""
    global _TRIGGER_REGISTRY
    _TRIGGER_REGISTRY.clear()

    # 触发器1：微信图片（蓝哆啦）→ 进入调试模式
    trigger1 = os.path.join(_IMAGE_DIR, "微信图片_20260513093202_38_20.jpg")
    response1 = os.path.join(_IMAGE_DIR, "雪花飘飘.png")

    if os.path.exists(trigger1):
        try:
            h = _compute_dhash(trigger1)
            _TRIGGER_REGISTRY["微信图片"] = {
                "hash": h,
                "action": "enter_debug",
                "image": response1,
                "reply": _DEBUG_MODE_ENTER_MSG,
            }
        except Exception:
            pass

    # 触发器2：黑西服哆啦（叼烟斗）→ 退出调试模式
    trigger2 = os.path.join(_IMAGE_DIR, "黑西服哆啦.jpg")
    if os.path.exists(trigger2):
        try:
            h = _compute_dhash(trigger2)
            _TRIGGER_REGISTRY["黑西服哆啦"] = {
                "hash": h,
                "action": "exit_debug",
                "image": "",
                "reply": "嗯～",
            }
        except Exception:
            pass


# 模块加载时自动注册
register_triggers()


def register_trigger_from_received(name: str, received_path: str, action: str,
                                    image: str = "", reply: str = ""):
    """动态注册触发器（用于从QQ收到的图中注册新触发器）"""
    global _TRIGGER_REGISTRY
    if not os.path.exists(received_path):
        return False
    try:
        h = _compute_dhash(received_path)
        _TRIGGER_REGISTRY[name] = {
            "hash": h,
            "action": action,
            "image": image,
            "reply": reply,
        }
        return True
    except Exception:
        return False


# 触发器冷却：防止同一图片短时间重复触发
_trigger_cooldown = {}  # hash -> 上次触发时间戳
_trigger_cooldown_lock = threading.Lock()


def match_trigger_image(received_path: str, uid: int) -> Optional[dict]:
    """检测收到的图片是否匹配任一触发器
    返回: 匹配的触发器配置，或None
    """
    from .config import MASTER_UID
    if uid != MASTER_UID:
        return None

    if not _TRIGGER_REGISTRY:
        return None

    try:
        received_hash = _compute_dhash(received_path)
    except Exception:
        return None

    # 冷却检查：同一hash 5秒内不重复触发
    now = time.time()
    with _trigger_cooldown_lock:
        last_trigger = _trigger_cooldown.get(received_hash, 0)
        if now - last_trigger < 5:
            return None
        _trigger_cooldown[received_hash] = now

    for name, cfg in _TRIGGER_REGISTRY.items():
        dist = _hamming_distance(received_hash, cfg["hash"])
        # dHash对同一图片的不同压缩版本距离通常≤4，阈值8足够安全
        if dist <= 8:
            return cfg

    # 未匹配任何已注册触发器 → 返回None（动态注册逻辑由上层调用方处理）
    return None


# ── 文字指令 ──────────────────────────────────────────

def detect_command(text: str, uid: int) -> Optional[str]:
    """检测是否为指令消息
    返回: 指令文本(去掉前缀)，或 None(不是指令)
    """
    from .config import MASTER_UID

    if uid != MASTER_UID:
        return None

    text = text.strip()

    # /d 前缀 → 指令
    if text.startswith(COMMAND_PREFIX):
        cmd = text[len(COMMAND_PREFIX):].strip()
        if cmd:
            return cmd

    # 调试模式中 → 所有文本都是指令（不含媒体描述文本）
    if is_debug_mode_active(uid):
        # 退出指令
        if text in ("结束", "@冬结束", "退出", "退出调试", "exit"):
            exit_debug_mode()
            return "__EXIT_DEBUG__"  # 特殊标记：通知调用方退出调试模式
        # 排除图片识别后的描述文本（以"[图片]"开头）
        if not text.startswith("[图片]") and text:
            return text

    return None


def build_command_prompt() -> str:
    """构建指令模式的额外系统提示词"""
    return _COMMAND_SYSTEM


def resolve_image(rep: str) -> Tuple[str, Optional[str]]:
    """从回复中提取 [IMAGE:名称] 标签
    返回: (去除标签后的文本, 图片绝对路径或None)
    """
    m = re.search(r"\[IMAGE:(.+?)\]", rep)
    if not m:
        return rep, None

    name = m.group(1).strip()
    path = _IMAGE_REGISTRY.get(name)
    if not path and len(name) >= 3:
        for k, v in _IMAGE_REGISTRY.items():
            if name in k or k in name:
                path = v
                break

    cleaned = re.sub(r"\s*\[IMAGE:.+?\]\s*", "", rep).strip()
    return cleaned, path
