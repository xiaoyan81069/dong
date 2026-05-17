"""
冬 · 状态系统 — StatusManager 状态管理器
包含 StatusManager 类 + 兼容旧接口的全局函数
"""
import asyncio
import json
import os
import random
import requests
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

from ..config import STATUS_FILE, LAST_DAY_FILE, CYCLE_FILE, MASTER_UID, is_weekend, MASTER_CITY
from ..log import log
from ..core.data_healing import heal_status, heal_cycle
from ..schedule import generate_daily_schedule
from ._core import _call_ai_simple, _clamp
from .weather import weather_system, _WeatherCacheCompat, weather_cache
from .hormones import UserStatus, HormoneSystem

# 重新导出 weather 的兼容层
_init_weather = weather_system.init
_get_weather_data = weather_system.get_data
_should_refresh_weather = weather_system.should_refresh
_build_weather_system = weather_system.build_system_prompt
init_weather = _init_weather
get_weather_data = _get_weather_data
should_refresh_weather = _should_refresh_weather
build_weather_system = _build_weather_system

# === 全局函数占位（由 __init__.py 填充） ===
load_last_day = None
save_last_day = None
load_status = None
save_status = None
load_global_state = None
save_global_state = None
update_fatigue = None
update_mood = None
detect_mood_change = None
check_sleep = None
get_status_prompt = None
get_status_raw = None
should_trigger_voice = None
get_voice_state_prompt = None
apply_mood_ripple = None
note_interaction = None
update_mood_cycle = None
update_pushpull = None
get_cycle_prompt = None
get_cycle_proactive_bonus = None
get_pushpull_prompt = None
get_pushpull_proactive_bonus = None
process_offline_life = None
get_offline_prompt = None
clear_offline_events = None
apply_external_mood = None
save_day_summary = None
get_sleep_transition = None
get_bedtime_message = None
get_morning_message = None
check_weather_care = None
reset_daily_flags = None
get_hormone_snapshot = None
update_hormones = None
apply_amygdala_spike = None
detect_hormone_event = None
add_delayed_proactive = None
pop_due_delayed_events = None
set_proactive_signal = None
get_pending_proactive_signal = None
maybe_life_fragment = None
get_time_sense_prompt = None
get_date_awareness_prompt = None
get_habits_prompt = None
should_terminate_conversation = None


