"""
冬 · 状态系统 — 激素系统
UserStatus / HormoneState / HormoneSystem 类
"""
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional


# ============ 状态系统 ============
@dataclass
class UserStatus:
    """用户状态类"""
    fatigue: int = 50
    mood: int = 60
    sleeping: bool = False
    last_update: Optional[str] = None

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "fatigue": self.fatigue,
            "mood": self.mood,
            "sleeping": self.sleeping,
            "last_update": self.last_update,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'UserStatus':
        """从字典创建"""
        return cls(
            fatigue=data.get("fatigue", 50),
            mood=data.get("mood", 60),
            sleeping=data.get("sleeping", False),
            last_update=data.get("last_update"),
        )


# ============ 激素系统 ============
@dataclass
class HormoneState:
    """五种激素状态"""
    dopamine: int = 60      # 多巴胺 愉悦/期待/奖赏
    adrenaline: int = 30    # 肾上腺素 紧张/兴奋/警觉
    cortisol: int = 20      # 皮质醇 压力/焦虑/烦躁
    oxytocin: int = 50      # 催产素 亲密/信任/依恋
    serotonin: int = 60    # 血清素 平静/满足/稳定

    def to_dict(self) -> Dict:
        return {
            "dopamine": self.dopamine, "adrenaline": self.adrenaline,
            "cortisol": self.cortisol, "oxytocin": self.oxytocin,
            "serotonin": self.serotonin
        }

    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(0, min(100, v))

    @classmethod
    def from_dict(cls, data: Dict) -> 'HormoneState':
        return cls(
            dopamine=cls._clamp(data.get("dopamine", 60)),
            adrenaline=cls._clamp(data.get("adrenaline", 30)),
            cortisol=cls._clamp(data.get("cortisol", 20)),
            oxytocin=cls._clamp(data.get("oxytocin", 50)),
            serotonin=cls._clamp(data.get("serotonin", 60)),
        )

    @property
    def dominant(self) -> str:
        """返回主导情绪标签"""
        d = self.to_dict()
        top = max(d, key=d.get)
        labels = {
            "dopamine": "兴奋期待", "adrenaline": "紧张警觉",
            "cortisol": "焦虑烦躁", "oxytocin": "亲密温暖",
            "serotonin": "平静满足"
        }
        # 组合判断
        if self.dopamine > 60 and self.oxytocin > 60:
            return "开心黏人"
        if self.adrenaline > 60 and self.cortisol > 50:
            return "紧张焦虑"
        if self.cortisol > 60 and self.serotonin < 40:
            return "烦躁低落"
        if self.dopamine > 65 and self.serotonin < 40:
            return "兴奋不安"
        return labels.get(top, "一般")

    def snapshot(self) -> Dict:
        """获取完整快照，含主导情绪"""
        d = self.to_dict()
        d["dominant"] = self.dominant
        return d


