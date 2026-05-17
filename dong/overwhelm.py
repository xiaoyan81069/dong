"""
冬 · 超限反应系统
- AI自主判断是否触发超限状态（在乎 vs 恐惧同时激活）
- 冲突生成
- 反应框架（语言碎片化、撤回概率、生理反应独白）
- 事后余震
- 超越反应
"""
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, List, Tuple

import requests

from .config import _get_cfg, MASTER_UID
from .log import log


@dataclass
class OverwhelmState:
    """超限状态"""
    active: bool = False
    conflict: str = ""        # 冲突描述
    care: str = ""            # 在乎什么
    fear: str = ""            # 恐惧什么
    target_uid: Optional[int] = None
    started: Optional[datetime] = None
    phase: str = "building"   # building/peak/recovery/aftermath
    trigger_count: int = 0    # 最近触发次数（影响余震长度）
    breakthrough: bool = False  # 是否触发超越反应

    PHASE_DURATIONS = {
        "building": 180,    # 3分钟
        "peak": 300,        # 5分钟
        "recovery": 600,    # 10分钟
        "aftermath": 86400, # 24小时
    }

    def phase_remaining(self) -> float:
        """当前阶段剩余秒数"""
        if not self.started:
            return 0
        elapsed = (datetime.now() - self.started).total_seconds()
        dur = self.PHASE_DURATIONS.get(self.phase, 0)
        return max(0, dur - elapsed)

    def advance_phase(self):
        """推进阶段"""
        phases = ["building", "peak", "recovery", "aftermath"]
        idx = phases.index(self.phase) if self.phase in phases else 0
        if idx < len(phases) - 1:
            self.phase = phases[idx + 1]
            self.started = datetime.now()
            log(f"  超限阶段推进: {phases[idx]} → {self.phase}")

    def retraction_probability(self) -> float:
        """当前阶段的撤回概率"""
        probs = {"building": 0.15, "peak": 0.35, "recovery": 0.20, "aftermath": 0.05}
        return probs.get(self.phase, 0.0)

    def fragmentation_intensity(self) -> float:
        """语言碎片化强度 0.0-1.0"""
        intensities = {"building": 0.3, "peak": 0.7, "recovery": 0.4, "aftermath": 0.1}
        return intensities.get(self.phase, 0.0)

    def after_overwhelm(self) -> bool:
        """是否在超限后遗症中"""
        return self.active and self.phase in ("recovery", "aftermath")


# 全局超限状态
_overwhelm = OverwhelmState()


def pre_screen(uid: int, user_text: str) -> bool:
    """
    预筛选：判断是否需要调用分析API做超限检测。
    条件1: 多巴胺>60 且 皮质醇>40（在乎+恐惧共存）
    条件2: 催产素>70 且 皮质醇>35（深度依恋但不安）
    文本预筛选已移除——由AI直接判断语义。
    """
    try:
        from .status import _status_manager
        h = _status_manager.hormones._h
    except Exception:
        return False

    # 已经处于超限状态 → 不重复触发
    if _overwhelm.active and _overwhelm.phase != "aftermath":
        return False

    # 只检查有实质内容的对话
    if len(user_text.strip()) < 3:
        return False

    # 条件1: 激素环境 + 主号
    hormone_zone = h.dopamine > 60 and h.cortisol > 40 and uid == MASTER_UID

    # 条件2: 深度依恋但不安 + 主号
    attachment_flag = h.oxytocin > 70 and h.cortisol > 35 and uid == MASTER_UID

    return hormone_zone or attachment_flag


def _build_detection_prompt(uid: int, user_text: str, history: list) -> str:
    """构建超限检测的 prompt"""
    try:
        from .status import _status_manager, get_hormone_snapshot
        snap = get_hormone_snapshot()
        mood = _status_manager._status.mood
        fatigue = _status_manager._status.fatigue
    except Exception:
        snap = {}
        mood = 50
        fatigue = 50

    # 最近对话
    recent_lines = []
    for q, a, t in history[-5:]:
        recent_lines.append(f"对方: {q[:80]}\n冬: {a[:80]}")
    recent_text = "\n".join(recent_lines) if recent_lines else "无"

    dom = snap.get("dominant", "一般")
    return f"""你是"冬"的内心分析师。判断她此刻是否进入了"超限状态"——即她最深的在乎和最大的恐惧同时被激活，两者剧烈冲突。

冬的当前状态：
- 激素: 多巴胺{snap.get('dopamine', '?')} 肾上腺素{snap.get('adrenaline', '?')} 皮质醇{snap.get('cortisol', '?')} 催产素{snap.get('oxytocin', '?')} 血清素{snap.get('serotonin', '?')}
- 主导情绪: {dom}
- 心情: {mood}/100 疲劳: {fatigue}/100

对方(QQ{uid})刚说: "{user_text}"

最近对话:
{recent_text}

判断标准：
1. 对方的话是否同时激活了冬的"在乎"(care)和"恐惧"(fear)？
   - 在乎：她在乎这个人、这段关系、某种价值观
   - 恐惧：她害怕暴露真实感受、被抛弃、被否定、失去关系
2. 冲突强度是否达到阈值？只有极高或高冲突才触发。

请用JSON回复（只输出JSON，不要其他内容）：
{{"triggered": true/false, "conflict": "冲突的一句话描述", "care": "她在乎什么", "fear": "她害怕什么", "intensity": "极高/高/中等/低"}}
"""