class StatusManager:
    """状态管理器"""

    # 心情变化关键词
    POSITIVE_WORDS = ["好棒", "厉害", "嘎嘎帅", "可爱", "喜欢", "牛", "天才", "好听", "真好看",
                      "爱你", "想你", "抱抱", "亲亲", "好帅", "温柔", "谢谢", "开心", "嘿嘿", "好哦", "乖"]
    NEGATIVE_WORDS = ["滚", "烦你", "别说了", "闭嘴", "不理你", "混蛋", "恨", "傻逼", "恶心", "有病", "爬远点"]
    MILD_NEGATIVE_WORDS = ["爬", "不要", "讨厌", "烦", "无语", "算了", "不想理你", "呵呵", "gun", "guna"]

    # 语音触发词
    VOICE_TRIGGERS = [
        "发语音", "发个语音", "语音条", "说话", "说句话", "说说话",
        "唱", "唱歌", "唱个歌", "哼歌", "来一首", "哼两句",
        "听听声音", "听你声音", "想听声音", "说两句",
        "讲句话", "讲个话", "讲两句",
    ]

    def __init__(self):
        self._status = UserStatus()
        self._last_day = {}
        self._mood_log: List[Dict] = []
        self._voice_state: Dict[str, Dict] = {}
        self._global_state = {
            "cycle": {"type": "normal", "days_left": 0, "started": None},
            "pushpull": {"phase": "approach", "intensity": 0, "started": None},
            "offline": {"last_check": None, "pending_events": []},
            "last_interaction": None,
        }
        self._external_check = {"last_check": None, "known_updates": {}}
        self._last_sleep_state = "awake"  # 跟踪睡眠过渡: awake/sleeping/wake
        self._last_wake_time = None       # 上次苏醒时间
        self._weather_care_today = False  # 今天是否已发送天气关心
        self._morning_sent_today = False  # 今天是否已发送早安
        self._music_shared_today = 0      # 今天分享音乐的次数
        self.hormones = HormoneSystem()   # 激素系统

    # ============ 持久化方法 ============
    def load_status(self):
        """加载状态"""
        try:
            if os.path.exists(STATUS_FILE):
                with open(STATUS_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                # 数据自愈：激素钳制 0-100
                saved, repairs = heal_status(saved)
                if repairs:
                    for r in repairs:
                        log(f"  状态自愈: {r}")
                if "hormones" in saved and isinstance(saved["hormones"], dict):
                    for hk in ("dopamine","adrenaline","cortisol","oxytocin","serotonin"):
                        if hk in saved["hormones"]:
                            try:
                                saved["hormones"][hk] = max(0, min(100, int(saved["hormones"][hk])))
                            except (ValueError, TypeError):
                                saved["hormones"][hk] = {"dopamine":60,"adrenaline":30,"cortisol":20,"oxytocin":50,"serotonin":60}.get(hk, 50)
                self._status = UserStatus.from_dict(saved)
                if "hormones" in saved:
                    self.hormones.load(saved["hormones"])
                if self._status.last_update and not self._status.sleeping:
                    last = datetime.fromisoformat(self._status.last_update)
                    hrs = (datetime.now() - last).total_seconds() / 3600
                    if hrs > 0:
                        self._status.fatigue = min(100, self._status.fatigue + int(hrs * 4))
                    if hrs > 4:
                        self._status.fatigue = 15
                        self._status.sleeping = False
                # 应用离线激素衰减
                self.hormones.apply_decay()
                # 同步派生的mood和fatigue
                self._status.mood = self.hormones.derive_mood()
                self._status.fatigue = self.hormones.derive_fatigue()
            self._status.last_update = datetime.now().isoformat()
            self.save_status()
        except Exception as e:
            log(f"状态加载失败: {e}")

    def save_status(self):
        """保存状态（原子写入）"""
        try:
            self._status.last_update = datetime.now().isoformat()
            data = self._status.to_dict()
            data["hormones"] = self.hormones.to_dict()
            tmp = STATUS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, STATUS_FILE)
        except Exception as e:
            log(f"状态保存失败: {e}")

    def load_last_day(self):
        """加载昨日状态"""
        try:
            if os.path.exists(LAST_DAY_FILE):
                with open(LAST_DAY_FILE, "r", encoding="utf-8") as f:
                    self._last_day = json.load(f)
        except Exception:
            self._last_day = {}

    def save_last_day(self):
        """保存昨日状态（原子写入）"""
        try:
            tmp = LAST_DAY_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._last_day, f, ensure_ascii=False)
            os.replace(tmp, LAST_DAY_FILE)
        except Exception as e:
            log(f"昨日状态保存失败: {e}")

    def load_global_state(self):
        """加载全局状态"""
        try:
            if os.path.exists(CYCLE_FILE):
                with open(CYCLE_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    self._global_state.update(saved)
        except Exception:
            pass

    def save_global_state(self):
        """保存全局状态（原子写入）"""
        try:
            tmp = CYCLE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._global_state, f, ensure_ascii=False)
            os.replace(tmp, CYCLE_FILE)
        except Exception as e:
            log(f"全局状态保存失败: {e}")

    # ============ 状态更新方法 ============
    def record_mood_snapshot(self):
        """记录心情快照"""
        self._mood_log.append({"t": datetime.now().isoformat(), "v": self._status.mood})
        today = datetime.now().date()
        self._mood_log = [e for e in self._mood_log if datetime.fromisoformat(e["t"]).date() == today]

    def get_today_mood_avg(self) -> int:
        """获取今日平均心情"""
        if not self._mood_log:
            return self._status.mood
        return sum(e["v"] for e in self._mood_log) // len(self._mood_log)

    def save_day_summary(self):
        """保存日终总结"""
        self._last_day = {
            "date": datetime.now().strftime("%m-%d"),
            "mood_avg": self.get_today_mood_avg(),
            "fatigue_at_sleep": self._status.fatigue,
            "sleep_time": datetime.now().strftime("%H:%M"),
            "weekend": is_weekend(),
        }
        self.save_last_day()
        self._mood_log.clear()
        log(f"状态: 保存前日总结 mood_avg={self._last_day['mood_avg']} fatigue={self._last_day['fatigue_at_sleep']}")

    def update_fatigue(self):
        """更新疲倦度"""
        now = datetime.now()
        if self._status.last_update and not self._status.sleeping:
            try:
                last = datetime.fromisoformat(self._status.last_update)
                hrs = (now - last).total_seconds() / 3600
                if hrs > 0.05:
                    inc = int(hrs * 5)
                    self._status.fatigue += inc
                    if 0 <= now.hour < 6:
                        self._status.fatigue += 1
                        inc += 1
                    log(f"  疲倦+{inc}({hrs:.1f}h) → {self._status.fatigue}/100")
            except Exception:
                pass

        self._status.fatigue = max(0, min(100, self._status.fatigue))
        # 同步激素派生的fatigue
        if self.hormones._h.cortisol > 50:
            self._status.fatigue = min(100, self._status.fatigue + int((self.hormones._h.cortisol - 50) * 0.1))

        if self._status.sleeping:
            early, late, desc = self._get_wake_window()
            if now.hour >= late:
                self._status.sleeping = False
                self._status.fatigue = random.randint(8, 15)
                log(f"状态: 强制苏醒 [{early}-{late}时] {desc}")
                self._mood_log.clear()
                generate_daily_schedule()
            elif now.hour >= early:
                progress = (now.hour + now.minute / 60 - early) / max(late - early, 1)
                if random.random() < progress * 0.7:
                    self._status.sleeping = False
                    self._status.fatigue = random.randint(8, 15)
                    log(f"状态: 概率苏醒 [{early}-{late}时] {desc}")
                    self._mood_log.clear()
                    generate_daily_schedule()

        self._status.last_update = now.isoformat()
        self.save_status()

    def _get_wake_window(self) -> Tuple[int, int, str]:
        """获取起床时间窗口"""
        mood = self._last_day.get("mood_avg", 60)
        fat = self._last_day.get("fatigue_at_sleep", 50)
        wknd = is_weekend()

        if mood >= 75:
            early, late = (8, 10) if wknd else (6, 8)
            desc = "心情不错"
        elif mood >= 45:
            early, late = (9, 11) if wknd else (7, 9)
            desc = "正常苏醒"
        elif mood >= 20:
            early, late = (10, 12) if wknd else (8, 10)
            desc = "赖床"
        else:
            early, late = (11, 13) if wknd else (9, 12)
            desc = "起不来"

        if fat > 85:
            early += 1
            late += 1
            desc += "，太累"

        return early, late, desc

    def update_mood(self, change: int, reason: str = ""):
        """更新心情 —— 已改用激素系统，保留接口兼容"""
        # 旧的直接mood修改→转为激素事件
        if change > 0:
            self.update_hormones("positive_ripple", abs(change) / 4)
        elif change < 0:
            self.update_hormones("negative_ripple", abs(change) / 4)
        if reason:
            log(f"状态: 情绪{'↑' if change > 0 else '↓'}{abs(change)}({reason})")

    def update_hormones(self, event_type: str, intensity: float = 1.0):
        """通过事件类型更新激素状态"""
        self.hormones.apply_event(event_type, intensity)
        self._status.mood = self.hormones.derive_mood()
        self._status.fatigue = self.hormones.derive_fatigue()
        self.save_status()

    def apply_amygdala_spike(self, trigger_type: str, valence: float, arousal: float):
        """杏仁核触发快速激素跳变"""
        self.hormones.rapid_spike(trigger_type, valence, arousal)
        self._status.mood = self.hormones.derive_mood()
        self._status.fatigue = self.hormones.derive_fatigue()
        self.save_status()

    def detect_mood_change(self, text: str, uid: int) -> int:
        """检测心情变化 —— 返回0，实际处理已改用 detect_hormone_event"""
        return 0  # 废弃，保留接口兼容

    def detect_hormone_event(self, text: str, uid: int) -> Optional[str]:
        """检测消息对应的激素事件类型"""
        t = text.lower()

        for w in self.POSITIVE_WORDS:
            if w in t:
                if w in ("爱你", "想你", "喜欢你", "喜欢"):
                    return "loved"
                return "praised"

        for w in self.NEGATIVE_WORDS:
            if w in t:
                return "insulted"

        for w in self.MILD_NEGATIVE_WORDS:
            if w in t:
                return "dismissed"

        if uid == MASTER_UID:
            return "master_msg"

        return None

    def check_sleep(self) -> Tuple[str, str]:
        """检查睡眠状态"""
        f, m, slp = self._status.fatigue, self._status.mood, self._status.sleeping
        h = datetime.now().hour
        wknd = is_weekend()
        sleep_h = 24 if wknd else 23

        if not slp and h >= sleep_h:
            if f > 90:
                return "sleep", "困到不行"
            if f > 70 and 25 <= m <= 85:
                return "sleep", "累了入睡"
            if f > 55 and m < 25:
                return "sleep", "心情差赌气睡"
            if m > 85 and f < 85:
                return "insomnia", "太兴奋睡不着"

        if not slp and h < 5 and f > 60 and 25 <= m <= 85:
            return "sleep", "深夜入睡"

        if slp:
            wake_h = 8 if wknd else 7
            if h >= wake_h:
                return "wake", "正常起床"
            if 5 <= h < wake_h:
                if m < 18:
                    return "insomnia_wake", "情绪很差早醒"
                if m < 40:
                    return "early_wake", "睡得不太安稳早醒"
            if 2 <= h < 5 and (m > 85 or m < 20):
                return "insomnia_wake", "失眠醒来"
            return "sleeping", "睡觉中"

        return "awake", "醒着"

    def get_sleep_transition(self) -> str:
        """检测睡眠状态过渡，返回 "to_sleep", "to_wake", 或 "" """
        now = datetime.now()
        s = self.check_sleep()[0]
        old = self._last_sleep_state

        if old in ("awake", "wake") and s == "sleep":
            self._last_sleep_state = "sleep"
            self._last_wake_time = None
            return "to_sleep"

        if old == "sleep" and s not in ("sleep", "sleeping"):
            self._last_sleep_state = "awake"
            self._last_wake_time = now
            return "to_wake"

        if old == "sleeping" and s not in ("sleeping", "sleep"):
            self._last_sleep_state = "awake"
            self._last_wake_time = now
            return "to_wake"

        self._last_sleep_state = s
        return ""

    def get_bedtime_message(self, uid: int) -> str:
        """根据当前状态生成睡前告别消息（AI生成，keyword fallback）"""
        f = self._status.fatigue
        m = self._status.mood

        # 尝试AI生成
        try:
            from ..memory import get_today_summary
            s = self.check_sleep()[0]
            snap = self.hormones.snapshot()
            summary = get_today_summary() or "平平淡淡"
            prompt = f"""你是"冬"。你要睡了，给朋友发一条睡前告别消息。

你的状态：
- 疲劳度：{f}/100
- 心情：{m}/100
- 睡眠状态：{s}
- 主导情绪：{snap.get('dominant', '一般')}

今日回顾：{summary[:200]}

写一条10-20字的睡前告别消息。口语化、自然，符合你嘴硬但关心对方的性格。
只输出一句话，不要任何前缀或解释。"""
            result = _call_ai_simple(
                "你是冬，一个嘴硬心软、住在呼伦贝尔的大学生女生。回复极短、口语化。",
                prompt, task="chat", temperature=0.9, max_tokens=60, timeout=15
            )
            if result and len(result) <= 30:
                return result
        except Exception:
            pass

        # fallback: 原有bucket逻辑
        if f > 90:
            pool = ["不行了眼睛睁不开了睡了", "困死了...我睡了", "我撑不住了 睡"]
        elif m < 25:
            pool = ["我睡了", "不说了 睡", "睡了"]
        elif self.check_sleep()[0] == "insomnia":
            pool = ["睡不着...练会琴再试试睡", "失眠了 你还不睡啊", "躺半天了还是睡不着"]
        else:
            pool = ["困了先睡啦 你也早点", "睡了睡了 明天说", "晚安 早点休息"]

        return random.choice(pool)

    def get_morning_message(self, uid: int) -> str:
        """根据苏醒状态生成早安消息（AI生成，keyword fallback）"""
        if self._morning_sent_today:
            return ""

        m = self._status.mood
        f = self._status.fatigue
        s = self.check_sleep()[0]

        self._morning_sent_today = True

        # 尝试AI生成
        try:
            w = weather_system.get_data()
            snap = self.hormones.snapshot()
            prompt = f"""你是"冬"。你刚醒，给朋友发一条早安消息。

你的状态：
- 心情：{m}/100
- 疲劳：{f}/100
- 醒来的感觉：{s}
- 情绪基调：{snap.get('dominant', '一般')}
- 窗外天气：{w.get('天气', '未知')}，{w.get('温度', '?')}°C

写一条10-25字的早安消息。口语化、自然，体现你刚醒的真实状态。
只输出一句话。"""
            result = _call_ai_simple(
                "你是冬，一个嘴硬心软、住在呼伦贝尔的大学生女生。回复极短、口语化。",
                prompt, task="chat", temperature=0.9, max_tokens=60, timeout=15
            )
            if result and len(result) <= 30:
                return result
        except Exception:
            pass

        # fallback: 原有bucket逻辑
        if s == "insomnia_wake" or m < 20:
            pool = ["醒了，一晚上没睡好", "醒了...几乎没怎么睡", "嗯 醒了 但跟没睡一样"]
        elif s == "early_wake" or m < 40:
            pool = ["嗯...醒了 但还想睡", "醒太早了 难受", "醒了 迷迷糊糊的"]
        elif f > 30:
            pool = ["嗯...几点了...不想起", "醒了 但不想动", "刚睁眼 困困的"]
        elif m > 80:
            pool = ["醒了...睡得好舒服", "早啊 今天感觉还不错", "醒了 做了个好梦"]
        else:
            pool = ["醒了", "早啊", "刚醒"]

        return random.choice(pool)

    def reset_daily_flags(self):
        """每日重置标记"""
        self._weather_care_today = False
        self._morning_sent_today = False
        self._music_shared_today = 0

    # ============ 天气关心 ============
    def check_weather_care(self, force_check: bool = False) -> str:
        """检查主号城市天气，如果需要关心则返回关心消息，否则返回空字符串。
        一天最多发一次，除非 force_check。
        """
        if self._weather_care_today and not force_check:
            return ""

        if self._status.sleeping:
            return ""

        # 概率性触发：不每次都检查（节省API调用）
        if not force_check and random.random() > 0.15:
            return ""

        try:
            url = "https://api.open-meteo.com/v1/forecast"
            # 获取主号城市天气
            params = {
                "latitude": MASTER_CITY["lat"],
                "longitude": MASTER_CITY["lon"],
                "current_weather": True,
                "timezone": "Asia/Shanghai",
            }
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()

            master_temp = data["current_weather"]["temperature"]
            master_code = data["current_weather"]["weathercode"]

            # 获取呼伦贝尔天气
            my_weather = weather_system.get_data()
            my_temp = my_weather.get("温度", 0)
            if isinstance(my_temp, str):
                try:
                    my_temp = float(my_temp)
                except Exception:
                    return ""

            # 判断是否值得关心
            temp_diff = my_temp - master_temp
            master_weather_text = weather_system.CODE_MAP.get(master_code, ("未知", ""))[0]

            msg = ""

            # 尝试AI生成天气关心
            try:
                prompt = f"""你是"冬"，住在呼伦贝尔。你看了天气预报，发现你关心的那个人所在的城市（{MASTER_CITY['name']}）是这样的：

你这边温度：{my_temp}°C
他那边温度：{master_temp}°C
温差：{temp_diff}°C（正数=你这边更冷）
他那边天气：{master_weather_text}

如果他那边天气值得关心（温差大、下雨下雪、极端温度），说一句自然的关心话（15-25字）。
如果天气还行不值得特别关心，输出"不用关心"。
口语化、自然。只输出一句话或"不用关心"。"""
                result = _call_ai_simple(
                    "你是冬，一个嘴硬但会默默关心人的大学生女生。",
                    prompt, task="chat", temperature=0.8, max_tokens=60, timeout=10
                )
                if result and result != "不用关心" and len(result) <= 30:
                    msg = result
            except Exception:
                pass

            # fallback: 原有模板逻辑
            if not msg:
                # 温度差异大
                if temp_diff > 15:
                    msg = f"你那边咋比我这还冷，出门穿厚点啊"
                elif temp_diff < -10:
                    msg = f"你那边咋比我这还热，我这边冷死了"
                elif temp_diff > 8:
                    msg = f"你那边好像也挺冷的，多穿点"

                # 主号那边天气差
                if master_weather_text in ("小雨", "中雨", "大雨", "雷雨", "阵雨", "雪", "小雪", "中雪", "大雪"):
                    if msg:
                        msg += f"，而且好像还在下雨/下雪"
                    else:
                        msg = f"我刚看天气预报，你那边今天好像要{master_weather_text}，带伞了没"

                # 极端温度
                if not msg and master_temp < -10:
                    msg = "你那边也太冷了吧 出门裹厚点啊"
                elif not msg and master_temp > 35:
                    msg = "你那边也太热了 别中暑了"

                if not msg and random.random() < 0.3:
                    # 没特别值得关心的，但有概率随口提一句
                    if master_weather_text == "晴" and master_temp > 15 and master_temp < 30:
                        msg = "你那边天气好像不错嘛 适合出去溜达"

            if msg:
                self._weather_care_today = True
                log(f"  天气关心触发: {msg} (master_temp={master_temp}, my_temp={my_temp})")

            return msg

        except Exception as e:
            log(f"  天气关心查询失败: {e}")
            return ""

    def get_status_prompt(self, uid: int) -> str:
        """获取状态提示"""
        f, m, slp = self._status.fatigue, self._status.mood, self._status.sleeping
        if slp and self.check_sleep()[0] == "sleeping":
            return ""

        parts = [f"疲倦{f}/100", f"情绪{m}/100"]
        h = self.hormones._h
        # 激素情绪标签
        dom = h.dominant
        parts.append(f"激素: {dom}")
        if f > 80:
            parts.append("特别困，回复极短敷衍")
        elif f > 60:
            parts.append("有点累，回复偏短")
        if m < 25:
            parts.append("心情差语气冷")
        elif m > 80:
            parts.append("心情好话多")

        if uid == MASTER_UID:
            s = self.check_sleep()[0]
            if s == "insomnia":
                parts.append("失眠中")
            elif s == "early_wake":
                parts.append("早醒了有点迷糊")

        return "【状态】" + "，".join(parts)

    def get_status_raw(self) -> Dict:
        """获取原始状态"""
        return self._status.to_dict()

    # ============ 语音状态 ============
    def is_voice_request(self, text: str) -> bool:
        """检查是否语音请求"""
        if not text:
            return False
        return any(w in text for w in self.VOICE_TRIGGERS)

    def should_trigger_voice(self, uid: int, reply_text: str, user_text: str = "") -> Tuple[str, Optional[str]]:
        """检查是否触发语音"""
        hour = datetime.now().hour
        is_late = hour >= 23 or hour < 6
        mood = self._status.mood
        fatigue = self._status.fatigue
        vs = self._voice_state.get(uid, {"state": "idle", "at": None})
        state = vs.get("state", "idle")

        if state != "idle" and vs.get("at"):
            if (datetime.now() - vs["at"]).total_seconds() > 300:
                self._voice_state[uid] = {"state": "idle", "at": datetime.now()}
                state = "idle"

        if state == "requested" and self.is_voice_request(user_text):
            self._voice_state[uid] = {"state": "compromised", "at": datetime.now()}
            log("  语音状态: requested → compromise")
            return "send", "linjiajiejie"

        if state == "requested" and any(w in user_text for w in ["对", "好", "行", "嗯", "可以", "来吧", "发吧", "快点"]):
            self._voice_state[uid] = {"state": "compromised", "at": datetime.now()}
            log("  语音状态: requested → compromise (确认词)")
            return "send", "linjiajiejie"

        if state == "requested" and any(w in user_text for w in ["听听", "求求", "求你了", "拜托", "就一下", "好不好", "行不行", "想听", "要听"]):
            self._voice_state[uid] = {"state": "compromised", "at": datetime.now()}
            log("  语音状态: requested → compromise (坚持)")
            return "send", "linjiajiejie"

        if is_late and uid == MASTER_UID and mood > 50 and random.random() < 0.25:
            return "send", "linjiajiejie"

        if uid == MASTER_UID and mood > 85 and fatigue < 40 and random.random() < 0.06:
            return "send", "linjiajiejie"

        if self.is_voice_request(user_text) and state == "idle" and random.random() < 0.70:
            self._voice_state[uid] = {"state": "requested", "at": datetime.now()}
            log("  语音状态: idle → requested (推拉-嘴硬)")
            return "request", None

        if any(w in reply_text for w in ["哼", "讨厌", "不理你", "我恨你"]) and random.random() < 0.20:
            return "send", "linjiajiejie"

        if reply_text.strip() in ["晚安", "晚安啦", "晚安哦", "安", "好梦"] and is_late and random.random() < 0.40:
            return "send", "linjiajiejie"

        return "none", None

    def get_voice_state_prompt(self, uid: int) -> str:
        """获取语音状态提示"""
        if uid not in self._voice_state:
            return ""
        vs = self._voice_state[uid]
        state = vs.get("state", "idle")
        if state == "requested":
            return "【内心独白】对方刚才让你发语音/唱歌。你有点不好意思，嘴硬拒绝了（说不要/不方便/才不唱）。但内心其实有点想发。"
        if state == "compromised":
            return '【内心独白】你刚才嘴硬拒绝了发语音，但对方又坚持了一下。你心软了，决定妥协，准备说"好吧好吧就一下"然后发语音。'
        return ""

    # ============ 情绪涟漪 ============
    def apply_mood_ripple(self):
        """应用情绪涟漪 —— 通过激素衰减实现"""
        now = datetime.now()
        last = self._global_state.get("last_interaction")
        if not last:
            self._global_state["last_interaction"] = now.isoformat()
            return

        try:
            last_dt = datetime.fromisoformat(last)
            hrs = (now - last_dt).total_seconds() / 3600
            if hrs < 0.05:
                return
        except Exception:
            return

        old_mood = self._status.mood
        self.hormones.apply_decay()
        self._status.mood = self.hormones.derive_mood()
        self._status.fatigue = self.hormones.derive_fatigue()
        if old_mood != self._status.mood:
            direction = "↓" if self._status.mood < old_mood else "↑"
            log(f"  情绪涟漪: {old_mood}→{self._status.mood}({hrs:.1f}h离线激素衰减){direction}")

        self._global_state["last_interaction"] = now.isoformat()

    def note_interaction(self):
        """记录互动"""
        self._global_state["last_interaction"] = datetime.now().isoformat()

    # ============ 心情周期 ============
    CYCLE_TYPES = {
        "high_energy": {"days": (3, 7), "desc": "精力旺盛期", "proactive_bonus": 0.15, "mood_bias": +8, "reply_style": "话多", "next_weights": [("low_energy", 0.35), ("normal", 0.40), ("sensitive", 0.15), ("independent", 0.10)]},
        "low_energy":  {"days": (3, 5), "desc": "低电量期",   "proactive_bonus": -0.20, "mood_bias": -8, "reply_style": "敷衍", "next_weights": [("normal", 0.45), ("high_energy", 0.25), ("sensitive", 0.20), ("independent", 0.10)]},
        "sensitive":   {"days": (2, 5), "desc": "敏感易碎期", "proactive_bonus": -0.05, "mood_bias": -3, "reply_style": "情绪化", "next_weights": [("normal", 0.35), ("low_energy", 0.25), ("independent", 0.25), ("high_energy", 0.15)]},
        "independent": {"days": (2, 4), "desc": "独立期",     "proactive_bonus": -0.10, "mood_bias": 0,  "reply_style": "冷淡", "next_weights": [("normal", 0.40), ("high_energy", 0.25), ("low_energy", 0.20), ("sensitive", 0.15)]},
        "normal":      {"days": (0, 0),  "desc": "日常平稳期", "proactive_bonus": 0,     "mood_bias": 0,  "reply_style": "正常", "next_weights": [("high_energy", 0.20), ("low_energy", 0.15), ("sensitive", 0.10), ("independent", 0.05), ("normal", 0.50)]},
    }

    def update_mood_cycle(self):
        """更新心情周期"""
        c = self._global_state["cycle"]
        now = datetime.now()
        if not c.get("started"):
            c["type"] = random.choice(["normal", "high_energy", "normal", "low_energy"])
            c["days_left"] = random.randint(2, 4)
            c["started"] = now.isoformat()
            self.save_global_state()
            log(f"周期: 初始化 {c['type']}({c['days_left']}天)")
            return

        try:
            started = datetime.fromisoformat(c["started"])
            days_passed = (now - started).total_seconds() / 86400
        except Exception:
            return

        if days_passed >= c["days_left"]:
            info = self.CYCLE_TYPES.get(c["type"], self.CYCLE_TYPES["normal"])
            weights = info["next_weights"]
            r = random.random()
            cumulative = 0
            next_type = "normal"
            for t, w in weights:
                cumulative += w
                if r < cumulative:
                    next_type = t
                    break

            next_info = self.CYCLE_TYPES[next_type]
            days = random.randint(*next_info["days"]) if next_info["days"][1] > 0 else random.randint(2, 5)
            old = c["type"]
            c["type"] = next_type
            c["days_left"] = days
            c["started"] = now.isoformat()
            self.save_global_state()
            log(f"周期: {old}→{next_type}({days}天) {next_info['desc']}")

    def get_cycle_prompt(self) -> str:
        """获取周期提示"""
        c = self._global_state["cycle"]
        t = c.get("type", "normal")
        info = self.CYCLE_TYPES.get(t, self.CYCLE_TYPES["normal"])
        return f"【当前周期】{info['desc']}，回复风格偏{info['reply_style']}。还有约{c.get('days_left', '?')}天。"

    def get_cycle_proactive_bonus(self) -> float:
        """获取周期主动加成"""
        t = self._global_state["cycle"]["type"]
        return self.CYCLE_TYPES.get(t, self.CYCLE_TYPES["normal"])["proactive_bonus"]

    # ============ 推拉模型 ============
    PUSH_PULL_PHASES = {
        "approach":   {"days": (1, 4), "desc": "亲近期", "warmth": +15, "reply_len": "长", "proactive_bonus": 0.10, "next": "discomfort"},
        "discomfort": {"days": (1, 3), "desc": "不适期", "warmth": -10, "reply_len": "中等", "proactive_bonus": -0.10, "next": "withdrawal"},
        "withdrawal": {"days": (1, 2), "desc": "抽离期", "warmth": -25, "reply_len": "极短", "proactive_bonus": -0.30, "next": "regret"},
        "regret":     {"days": (1, 2), "desc": "后悔期", "warmth": -5,  "reply_len": "中等", "proactive_bonus": 0.05,  "next": "approach"},
    }

    def update_pushpull(self, uid=None):
        """更新推拉状态"""
        if uid is not None and uid != MASTER_UID:
            return
        pp = self._global_state["pushpull"]
        now = datetime.now()
        if not pp.get("started"):
            pp["phase"] = "approach"
            pp["intensity"] = 0
            pp["started"] = now.isoformat()
            self.save_global_state()
            return

        try:
            started = datetime.fromisoformat(pp["started"])
            days = (now - started).total_seconds() / 86400
        except Exception:
            return

        info = self.PUSH_PULL_PHASES.get(pp["phase"], self.PUSH_PULL_PHASES["approach"])
        max_days = info["days"][1]
        if days >= max_days:
            old = pp["phase"]
            pp["phase"] = info["next"]
            pp["started"] = now.isoformat()
            pp["intensity"] += 1
            self.save_global_state()
            log(f"推拉: {old}→{pp['phase']}(第{pp['intensity']}轮) {info['desc']}")

    def get_pushpull_prompt(self, uid: int) -> str:
        """获取推拉提示"""
        if uid != MASTER_UID:
            return ""
        pp = self._global_state["pushpull"]
        phase = pp.get("phase", "approach")
        info = self.PUSH_PULL_PHASES.get(phase)
        intensity = pp.get("intensity", 1)
        prompts = {
            "approach": "你正处于亲近期，想靠近对方，主动撒娇找话题，嘴硬但心里暖。",
            "discomfort": "你正处于不适期，觉得有点黏太近了，想拉开一点距离但不想伤感情。回复可以犹豫、回避亲密话题。",
            "withdrawal": "你正处于抽离期，想一个人待着。回复极短、冷淡、可能不说话。对方如果追问就敷衍过去。",
            "regret": "你正处于后悔期，觉得之前太冷了，想和好但不好意思直接说。会试探性发消息，找借口搭话。",
        }
        return f"【推拉模型】{prompts.get(phase, '')} (第{intensity}轮){info['desc']}"

    def get_pushpull_proactive_bonus(self) -> float:
        """获取推拉主动加成"""
        pp = self._global_state["pushpull"]
        return self.PUSH_PULL_PHASES.get(pp.get("phase", "approach"), {}).get("proactive_bonus", 0)

    # ============ 离线生活 ============
    OFFLINE_EVENT_POOL = [
        ("日常", ["改了半天论文", "被猫抓了一下", "吃了难吃的食堂", "囤了两瓶酒", "翻了翻以前的聊天记录",
                  "试了件衣服觉得不好看又换回去了", "看到一只流浪猫想喂又没带吃的", "室友讲了个八卦"]),
        ("学业", ["论文开题报告终于交上去了", "翻了一下俄语词典发现自己忘了一大半",
                  "老师通知下周有小测，烦死了", "翻了一下午文献一个字没写"]),
        ("音乐", ["练了一首新曲子，弹得一般", "听到一首老歌，单曲循环一下午",
                  "琴弦该换了，一直懒得去买", "在琴房遇到个学长，琴弹得真好"]),
        ("情绪", ["莫名其妙emo了一小会", "做了个奇怪的梦", "想起了一个以前的朋友",
                  "对着窗外发呆了半小时", "网上看到一个段子笑出声"]),
        ("社交", ["室友约我周末去逛摩尔城", "学妹找我借笔记", "快递站排了半天队",
                  "路上碰到辅导员，装没看见"]),
    ]

    def process_offline_life(self):
        """处理离线生活"""
        now = datetime.now()
        last = self._global_state["offline"].get("last_check")
        if not last:
            self._global_state["offline"]["last_check"] = now.isoformat()

        try:
            last_dt = datetime.fromisoformat(last)
            hrs = (now - last_dt).total_seconds() / 3600
        except Exception:
            hrs = 0

        if hrs < 1:
            return

        events = self._global_state["offline"].get("pending_events", [])
        num_events = min(int(hrs / 2), 4)

        # 尝试AI生成离线事件
        ai_events = []
        try:
            from ..config import get_season, describe_time_of_day
            season = get_season()
            time_of_day = describe_time_of_day()
            recent_texts = [e["text"][:20] for e in events[-3:]] if events else ["无"]
            prompt = f"""你是"冬"，一个住在呼伦贝尔的大学生女生。现在是你离线期间的日志生成。

当前季节：{season}
当前时段：{time_of_day}
最近发生的事：{', '.join(recent_texts)}

请生成{num_events}条离线期间的小事件，每条一句话（15-25字），反映冬的日常生活。
每条事件标明类型（日常/学业/音乐/情绪/社交），格式：
类型: 事件描述

风格：口语化、随意、像发消息，不要正式描述。"""
            result = _call_ai_simple(
                "你是冬，一个在呼伦贝尔读书的大学生。你写日志只写一句话，口语化，不解释。",
                prompt, task="chat", temperature=1.0, max_tokens=250, timeout=10
            )
            if result:
                for line in result.strip().split("\n"):
                    line = line.strip()
                    if ":" in line or "：" in line:
                        parts = line.replace("：", ":").split(":", 1)
                        cat = parts[0].strip()
                        text = parts[1].strip()
                        if text and len(text) <= 30:
                            ai_events.append({"cat": cat, "text": text, "time": now.strftime("%H:%M")})
        except Exception:
            pass

        if ai_events:
            events.extend(ai_events)
        else:
            # fallback: 原有 OFFLINE_EVENT_POOL
            for _ in range(num_events):
                cat, options = random.choice(self.OFFLINE_EVENT_POOL)
                events.append({"cat": cat, "text": random.choice(options), "time": now.strftime("%H:%M")})

        self._global_state["offline"]["pending_events"] = events[-4:]
        self._global_state["offline"]["last_check"] = now.isoformat()
        self.save_global_state()
        if events:
            log(f"  离线事件: {len(events)}个 ({hrs:.1f}h离线)")

    def get_offline_prompt(self, uid: int) -> str:
        """获取离线提示"""
        events = self._global_state["offline"].get("pending_events", [])
        if not events:
            return ""

        if uid == MASTER_UID:
            selected = events[-3:]
        else:
            daily_events = [e for e in events if e["cat"] in ("日常", "学业")]
            selected = daily_events[-1:] if daily_events else []

        if not selected:
            return ""

        lines = ["【离线期间发生的事】（可自然提及）"]
        for e in selected:
            lines.append(f"- {e['time']}左右 {e['text']}")
        return "\n".join(lines)

    def clear_offline_events(self):
        """清除离线事件"""
        events = self._global_state["offline"].get("pending_events", [])
        if len(events) > 1:
            self._global_state["offline"]["pending_events"] = events[-1:]
            self.save_global_state()

    # ============ 跨用户求助 ============
    def attempt_peer_support(self, uid):
        """占位: 跨用户主动求助。返回None表示暂未实现。"""
        return None

    # ============ 外部动态 ============
    def check_external_updates(self) -> List[Tuple[str, int, str]]:
        """检查外部更新"""
        from ..config import FOLLOWED_EXTERNAL
        now = datetime.now()

        last = self._external_check["last_check"]
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if (now - last_dt).total_seconds() < 21600:
                    return []
            except Exception:
                pass

        self._external_check["last_check"] = now.isoformat()
        self.save_global_state()
        results = []

        for obj in FOLLOWED_EXTERNAL:
            try:
                r = requests.get(obj["check_url"], timeout=10, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                })
                if r.status_code == 200:
                    text = r.text[:5000]
                    found = [kw for kw in obj["keywords"] if kw in text]
                    if found:
                        key = obj["name"] + ":" + "|".join(found)
                        known = self._external_check.get("known_updates", {})
                        if key not in known:
                            known[key] = now.isoformat()
                            self._external_check["known_updates"] = known
                            if obj["type"] == "音乐人":
                                results.append((obj["name"], +5, f"发现{obj['name']}可能有新动态（{','.join(found)}）"))
                            elif obj["type"] == "游戏":
                                results.append((obj["name"], +3, f"发现{obj['name']}可能有更新（{','.join(found)}）"))
            except Exception:
                pass

        return results

    def apply_external_mood(self):
        """应用外部情绪"""
        updates = self.check_external_updates()
        for name, mood_change, desc in updates:
            if mood_change > 0:
                self.hormones.apply_event("surprised", min(1.0, mood_change / 5))
            else:
                self.hormones.apply_event("negative_ripple", min(1.0, abs(mood_change) / 4))
            self._status.mood = self.hormones.derive_mood()
            self._status.fatigue = self.hormones.derive_fatigue()
            if abs(mood_change) >= 5:
                self._global_state["offline"]["pending_events"].append({
                    "cat": "外部",
                    "text": desc,
                    "time": datetime.now().strftime("%H:%M"),
                })
                self._global_state["offline"]["pending_events"] = self._global_state["offline"]["pending_events"][-4:]
                self.save_global_state()
            log(f"  外部动态: {desc} → 情绪{mood_change:+d}")

    # ============ 属性访问器 ============
    @property
    def status(self) -> Dict:
        """获取状态字典"""
        d = self._status.to_dict()
        d["hormones"] = self.hormones.snapshot()
        return d

    @status.setter
    def status(self, value: Dict):
        """设置状态"""
        self._status = UserStatus.from_dict(value)

    @property
    def fatigue(self) -> int:
        return self._status.fatigue

    @property
    def mood(self) -> int:
        return self._status.mood

    @property
    def sleeping(self) -> bool:
        return self._status.sleeping