class HormoneSystem:
    """激素系统 —— 模拟神经递质共和作用"""

    # 基线值
    BASELINES = {"dopamine": 50, "adrenaline": 30, "cortisol": 20, "oxytocin": 50, "serotonin": 60}
    # 半衰期（小时）
    HALF_LIVES = {"dopamine": 2, "adrenaline": 0.5, "cortisol": 4, "oxytocin": 3, "serotonin": 6}
    HORMONE_NAMES = ["dopamine", "adrenaline", "cortisol", "oxytocin", "serotonin"]

    def __init__(self):
        self._h = HormoneState()
        self._last_update = None
        # P2: 追踪激素高于阈值的持续时间
        self._high_since: Dict[str, datetime] = {}
        self._high_thresholds = {
            "oxytocin":   {"threshold": 65, "min_minutes": 60, "label": "求贴贴/分享欲"},
            "cortisol":   {"threshold": 70, "min_minutes": 30, "label": "烦躁/抱怨"},
            "dopamine":   {"threshold": 70, "min_minutes": 45, "label": "兴奋/想分享"},
            "adrenaline": {"threshold": 65, "min_minutes": 20, "label": "紧张/不安"},
        }

    @property
    def state(self) -> HormoneState:
        return self._h

    def load(self, data: Dict):
        if data:
            self._h = HormoneState.from_dict(data)

    def to_dict(self) -> Dict:
        return self._h.to_dict()

    def snapshot(self) -> Dict:
        return self._h.snapshot()

    def apply_decay(self):
        """应用激素自然衰减（向基线回归）"""
        now = datetime.now()
        if self._last_update:
            hrs = (now - self._last_update).total_seconds() / 3600
            if hrs <= 0:
                return
            for name in self.HORMONE_NAMES:
                baseline = self.BASELINES[name]
                half = self.HALF_LIVES[name]
                current = getattr(self._h, name)
                if current == baseline:
                    continue
                gap = current - baseline
                decay_factor = 0.5 ** (hrs / half)
                new_gap = gap * decay_factor
                new_val = int(baseline + new_gap)
                new_val = max(0, min(100, new_val))
                setattr(self._h, name, new_val)
        self._last_update = now
        self._apply_interactions()
        self._update_high_tracking()

    def _apply_interactions(self):
        """激素间互相作用"""
        # 血清素抑制皮质醇
        self._h.cortisol = int(self._h.cortisol * max(0.3, 1 - self._h.serotonin / 200))
        # 催产素抑制肾上腺素
        self._h.adrenaline = int(self._h.adrenaline * max(0.3, 1 - self._h.oxytocin / 200))
        # 多巴胺+催产素同高时互相增强
        if self._h.dopamine > 55 and self._h.oxytocin > 55:
            self._h.dopamine = min(100, int(self._h.dopamine * 1.05))
            self._h.oxytocin = min(100, int(self._h.oxytocin * 1.05))
        # 肾上腺素+皮质醇同高时互相放大
        if self._h.adrenaline > 50 and self._h.cortisol > 40:
            self._h.cortisol = min(100, int(self._h.cortisol * 1.08))
        # 限制范围
        for name in self.HORMONE_NAMES:
            setattr(self._h, name, max(0, min(100, getattr(self._h, name))))

    def apply_event(self, event_type: str, intensity: float = 1.0):
        """根据事件类型调整激素"""
        events = {
            "praised": {"dopamine": +10, "oxytocin": +8},           # 被夸
            "loved": {"dopamine": +12, "oxytocin": +10, "adrenaline": +5},  # 被表白/喜欢
            "insulted": {"cortisol": +12, "serotonin": -8},         # 被骂
            "dismissed": {"cortisol": +8, "serotonin": -5},         # 被敷衍
            "surprised": {"adrenaline": +15, "dopamine": +5},      # 被点名/惊喜
            "deep_chat": {"oxytocin": +10, "serotonin": +5},        # 深度聊天
            "cold_shoulder": {"cortisol": +5, "oxytocin": -3},     # 被冷落
            "master_msg": {"dopamine": +3, "oxytocin": +2},        # 主号消息
            "negative_ripple": {"cortisol": +3, "serotonin": -3},  # 负面情绪涟漪
            "positive_ripple": {"dopamine": +4, "serotonin": +2},  # 正面情绪涟漪
        }
        if event_type not in events:
            return
        for name, delta in events[event_type].items():
            val = getattr(self._h, name) + int(delta * intensity)
            setattr(self._h, name, max(0, min(100, val)))
        self._apply_interactions()

    def derive_mood(self) -> int:
        """从激素派生 mood（0-100）"""
        h = self._h
        raw = 50 \
            + (h.dopamine - 50) * 0.4 \
            + (h.serotonin - 50) * 0.3 \
            + (h.oxytocin - 50) * 0.2 \
            - (h.cortisol - 20) * 0.3 \
            - abs(h.adrenaline - 30) * 0.1
        # 多巴胺+催产素同高时额外情绪提升
        if h.dopamine > 60 and h.oxytocin > 60:
            raw += 5
        # 皮质醇+肾上腺素同高时额外情绪压降
        if h.cortisol > 50 and h.adrenaline > 55:
            raw -= 8
        return max(0, min(100, int(raw)))

    def derive_fatigue(self) -> int:
        """从激素派生 fatigue（0-100）"""
        h = self._h
        raw = 50 + h.cortisol * 0.5 - h.serotonin * 0.3
        if h.adrenaline > 70:
            raw += 10  # 肾上腺素高时也累
        return max(0, min(100, int(raw)))

    def _update_high_tracking(self):
        """P2: 追踪每种激素高于阈值多久了"""
        now = datetime.now()
        for name, cfg in self._high_thresholds.items():
            val = getattr(self._h, name)
            if val >= cfg["threshold"]:
                if name not in self._high_since:
                    self._high_since[name] = now
            else:
                self._high_since.pop(name, None)

    def check_hormone_overflow(self) -> List[Dict]:
        """
        P2: 检查是否有激素持续溢出触发主动事件。
        返回: [{"hormone": "oxytocin", "label": "求贴贴", "duration_min": 65}, ...]
        调用后自动重置对应激素的计时（避免反复触发）
        """
        now = datetime.now()
        results = []
        for name, cfg in self._high_thresholds.items():
            since = self._high_since.get(name)
            if since is None:
                continue
            mins = (now - since).total_seconds() / 60
            if mins >= cfg["min_minutes"]:
                results.append({
                    "hormone": name,
                    "label": cfg["label"],
                    "duration_min": int(mins),
                    "value": getattr(self._h, name),
                })
                del self._high_since[name]  # 重置计时
        return results

    def rapid_spike(self, trigger_type: str, valence: float, arousal: float):
        """
        杏仁核触发的快速激素跳变。
        与 apply_event 不同：瞬时的、大幅度的，模拟杏仁核直接驱动激素释放。
        """
        spikes = {
            "threat": {"adrenaline": +20, "cortisol": +15, "serotonin": -10},
            "reward": {"dopamine": +15, "serotonin": +5},
            "social_bond": {"oxytocin": +15, "dopamine": +10},
            "surprise": {"adrenaline": +25},
        }
        if trigger_type not in spikes:
            return
        for name, base_delta in spikes[trigger_type].items():
            delta = int(base_delta * arousal) if trigger_type != "reward" else int(base_delta * valence)
            val = getattr(self._h, name) + delta
            setattr(self._h, name, max(0, min(100, val)))
        self._apply_interactions()
        from ..log import log
        log(f"  激素瞬跳[{trigger_type}]: dop={self._h.dopamine} adr={self._h.adrenaline} "
            f"cor={self._h.cortisol} oxy={self._h.oxytocin} ser={self._h.serotonin}")