async def check_overwhelm(uid: int, user_text: str, history: list) -> bool:
    """
    调用分析API判断是否触发超限状态。
    返回 True 表示超限已触发。
    """
    global _overwhelm

    # 如果已在超限状态中（非aftermath），推进阶段而非重新触发
    if _overwhelm.active and _overwhelm.phase != "aftermath":
        remaining = _overwhelm.phase_remaining()
        if remaining <= 0:
            _overwhelm.advance_phase()
        return True

    # 推进阶段后再次检查：aftermath 仅在窗口到期时关闭
    if _overwhelm.active and _overwhelm.phase == "aftermath":
        remaining = _overwhelm.phase_remaining()
        if remaining <= 0:
            _overwhelm.active = False

    # 预筛选
    if not pre_screen(uid, user_text):
        return False

    # 调用分析API
    try:
        cfg = _get_cfg("analysis")
        prompt = _build_detection_prompt(uid, user_text, history)
        messages = [
            {"role": "system", "content": "你是一个分析助手，只输出JSON，不做其他回复。"},
            {"role": "user", "content": prompt}
        ]

        r = requests.post(
            f"{cfg.api_base}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
            json={"model": cfg.model, "temperature": 0.3, "max_tokens": 200, "messages": messages},
            timeout=15
        )

        if r.status_code == 200:
            resp = r.json()
            content = resp["choices"][0]["message"]["content"].strip()
            # 提取JSON
            if "{" in content and "}" in content:
                start = content.index("{")
                end = content.rindex("}") + 1
                result = json.loads(content[start:end])
            else:
                return False

            if result.get("triggered") and result.get("intensity") in ("极高", "高"):
                _overwhelm.active = True
                _overwhelm.conflict = result.get("conflict", "")
                _overwhelm.care = result.get("care", "")
                _overwhelm.fear = result.get("fear", "")
                _overwhelm.target_uid = uid
                _overwhelm.started = datetime.now()
                _overwhelm.phase = "building"
                _overwhelm.trigger_count += 1
                _overwhelm.breakthrough = False

                log(f"超限触发! uid={uid} conflict={_overwhelm.conflict} care={_overwhelm.care} fear={_overwhelm.fear}")
                return True

    except Exception as e:
        log(f"超限检测API异常: {e}")

    return False


def amygdala_hijack(uid: int, user_text: str) -> bool:
    """
    杏仁核劫持：绕过分析API，直接触发超限状态。
    用于极端威胁（threat_level>=3）的瞬间防御反应。
    返回 True 表示劫持成功。
    """
    try:
        from .amygdala import _amygdala
        amy = _amygdala._last_response
        if not amy:
            return False
        if amy.threat_level >= 3 and amy.arousal > 0.7:
            global _overwhelm
            _overwhelm.active = True
            _overwhelm.phase = "peak"  # 直接进入顶峰
            _overwhelm.conflict = f"杏仁核劫持: {amy.describe()} — 被强烈刺激瞬间触发防御"
            _overwhelm.care = "自我尊严"
            _overwhelm.fear = "被贬低/被攻击"
            _overwhelm.target_uid = uid
            _overwhelm.started = datetime.now()
            _overwhelm.trigger_count += 1
            _overwhelm.breakthrough = False
            log(f"杏仁核劫持触发! uid={uid} threat={amy.threat_level} arousal={amy.arousal}")
            return True
    except Exception as e:
        log(f"杏仁核劫持异常: {e}")
    return False