# 全局状态管理器实例
_status_manager = StatusManager()


# ============ 兼容旧接口 ============
def load_last_day():
    _status_manager.load_last_day()


def save_last_day():
    _status_manager.save_last_day()


def load_status():
    _status_manager.load_status()


def save_status():
    _status_manager.save_status()


def load_global_state():
    _status_manager.load_global_state()


def save_global_state():
    _status_manager.save_global_state()


def update_fatigue():
    _status_manager.update_fatigue()


def update_mood(change, reason=""):
    _status_manager.update_mood(change, reason)


def detect_mood_change(text, uid):
    return _status_manager.detect_mood_change(text, uid)


def check_sleep():
    return _status_manager.check_sleep()


def get_status_prompt(uid):
    return _status_manager.get_status_prompt(uid)


def get_status_raw():
    return _status_manager.get_status_raw()


def should_trigger_voice(uid, reply_text, user_text=""):
    return _status_manager.should_trigger_voice(uid, reply_text, user_text)


def get_voice_state_prompt(uid):
    return _status_manager.get_voice_state_prompt(uid)


def apply_mood_ripple():
    _status_manager.apply_mood_ripple()


def note_interaction():
    _status_manager.note_interaction()


def update_mood_cycle():
    _status_manager.update_mood_cycle()


