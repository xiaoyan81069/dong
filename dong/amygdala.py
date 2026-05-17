"""
冬 · 杏仁核模块
- 消息处理链第一站，规则驱动，毫秒级
- 快速情绪价判断（威胁/奖赏/社会信号）
- 关联学习（词级情绪记忆）
- 杏仁核劫持（极端威胁时跳过皮层直接触发防御）
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from .config import MASTER_UID, BASE_DIR
from .log import log

AMYGDALA_FILE = os.path.join(BASE_DIR, "dong_amygdala_memory.json")
_MAX_UIDS = 50  # 限制最大用户数，防止内存无限增长


@dataclass
class AmygdalaResponse:
    """杏仁核处理结果"""
    valence: float = 0.0            # -1.0(威胁) ~ +1.0(奖赏)
    arousal: float = 0.0            # 0.0(平静) ~ 1.0(极度兴奋)
    trigger_type: str = "neutral"   # threat/reward/social_bond/surprise/neutral
    salient: bool = False           # 是否应被记忆强化
    hijack: bool = False            # 是否触发杏仁核劫持
    threat_level: int = 0           # 0-3 威胁等级
    reward_type: str = ""           # love/praise/surprise

    def describe(self) -> str:
        if self.trigger_type == "threat":
            return f"威胁L{self.threat_level}"
        if self.trigger_type == "reward":
            return f"奖赏:{self.reward_type}"
        if self.trigger_type == "social_bond":
            return "社会纽带"
        if self.trigger_type == "surprise":
            return "惊讶"
        return "中性"

    def alert_state(self) -> str:
        """警觉状态标签"""
        if self.hijack:
            return "劫持"
        if self.arousal > 0.7:
            return "高度警觉"
        if self.arousal > 0.3:
            return "警觉"
        return "平静"


class AmygdalaSystem:
    """杏仁核系统 —— 快速情绪直觉判断"""

    # ===== 威胁词库 (valence负, high arousal) =====
    THREAT_RED = {       # Level 3: 严重威胁 → 劫持
        "滚": -0.9, "傻逼": -0.9, "sb": -0.9, "脑残": -0.9,
        "有病": -0.9, "恶心": -0.9, "去死": -0.95, "闭嘴": -0.85,
        "guna": -0.9, "gun": -0.9, "你他妈": -0.95,
    }
    THREAT_ORANGE = {    # Level 2: 中度威胁
        "烦": -0.7, "别说了": -0.7, "不想理你": -0.75,
        "讨厌": -0.6, "恨": -0.8, "我恨你": -0.8,
    }
    THREAT_YELLOW = {    # Level 1: 轻度威胁
        "6": -0.35, "哦": -0.4, "嗯": -0.3, "额": -0.3,
        "呵呵": -0.5, "行吧": -0.4, "随便": -0.45,
    }

    # ===== 奖赏词库 (valence正) =====
    REWARD_LOVE = {      # 亲密信号
        "爱你": 0.9, "想你": 0.85, "抱抱": 0.8, "亲亲": 0.85,
        "乖": 0.7, "想你了": 0.85,
    }
    REWARD_PRAISE = {    # 夸赞信号
        "好棒": 0.75, "厉害": 0.7, "好看": 0.65, "可爱": 0.7,
        "好帅": 0.65, "天才": 0.8, "真好看": 0.7, "好听": 0.65,
        "温柔": 0.6, "嘿嘿": 0.3, "好哦": 0.2, "开心": 0.5,
    }
    REWARD_APPROVAL = {  # 认可信号（弱）
        "谢谢": 0.3, "行": 0.15, "好": 0.1, "对": 0.1,
        "可以": 0.1, "还行": 0.05,
    }
    REWARD_NEEDED = {    # 被需要信号
        "你在吗": 0.6, "陪我": 0.65, "想听你说": 0.7,
        "听听": 0.4, "说句话": 0.4,
    }

    # ===== 惊讶词库 =====
    SURPRISE_WORDS = {
        "突然": 0.8, "出事了": 0.9, "你知道吗": 0.6,
        "猜猜": 0.5, "告诉你": 0.4, "大事": 0.8,
    }

    def __init__(self):
        self._associations: Dict[str, Dict] = {}      # {uid_str: {word: {valence, arousal, count, last}}}
        self._last_response: Optional[AmygdalaResponse] = None
        self._recent_responses: list = []              # 最近10次响应
        self._total_threats: int = 0
        self._total_rewards: int = 0

    # ===== 持久化 =====
    def load(self):
        try:
            if os.path.exists(AMYGDALA_FILE):
                with open(AMYGDALA_FILE, "r", encoding="utf-8") as f:
                    self._associations = json.load(f)
        except Exception:
            pass

    def save(self):
        """原子写入：先写临时文件再替换，防止崩溃损坏数据。"""
        try:
            tmp = AMYGDALA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._associations, f, ensure_ascii=False, indent=2)
            os.replace(tmp, AMYGDALA_FILE)
        except Exception as e:
            log(f"杏仁核保存失败: {e}")

    # ===== 核心处理 =====
    def process(self, uid: int, text: str) -> AmygdalaResponse:
        """第一站：快速情绪价判断"""
        t = text.lower().strip()
        resp = AmygdalaResponse()

        # 空消息
        if len(t) < 1:
            self._last_response = resp
            return resp

        # 1. 词级威胁检测（优先级最高）
        self._detect_threat(t, resp)

        # 2. 词级奖赏检测
        self._detect_reward(t, resp)

        # 3. 惊讶检测
        self._detect_surprise(t, resp)

        # 4. 关联记忆增强
        self._apply_associations(uid, t, resp)

        # 5. 计算 arousal
        self._compute_arousal(resp)

        # 6. 劫持判断
        if resp.threat_level >= 3 and resp.arousal > 0.7:
            resp.hijack = True

        # 7. 显著性判断
        resp.salient = resp.arousal > 0.5 or resp.threat_level >= 2

        # 记录
        self._last_response = resp
        self._recent_responses.append({
            "uid": str(uid), "text": text[:30], "valence": resp.valence,
            "arousal": resp.arousal, "type": resp.trigger_type,
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        if len(self._recent_responses) > 20:
            self._recent_responses = self._recent_responses[-20:]
        if resp.threat_level >= 2:
            self._total_threats += 1
        if resp.valence > 0.5:
            self._total_rewards += 1

        log(f"  杏仁核: {resp.describe()} val={resp.valence:.1f} arousal={resp.arousal:.1f} "
            f"{'[劫持]' if resp.hijack else ''}{'[显著]' if resp.salient else ''}")
        return resp

    def _word_match(self, text: str, word: str) -> bool:
        """词边界匹配：英文用\b，中文单字用完整匹配防误判"""
        if word.isascii():
            return bool(re.search(r'\b' + re.escape(word) + r'\b', text, re.IGNORECASE))
        if len(word) == 1:
            # 单字中文只在完全匹配或独立出现时命中，避免"滚"匹配"滚筒"
            return word == text or f" {word} " in f" {text} "
        return word in text

    def _detect_threat(self, t: str, resp: AmygdalaResponse):
        """检测威胁信号"""
        # L3 红色威胁
        for word, val in self.THREAT_RED.items():
            if self._word_match(t, word):
                resp.valence = val
                resp.arousal = 0.9
                resp.threat_level = 3
                resp.trigger_type = "threat"
                return
        # L2 橙色威胁
        for word, val in self.THREAT_ORANGE.items():
            if self._word_match(t, word):
                resp.valence = val
                resp.arousal = 0.7
                resp.threat_level = 2
                resp.trigger_type = "threat"
                return
        # L1 黄色威胁 (纯敷衍词需要完全匹配)
        plain_text = t.strip()
        for word, val in self.THREAT_YELLOW.items():
            if plain_text == word or (len(plain_text) <= 2 and word in plain_text):
                resp.valence = val
                resp.arousal = 0.4
                resp.threat_level = 1
                resp.trigger_type = "threat"
                return

    def _detect_reward(self, t: str, resp: AmygdalaResponse):
        """检测奖赏信号"""
        best_val = 0.0
        best_type = ""
        best_type_name = ""

        for name, word_dict, rtype in [
            ("love", self.REWARD_LOVE, "social_bond"),
            ("praise", self.REWARD_PRAISE, "reward"),
            ("needed", self.REWARD_NEEDED, "social_bond"),
            ("approval", self.REWARD_APPROVAL, "reward"),
        ]:
            for word, val in word_dict.items():
                if self._word_match(t, word) and val > best_val:
                    best_val = val
                    best_type_name = name
                    best_type = rtype

        if best_val > 0:
            resp.valence = best_val
            resp.trigger_type = best_type
            resp.reward_type = best_type_name

    def _detect_surprise(self, t: str, resp: AmygdalaResponse):
        """检测惊讶信号"""
        for word, arousal in self.SURPRISE_WORDS.items():
            if self._word_match(t, word):
                resp.trigger_type = "surprise"
                resp.arousal = max(resp.arousal, arousal)
                break

    def _apply_associations(self, uid: int, t: str, resp: AmygdalaResponse):
        """应用学习到的关联记忆增强/削弱信号"""
        uid_key = str(uid)
        if uid_key not in self._associations:
            return

        assoc = self._associations[uid_key]
        matched = []
        for word, data in assoc.items():
            if self._word_match(t, word) and len(word) >= 2 and data.get("count", 0) >= 2:
                # 衰减：超过30天未触发，权重减半
                last = data.get("last", "")
                try:
                    last_dt = datetime.strptime(last, "%Y-%m-%d")
                    days = (datetime.now() - last_dt).days
                    decay = 0.5 ** (days / 30)
                except Exception:
                    decay = 1.0
                matched.append((data["valence"] * decay, data["arousal"] * decay))

        if matched:
            avg_val = sum(m[0] for m in matched) / len(matched)
            avg_aro = sum(m[1] for m in matched) / len(matched)
            # 关联记忆影响但不完全覆盖当前判断
            resp.valence = resp.valence * 0.7 + avg_val * 0.3
            resp.arousal = max(resp.arousal, avg_aro * 0.5)

    def _compute_arousal(self, resp: AmygdalaResponse):
        """综合计算唤醒度"""
        # 威胁自带 arousal
        if resp.threat_level > 0:
            resp.arousal = max(resp.arousal, resp.threat_level * 0.3)
        # 奖赏强度映射到 arousal
        if abs(resp.valence) > 0.5 and resp.arousal < 0.3:
            resp.arousal = abs(resp.valence) * 0.4
        # 惊讶大幅提升 arousal
        if resp.trigger_type == "surprise" and resp.arousal < 0.5:
            resp.arousal = max(resp.arousal, 0.5)
        # 限制范围
        resp.arousal = min(1.0, max(0.0, resp.arousal))

    # ===== 关联学习 =====
    def learn(self, uid: int, text: str, actual_valence: float, actual_arousal: float):
        """
        从交互结果中学习：只学习触发杏仁核的 关键词 的情绪关联。
        不再使用全文本 n-gram 滑动窗口，避免产生无意义片段如"天天气""气真"。
        """
        if abs(actual_valence) < 0.4 and actual_arousal < 0.5:
            return

        # 收集所有杏仁核监察词
        all_watch = set()
        for d in [self.THREAT_RED, self.THREAT_ORANGE, self.THREAT_YELLOW,
                  self.REWARD_LOVE, self.REWARD_PRAISE, self.REWARD_APPROVAL,
                  self.REWARD_NEEDED, self.SURPRISE_WORDS]:
            all_watch.update(d.keys())

        # 只保留文本中真正出现的监察词
        matched_words = [w for w in all_watch if w in text]

        # 如果没有监察词命中但情绪显著（比如纯粹的语气表达），跳过学习
        if not matched_words:
            return

        uid_key = str(uid)
        if uid_key not in self._associations:
            self._associations[uid_key] = {}

        today = datetime.now().strftime("%Y-%m-%d")
        for word in set(matched_words):
            if word in self._associations[uid_key]:
                old = self._associations[uid_key][word]
                n = old["count"] + 1
                self._associations[uid_key][word] = {
                    "valence": old["valence"] * 0.7 + actual_valence * 0.3,
                    "arousal": old["arousal"] * 0.7 + actual_arousal * 0.3,
                    "count": n,
                    "last": today,
                }
            else:
                self._associations[uid_key][word] = {
                    "valence": actual_valence,
                    "arousal": actual_arousal,
                    "count": 1,
                    "last": today,
                }

        # 限制每个用户的关联词数量
        if len(self._associations[uid_key]) > 200:
            sorted_words = sorted(
                self._associations[uid_key].items(),
                key=lambda x: x[1]["count"], reverse=True
            )
            self._associations[uid_key] = dict(sorted_words[:150])

        # 限制最大UID数，淘汰最久未活跃的用户
        if len(self._associations) > _MAX_UIDS:
            sorted_uids = sorted(
                self._associations.keys(),
                key=lambda k: max(
                    (v.get("last", "1970-01-01") for v in self._associations[k].values()),
                    default="1970-01-01"
                ),
            )
            for old_uid in sorted_uids[:len(self._associations) - _MAX_UIDS + 10]:
                del self._associations[old_uid]

        self.save()

    # ===== 状态导出 =====
    def get_state(self) -> Dict:
        """导出杏仁核状态（供仪表盘）"""
        last = self._last_response
        # 提取关联词 top 10（按count排序）
        all_assoc = []
        for uid_key, words in self._associations.items():
            for word, data in words.items():
                all_assoc.append({"word": word, "valence": data["valence"], "arousal": data["arousal"], "count": data.get("count", 1), "uid": uid_key})
        top_assoc = sorted(all_assoc, key=lambda x: x["count"], reverse=True)[:12]
        return {
            "last_valence": last.valence if last else 0,
            "last_arousal": last.arousal if last else 0,
            "last_type": last.trigger_type if last else "neutral",
            "alert": last.alert_state() if last else "平静",
            "hijack": last.hijack if last else False,
            "salient": last.salient if last else False,
            "threat_level": last.threat_level if last else 0,
            "reward_type": last.reward_type if last else "",
            "total_threats": self._total_threats,
            "total_rewards": self._total_rewards,
            "association_count": sum(len(v) for v in self._associations.values()),
            "top_associations": top_assoc,
            "recent": self._recent_responses[-5:],
        }


# 全局杏仁核实例
_amygdala = AmygdalaSystem()


def process_amygdala(uid: int, text: str) -> AmygdalaResponse:
    """全局入口：杏仁核快速处理"""
    return _amygdala.process(uid, text)


def amygdala_learn(uid: int, text: str, actual_valence: float, actual_arousal: float):
    """全局入口：杏仁核关联学习"""
    _amygdala.learn(uid, text, actual_valence, actual_arousal)


def get_amygdala_state() -> Dict:
    """全局入口：获取杏仁核状态"""
    return _amygdala.get_state()