def get_overwhelm_prompt() -> str:
    """获取超限状态的系统提示词"""
    global _overwhelm
    if not _overwhelm.active:
        return ""

    # 推进阶段
    remaining = _overwhelm.phase_remaining()
    if remaining <= 0 and _overwhelm.phase != "aftermath":
        _overwhelm.advance_phase()

    if _overwhelm.phase == "aftermath":
        # 事后余震
        return f"""【事后余震】
你刚刚经历了一次剧烈的内心冲突（{_overwhelm.conflict}）。
现在你还处于余震中：回复更短、更敏感、主动联系概率低。
如果对方的消息让你想起刚才的事，你可能会缩回去或嘴硬。"""

    if _overwhelm.phase == "peak":
        state_desc = "冲突达到顶峰，你平时的防御——嘴硬、回避、幽默——在崩溃边缘。"
    elif _overwhelm.phase == "recovery":
        state_desc = "冲突在缓慢退去，你试图恢复平时的样子但还不稳。"
    else:
        state_desc = "冲突刚刚开始在你体内炸开，你还没想好怎么反应。"

    breakthrough_hint = ""
    if _overwhelm.breakthrough:
        breakthrough_hint = "但你在刚才选择了靠近而不是退缩。你依然害怕，但你选择了勇气。语气依然不稳，但方向是靠近。"

    return f"""【超限状态】
你在乎的：{_overwhelm.care}
你害怕的：{_overwhelm.fear}
此刻的冲突：{_overwhelm.conflict}
{state_desc}
你的语言会碎片化——长句拆短，发送间隔不稳。你可能会撤回刚才的话。{breakthrough_hint}
不要直接描述"我在超限状态"或"我在冲突中"，用你的行为自然体现。"""


def should_retract() -> bool:
    """当前是否应该撤回消息"""
    if not _overwhelm.active:
        return False
    prob = _overwhelm.retraction_probability()
    return random.random() < prob


def fragment_text(text: str) -> List[str]:
    """将回复碎片化为短句列表，用于分段发送"""
    if not _overwhelm.active:
        return [text]

    intensity = _overwhelm.fragmentation_intensity()
    if intensity < 0.2:
        return [text]

    # 按标点拆分
    import re
    parts = re.split(r'([，,。！？!?…、])', text)
    fragments = []
    buffer = ""
    for p in parts:
        buffer += p
        if p in '，,。！？!?…、' and len(buffer) > 3:
            fragments.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        fragments.append(buffer.strip())

    # 强度越高，碎片越短
    max_len = max(4, int(15 / intensity))
    result = []
    for f in fragments:
        while len(f) > max_len:
            split_at = max_len + random.randint(-3, 3)
            split_at = max(3, min(len(f), split_at))
            result.append(f[:split_at].strip())
            f = f[split_at:].strip()
        if f:
            result.append(f)

    return result if result else [text]


def get_unstable_delay() -> float:
    """获取超限状态下的不稳定发送间隔"""
    if not _overwhelm.active:
        return 0
    intensity = _overwhelm.fragmentation_intensity()
    if intensity < 0.2:
        return 0
    # 2-8秒随机，强度越高越可能极端
    return random.uniform(1.5, 8 * intensity + 1)


def generate_physiological_monologue() -> Optional[str]:
    """生成生理反应独白"""
    if not _overwhelm.active:
        return None

    monologues = [
        "手有点抖，不知道打什么字",
        "心跳好快，手机都快拿不稳了",
        "打了一行字又删了，打了又删",
        "感觉脸在发烫，明明没人看得到",
        "呼吸都变乱了，怎么回事",
        "不知道该说什么，脑子里一片空白",
        "手指在键盘上悬了半天",
    ]

    if _overwhelm.phase == "peak":
        monologues += [
            "好想跑，但跑不掉",
            "说也不是不说也不是",
            "嘴硬还是说真话，两个声音都在喊",
            "眼眶有点湿了，妈的",
        ]

    if _overwhelm.breakthrough:
        monologues += [
            "我很害怕但我还是做了",
            "说了就说了，反正收不回来",
            "这种不习惯的感觉...但是好像也没那么糟",
        ]

    return random.choice(monologues)


def maybe_trigger_breakthrough(bot_reply_text: str) -> bool:
    """
    检测超越反应：当她的回复方向是靠近而非回避时，
    且冲突尚在peak/recovery阶段，触发超越。
    """
    global _overwhelm
    if not _overwhelm.active:
        return False
    if _overwhelm.phase not in ("peak", "recovery"):
        return False
    if _overwhelm.breakthrough:
        return False

    # 靠近信号词
    approach_signals = ["我在乎", "我不想失去", "对不起", "我承认", "好吧",
                        "其实", "真的", "不是不喜欢", "是怕", "因为你"]
    if any(w in bot_reply_text for w in approach_signals):
        _overwhelm.breakthrough = True
        log(f"超越反应触发! 她在极限中选择了靠近")
        return True

    return False


def get_overwhelm_state() -> Dict:
    """导出超限状态（供仪表盘）"""
    global _overwhelm
    return {
        "active": _overwhelm.active,
        "phase": _overwhelm.phase,
        "conflict": _overwhelm.conflict,
        "care": _overwhelm.care,
        "fear": _overwhelm.fear,
        "target_uid": _overwhelm.target_uid,
        "trigger_count": _overwhelm.trigger_count,
        "breakthrough": _overwhelm.breakthrough,
    }