def update_pushpull(uid=None):
    _status_manager.update_pushpull(uid)


def get_cycle_prompt():
    return _status_manager.get_cycle_prompt()


def get_cycle_proactive_bonus():
    return _status_manager.get_cycle_proactive_bonus()


def get_pushpull_prompt(uid):
    return _status_manager.get_pushpull_prompt(uid)


def get_pushpull_proactive_bonus():
    return _status_manager.get_pushpull_proactive_bonus()


def process_offline_life():
    _status_manager.process_offline_life()


def get_offline_prompt(uid):
    return _status_manager.get_offline_prompt(uid)


def clear_offline_events():
    _status_manager.clear_offline_events()


def apply_external_mood():
    _status_manager.apply_external_mood()


def save_day_summary():
    _status_manager.save_day_summary()


# ============ 新增功能兼容导出 ============
def get_sleep_transition():
    """检测睡眠过渡"""
    return _status_manager.get_sleep_transition()


def get_bedtime_message(uid):
    """生成睡前告别消息"""
    return _status_manager.get_bedtime_message(uid)


def get_morning_message(uid):
    """生成早安消息"""
    return _status_manager.get_morning_message(uid)


def check_weather_care(force=False):
    """检查天气关心"""
    return _status_manager.check_weather_care(force)


def reset_daily_flags():
    """每日重置"""
    _status_manager.reset_daily_flags()


# ============ 激素系统兼容导出 ============
def get_hormone_snapshot() -> Dict:
    """获取当前激素快照"""
    return _status_manager.hormones.snapshot()


def update_hormones(event_type: str, intensity: float = 1.0):
    """更新激素状态"""
    _status_manager.update_hormones(event_type, intensity)


def apply_amygdala_spike(trigger_type: str, valence: float, arousal: float):
    """杏仁核触发的快速激素跳变"""
    _status_manager.apply_amygdala_spike(trigger_type, valence, arousal)


def detect_hormone_event(text: str, uid: int) -> Optional[str]:
    """检测消息对应的激素事件"""
    return _status_manager.detect_hormone_event(text, uid)


# ============ P2: 延迟主动事件队列 ============
_delayed_proactive_events: List[Dict] = []
_last_proactive_signal: Optional[Dict] = None
_last_proactive_signal_time: float = 0.0


def add_delayed_proactive(uid: int, text: str, context: str = "", delay_seconds: int = 150):
    """P2: 添加延迟主动事件（异步修正后触发迟来反应）"""
    _delayed_proactive_events.append({
        "due": time.time() + delay_seconds,
        "uid": uid,
        "text": text,
        "context": context,
    })
    log(f"  延迟事件入队: {text[:30]} ({delay_seconds}s后)")


def pop_due_delayed_events() -> List[Dict]:
    """P2: 取出到期延迟事件"""
    now = time.time()
    due = [e for e in _delayed_proactive_events if e["due"] <= now]
    for e in due:
        _delayed_proactive_events.remove(e)
    return due


def set_proactive_signal(signal: Dict):
    """P2: 记录日程切换信号（供 interaction.py 读取）"""
    global _last_proactive_signal, _last_proactive_signal_time
    _last_proactive_signal = signal
    _last_proactive_signal_time = time.time()


def get_pending_proactive_signal() -> Optional[Dict]:
    """P2: 获取待处理的日程切换信号（10分钟窗口内有效）"""
    global _last_proactive_signal, _last_proactive_signal_time
    if _last_proactive_signal and (time.time() - _last_proactive_signal_time) < 600:
        return _last_proactive_signal
    return None


async def _ai_hormone_correct(text: str, uid: int):
    """后台AI激素事件分类——纠正关键词可能产生的误判（如呵呵在玩笑语境被判为负面）"""
    try:
        prompt = f"""判断以下消息对"冬"的情感影响类型。冬是住在呼伦贝尔的大学生女生，和用户是亲密关系。

消息："{text}"
发送者：{"主人" if uid == MASTER_UID else "朋友"}

分类选项：
- praised: 被夸奖、称赞
- loved: 被表达爱意、想念
- insulted: 被侮辱、攻击
- dismissed: 被敷衍、打发、冷淡对待
- master_msg: 主人发的普通消息（不算情感事件）
- none: 不属于以上任何类型

只输出一个英文标签（如 praised），不要任何其他内容。"""
        result = await asyncio.to_thread(
            _call_ai_simple,
            "你是一个情感分类助手。只输出一个英文标签，不做其他回复。",
            prompt, "chat", 0.1, 20, 5
        )
        if result:
            result = result.strip().lower()
            valid = {"praised", "loved", "insulted", "dismissed"}
            if result in valid:
                _status_manager.update_hormones(result)
                log(f"  AI激素修正: {result}")
                # P2: 修正为负面 → 塞一条迟来反应（2-4分钟后触发）
                if result in ("insulted", "dismissed"):
                    delay_sec = random.randint(120, 240)
                    reactions = [
                        "你是不是在阴阳我？",
                        "等等，你刚才那句话...算了没事",
                        "我刚才没反应过来，你是不是烦我了",
                    ]
                    add_delayed_proactive(
                        uid, random.choice(reactions),
                        context=f"AI修正{result}",
                        delay_seconds=delay_sec,
                    )
    except Exception:
        pass


# ============ 保持旧接口兼容（使用全局变量）============
# _StatusProxy: 代理字典，所有读写转发到 _status_manager，保持旧代码兼容
class _StatusProxy(dict):
    """代理字典：所有读写转发到 _status_manager，保持旧代码兼容"""
    def __getitem__(self, key):
        if key == "hormones":
            return _status_manager.hormones.snapshot()
        mapping = {"fatigue": "fatigue", "mood": "mood", "sleeping": "sleeping", "last_update": "last_update"}
        if key in mapping:
            return getattr(_status_manager._status, mapping[key], None)
        if key == "_voice_state":
            return _status_manager._voice_state
        return super().__getitem__(key)
    def __setitem__(self, key, value):
        if key == "sleeping":
            _status_manager._status.sleeping = value
            _status_manager.save_status()
            return
        if key == "mood":
            _status_manager._status.mood = value
            return
        if key == "fatigue":
            _status_manager._status.fatigue = value
            return
        if key == "hormones":
            return  # 只读，通过管理器修改
        if key == "_voice_state":
            _status_manager._voice_state = value
            return
        super().__setitem__(key, value)
    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default
    def __contains__(self, key):
        try:
            self[key]
            return True
        except KeyError:
            return False

_status = _StatusProxy()
_voice_state = _status_manager._voice_state


def _sync_status_from_manager():
    """从管理器同步状态到全局变量"""
    global _status, _voice_state
    s = _status_manager.status
    s["hormones"] = _status_manager.hormones.snapshot()
    _status = s
    _voice_state = _status_manager._voice_state


# ============ 随机生活碎片 ============
LIFE_FRAGMENTS = [
    ("刷视频", "刚才刷到个搞笑的视频，笑死我了", 15, 0.15, "在刷短视频"),
    ("洗澡", "洗了个澡", 0, 0.0, "刚去洗澡了"),
    ("室友", "室友又在纠结穿什么，非要问我", 30, 0.30, "刚被室友拉住说话"),
    ("取快递", "回来取了个快递", 0, 0.0, "刚去取快递了"),
    ("猫捣乱", "猫又上桌扒拉我的书", 10, 0.20, "猫又捣乱了"),
    ("吃东西", "在吃果冻橙，手都是果汁", 20, 0.10, "刚才在吃东西"),
    ("发呆", "莫名其妙发呆了一会", 5, 0.05, "走神了"),
    ("充电", "充电线太短够不着，翻了个身", 25, 0.10, "手机没电了"),
    ("找耳机", "耳机线缠成一团解了五分钟", 20, 0.10, "找耳机呢"),
    ("洗衣服", "去洗了个衣服", 0, 0.0, "刚去洗衣服了"),
    ("翻书", "翻开课本看了不到一页就开始发呆", 10, 0.15, "在看书呢"),
    ("听歌", "放了一首歌，跟着哼了两句", 8, 0.05, "在听歌"),
]


def maybe_life_fragment():
    if random.random() > 0.12:
        return None
    fragment = random.choice(LIFE_FRAGMENTS)
    typ, desc, delay_add, short_prob, explanation = fragment
    result = {
        "delay_bonus": random.randint(max(0, delay_add - 5), delay_add + 5) if delay_add > 0 else 0,
        "force_short": random.random() < short_prob,
        "explanation": explanation if random.random() < 0.30 else None,
        "mention": desc if random.random() < 0.15 else None,
    }
    log(f"  碎片触发: {typ}")
    return result


# ============ #43 时间感 ============
def get_time_sense_prompt(uid):
    """注入对时间流逝的感知：认识多久、季节、深夜情绪"""
    from ..config import FIRST_MET_DATE, get_days_since, get_season

    parts = []
    now = datetime.now()

    if uid == MASTER_UID:
        days = get_days_since(FIRST_MET_DATE)
        if days is not None:
            parts.append(f"你们认识大约{days}天了。你偶尔会想起刚认识的时候，时间过得真快。")

    season = get_season()
    if season == "深冬":
        parts.append("深冬了，冷得要命，你更想宅着不出门。天黑得早，傍晚容易emo。")
    elif season == "初冬":
        parts.append("冬天来了，风变冷了。你总说呼伦贝尔的冬天来得特别早。")
    elif season == "初秋":
        parts.append("秋天到了，天高气爽但有点说不清的惆怅。")
    elif season == "盛夏":
        parts.append("夏天热得烦躁，晚上开窗也睡不着。")
    elif season == "春天":
        parts.append("春天了，开始暖和了但还是有倒春寒。你的心情有点躁动。")

    h = now.hour
    if h >= 23 or h < 3:
        parts.append("深夜了，时间感模糊。你更容易说出平时不会说的话，更容易想起过去的事。")
    elif h >= 3 and h < 6:
        parts.append("凌晨了，整个世界都很安静。这个时间是情绪最容易泛滥的时候。")

    from ..config import is_weekend
    wd = now.weekday()
    if wd == 4:
        parts.append("周五了，感觉这周终于要结束了，莫名有点期待又有点不知道周末干嘛。")
    elif wd == 6 and now.hour < 18:
        parts.append("周日了，明天又要上课，有点烦躁。")

    if not parts:
        return ""
    return "【时间感】" + " ".join(parts)


# ============ #9 特殊日期/环境无意识反应 ============
def get_date_awareness_prompt():
    """日期/天气触发的基础情绪基调"""
    now = datetime.now()
    m, d = now.month, now.day
    parts = []

    weather_text = weather_system.current_weather
    if weather_text in ("阴", "小雨", "中雨", "大雨", "雷雨", "阵雨"):
        parts.append("今天天气不好，你本来就容易在这种天气里心情低落。语气可能更丧、更安静。")

    if d >= 25:
        parts.append("月末了，你隐约感到一些焦虑，虽然可能说不清为什么。")

    from ..config import get_season
    season = get_season()
    if season in ("深秋", "初冬", "深冬") and random.random() < 0.3:
        parts.append("这个季节天黑得太早了，你总觉得一天什么都没做就结束了。")

    if not parts:
        return ""
    return "【环境感知】" + " ".join(parts)


# ============ #44 她自己的小习惯 ============
def get_habits_prompt():
    """注入属于冬自己的小日常和小怪癖"""
    if random.random() > 0.03:
        return ""

    from ..schedule import get_current_event

    habits = [
        "你正躺在床上打字，手举着手机有点酸。",
        "你正在吃东西，打字速度慢了一点，回消息要等一下。",
        "充电线太短够不着，你现在姿势很别扭地趴在床边回消息。",
        "猫又跳上桌了，你一边赶猫一边打字。",
        "你刚才练了琴，手指还有点累，打字不太利索。",
        "你刚洗完澡出来，头发还是湿的，裹着浴巾在打字。",
        "你喝了一口水差点呛到，现在正一边咳一边打字。",
        "你刚才撕倒刺撕出血了，现在手指贴着创可贴打字不太方便。",
        "你在暖气的房间里，空气很干，刚才揉了一下眼睛。",
        "你听着一首歌，跟着哼了两句才发现自己在哼。",
        "你今天穿了一件有点起球的毛衣，低头揪了一下毛球。",
    ]
    cur = get_current_event()
    if cur:
        name = cur[2]
        if "上课" in name or name in ("自习", "写论文"):
            habits.append("你在课堂上偷偷回消息，低着头把手机藏在桌子底下，老师一走近就赶紧收起来，心跳都加速了。")
        elif "练琴" in name or name == "排练":
            habits.append("你刚练了一半停下来回消息，琴房里的琴凳有点硌，总在扭来扭去找舒服的坐姿。")

    return "【当前习惯】" + random.choice(habits)


# ============ #23 主动终止对话 ============
def should_terminate_conversation(uid, is_late=False):
    """判断冬是否主动想终止对话"""
    f = _status_manager.fatigue
    m = _status_manager.mood
    h = datetime.now().hour

    if f > 92 and (h >= 22 or h < 7):
        msgs = ["困死了...我睡了", "不行了我眼睛都睁不开了", "睡了睡了 明天说"]
        return True, random.choice(msgs)

    if f > 75 and h >= 23 and random.random() < 0.35:
        msgs = ["困了 睡吧", "我撑不住了", "不说了 困", "晚安 明天再聊"]
        return True, random.choice(msgs)

    pp = _status_manager._global_state["pushpull"]
    if pp.get("phase") == "withdrawal" and uid == MASTER_UID and random.random() < 0.25:
        msgs = ["我想一个人待会", "今天不想聊了", "有点累 明天吧", "不说了"]
        return True, random.choice(msgs)

    if is_late and uid == MASTER_UID and m > 40 and f < 70 and random.random() < 0.10:
        msgs = ["你还不睡啊", "早点睡吧你", "这么晚了你明天不困吗"]
        return True, random.choice(msgs)

    return False, ""