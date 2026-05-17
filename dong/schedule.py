"""
冬 · 详细日程系统
- 固定周课表 + AI 生成自由时间活动
- 起床/入睡锚点事件
- 三餐浮动窗口 + 决策链
- 事件切换时 AI 填充具体细节（每天3-5次）
- 过渡微事件随机池
- 日常归档 + 昨日状态快照

设计原则：能用 AI 的地方不硬编码，让她像真人一样有每一天的独特感。
"""
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

from .config import (
    is_weekend, is_late_night, BASE_DIR,
    MASTER_UID, ALLOWED_USERS
)
from .log import log
from .core.data_healing import FieldSpec, heal_any

# ============ 文件路径 ============
SCHEDULE_FILE = os.path.join(BASE_DIR, "dong_today_schedule.json")
ARCHIVE_DIR = os.path.join(BASE_DIR, "dong_schedule_archive")
try:
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
except OSError:
    pass


# ============ 固定周课表（俄语专业大四）============
# 格式: 星期几 → [(开始, 结束, 课名, 地点, 标签)]
WEEKLY_CLASSES = {
    0: [("08:00", "09:40", "俄语高级笔译", "外语楼302", "专业课严"),
        ("10:00", "11:40", "俄罗斯文学赏析", "外语楼205", "老师有趣")],
    1: [("08:30", "10:00", "俄语口译实务", "综合楼401", "小班课点名"),
        ("15:30", "17:10", "俄语语言学概论", "综合楼303", "难但要听")],
    2: [("08:00", "09:40", "俄汉互译理论", "外语楼301", "有点水"),
        ("10:00", "11:40", "论文写作指导", "综合楼210", "要交开题报告"),
        ("13:30", "15:10", "俄罗斯文化史", "外语楼阶梯教室", "大课无聊")],
    3: [("08:00", "09:40", "俄语高级笔译", "外语楼302", "专业课严"),
        ("10:00", "11:40", "二外英语", "外语楼201", "必修")],
    4: [("08:30", "10:00", "俄语口译实务", "综合楼401", "小班课点名"),
        ("15:30", "17:10", "俄罗斯文化史", "外语楼阶梯教室", "大课无聊")],
    5: [],  # 周六
    6: [],  # 周日
}


# ============ 过渡微事件池（不用 API 的小随机）============
MICRO_EVENTS_CAMPUS = [
    "路过花坛看见一只橘猫蹲在台阶上晒太阳",
    "听到有人在琴房弹《梦中的婚礼》，弹得磕磕巴巴的",
    "室友发了张搞笑表情包，笑出声",
    "风吹过来有点凉，把外套拉链往上拉了拉",
    "看到一对情侣在教学楼门口腻歪，翻了个白眼",
    "踩到一片落叶，咔嚓一声",
    "路过奶茶店犹豫了三秒还是走了",
    "看了一眼手机发现没新消息，又放回兜里",
    "有个外卖骑手骑车太快差点撞到人",
    "听见两个学妹在用俄语念课文，发音惨不忍睹",
    "发现今天穿错袜子颜色了，但无所谓",
    "路边有人在发健身房传单，绕开了",
    "闻到食堂飘过来的饭香味，肚子叫了一下",
    "收到一条推送说有新番更新了",
    "摸兜发现润唇膏不见了，应该是掉在教室了",
    "看到墙上贴了个考研讲座的海报",
    "有个老师在走廊打电话，声音很大",
    "图书馆门口排了很长的队",
]

MICRO_EVENTS_DORM = [
    "室友在看剧，外放的声音有点大",
    "躺在床上刷了五分钟短视频才起来",
    "发现充电线又被室友借走了",
    "窗外有人在喊谁的名字，听不清",
    "桌子上有半包没吃完的薯片，犹豫要不要继续吃",
    "打了个喷嚏，室友说了句'你感冒了？'",
    "空调温度好像不太对，调了一下",
    "看到镜子里的自己，头发有点乱，捋了两下",
    "手机弹出电量不足20%的提醒",
]

MICRO_EVENTS_EVENING = [
    "走廊里有女生在打电话，声音断断续续的",
    "楼下有人在弹吉他，弹的是周杰伦的老歌",
    "刷到一个很好笑的视频，看了三遍",
    "想起今天还有件事没做，但死活想不起来是什么",
    "打了个哈欠，眼泪出来了",
    "手指碰到琴弦有点凉",
]


# ============ 三餐选项池 ============
BREAKFAST_OPTIONS = ["食堂的包子豆浆", "面包牛奶随便对付", "买了个饭团边走边吃",
                     "室友带的茶叶蛋", "昨晚剩的半块蛋糕", "泡了杯麦片"]
LUNCH_DINNER_OPTIONS = ["食堂一楼的套餐", "食堂二楼的铁板饭", "外卖点了份黄焖鸡",
                        "室友帮带了一份盖浇饭", "去校外吃了碗面",
                        "外卖点了麻辣烫", "食堂新开的窗口试了一下"]
MEAL_SKIP_REASONS = {  # 吃 or 不吃的微理由
    "eat": ["确实有点饿了", "虽然不饿但到点了", "室友喊一起"],
    "skip": ["好像不太饿", "课间太短来不及", "懒得下楼"],
}


# ============ 数据类 ============
@dataclass
class MealDecision:
    """一顿饭的决策结果"""
    meal_type: str = ""          # breakfast/lunch/dinner
    decided: bool = False        # 是否吃
    location: str = ""           # 在哪吃
    with_whom: str = ""          # 和谁
    what: str = ""               # 吃了什么
    detail: str = ""             # AI/模板填充的细节


@dataclass
class ScheduleActivity:
    """日程中的一个活动"""
    start_time: str = ""         # "HH:MM"
    end_time: str = ""
    name: str = ""
    activity_type: str = ""      # wake/sleep/class/meal/free/transit
    location: str = ""
    tag: str = ""                # 情绪标签/备注
    detail: str = ""             # AI 填充的当日具体细节
    reply_mode: str = "ok"      # ok/short/no/delayed
    meal_decision: Optional[MealDecision] = None  # 如果是用餐类型

    @property
    def time_range(self) -> str:
        return f"{self.start_time}-{self.end_time}"

    def to_dict(self) -> Dict:
        d = {
            "start": self.start_time, "end": self.end_time,
            "name": self.name, "type": self.activity_type,
            "location": self.location, "tag": self.tag,
            "detail": self.detail, "reply": self.reply_mode
        }
        if self.meal_decision:
            d["meal"] = {
                "type": self.meal_decision.meal_type,
                "decided": self.meal_decision.decided,
                "what": self.meal_decision.what,
                "with": self.meal_decision.with_whom,
                "detail": self.meal_decision.detail,
            }
        return d

    @staticmethod
    def from_dict(d: Dict) -> "ScheduleActivity":
        md = None
        if "meal" in d:
            md = MealDecision(
                meal_type=d["meal"].get("type", ""),
                decided=d["meal"].get("decided", False),
                what=d["meal"].get("what", ""),
                with_whom=d["meal"].get("with", ""),
                detail=d["meal"].get("detail", ""),
            )
        return ScheduleActivity(
            start_time=d.get("start", ""), end_time=d.get("end", ""),
            name=d.get("name", ""), activity_type=d.get("type", ""),
            location=d.get("location", ""), tag=d.get("tag", ""),
            detail=d.get("detail", ""), reply_mode=d.get("reply", "ok"),
            meal_decision=md,
        )


@dataclass
class DaySchedule:
    """完整的一天日程"""
    date: str = ""
    weekday: int = 0
    is_weekend: bool = False
    wake_time: str = ""
    sleep_time: str = ""
    activities: List[ScheduleActivity] = field(default_factory=list)
    meals: Dict[str, MealDecision] = field(default_factory=dict)
    micro_events: List[str] = field(default_factory=list)
    prev_day_context: str = ""   # 昨天怎么了（影响今天）
    ai_fill_count: int = 0       # 今天已填充几次细节

    def sort(self):
        self.activities.sort(key=lambda a: a.start_time)


# ============ API 调用辅助 ============
def _call_schedule_ai(system_prompt: str, user_prompt: str) -> str:
    """日程专用的 AI 调用——通过统一网关。"""
    from .core.api_gateway import gateway
    result = gateway.call_simple(
        system_prompt, user_prompt,
        task="chat",
        temperature=1.0,
        max_tokens=400,
        timeout=25,
    )
    return result or ""


# ============ 日程系统 ============
class ScheduleSystem:
    """详细日程管理器"""

    def __init__(self):
        self.today: Optional[DaySchedule] = None
        self._prev_activity: Optional[ScheduleActivity] = None
        self._interaction_ids: Dict[str, List[ScheduleActivity]] = {}  # uid → 互动记录

    # ----- 生成全天日程 -----
    def generate_today(self, prev_sleep_quality: str = "一般",
                       prev_mood_residue: str = "",
                       prev_unfinished: str = "",
                       current_mood: int = 50,
                       current_fatigue: int = 50,
                       cycle_phase: str = "日常") -> DaySchedule:
        """
        每日清晨调用：生成今天的完整日程。

        Args:
            prev_sleep_quality: 昨晚睡眠质量（好/一般/失眠/熬夜）
            prev_mood_residue: 昨天的情绪残留描述
            prev_unfinished: 昨天未完成的事项
            current_mood: 当前心情 0-100
            current_fatigue: 当前疲劳 0-100
            cycle_phase: 当前周期阶段
        """
        now = datetime.now()
        weekday = now.weekday()
        weekend = is_weekend(now)
        today_str = now.strftime("%Y-%m-%d")

        day = DaySchedule(
            date=today_str,
            weekday=weekday,
            is_weekend=weekend,
            prev_day_context=self._build_prev_context(prev_sleep_quality, prev_mood_residue, prev_unfinished),
        )

        # 1. 确定起床和入睡时间
        day.wake_time, day.sleep_time = self._generate_wake_sleep(weekend, prev_sleep_quality, current_mood, current_fatigue)

        # 2. 添加起床事件（短，给早餐腾空间）
        day.activities.append(ScheduleActivity(
            start_time=day.wake_time,
            end_time=self._add_minutes(day.wake_time, 5),
            name="起床",
            activity_type="wake",
            location="宿舍",
            tag="舒服" if "好" in prev_sleep_quality else "困",
            reply_mode="ok",
        ))

        # 3. 如果是工作日，加载课表
        if not weekend:
            classes_today = WEEKLY_CLASSES.get(weekday, [])
            for start_t, end_t, name, loc, tag in classes_today:
                day.activities.append(ScheduleActivity(
                    start_time=start_t, end_time=end_t,
                    name=name, activity_type="class",
                    location=loc, tag=tag,
                    reply_mode="short" if "点名" in tag else "ok",
                ))

        # 4. 生成三餐窗口
        self._generate_meals(day, current_mood, current_fatigue)

        # 5. 用 AI 填充自由时段的活动
        free_slots = self._find_free_slots(day)
        if free_slots:
            ai_activities = self._ai_generate_free_activities(
                free_slots, weekend, current_mood, current_fatigue,
                cycle_phase, day.prev_day_context
            )
            day.activities.extend(ai_activities)

        # 6. 添加入睡事件
        day.activities.append(ScheduleActivity(
            start_time=day.sleep_time,
            end_time=self._add_minutes(day.sleep_time, 5),
            name="准备睡觉",
            activity_type="sleep",
            location="宿舍",
            tag="困了",
            reply_mode="delayed",
        ))

        # 7. 排序
        day.sort()

        self.today = day
        self._save_today()
        log(f"日程已生成: {len(day.activities)}个活动 "
            f"起床{day.wake_time} 入睡{day.sleep_time} "
            f"{'周末' if weekend else '工作日'} "
            f"课{len([a for a in day.activities if a.activity_type == 'class'])}节 "
            f"餐{len([m for m in day.meals.values() if m.decided])}顿")
        return day

    # ----- 起床/入睡时间 -----
    def _generate_wake_sleep(self, weekend: bool, sleep_quality: str,
                              mood: int, fatigue: int) -> Tuple[str, str]:
        """根据诸多因素偏移起床和入睡时间"""
        if weekend:
            base_wake_h, base_wake_m = 9, random.randint(0, 30)
            base_sleep_h, base_sleep_m = 0, random.randint(0, 30)
        else:
            base_wake_h, base_wake_m = 7, random.randint(0, 29)
            base_sleep_h, base_sleep_m = 23, random.randint(0, 30)

        # 睡眠质量偏移
        if sleep_quality == "失眠":
            base_wake_h += random.randint(1, 2)
            base_wake_m += random.randint(0, 30)
        elif sleep_quality == "熬夜":
            base_wake_h += 1
            base_wake_m += random.randint(0, 59)
        elif "好" in sleep_quality:
            base_wake_m -= random.randint(0, 15)

        # 心情偏移
        if mood < 35:
            base_wake_h += random.randint(0, 1)
            base_wake_m += random.randint(0, 30)
        elif mood > 75:
            base_wake_m -= random.randint(0, 20)

        # 疲劳偏移
        if fatigue > 75:
            base_sleep_h = min(base_sleep_h, 23)
            base_sleep_m = random.randint(0, 30)  # 更早睡

        # 规范时间
        while base_wake_m >= 60:
            base_wake_h += 1
            base_wake_m -= 60
        while base_wake_m < 0:
            base_wake_h -= 1
            base_wake_m += 60
        while base_sleep_m >= 60:
            base_sleep_h += 1
            base_sleep_m -= 60
        while base_sleep_m < 0:
            base_sleep_h -= 1
            base_sleep_m += 60
        base_sleep_h = base_sleep_h % 24

        base_wake_h %= 24
        base_sleep_h %= 24
        wake = f"{base_wake_h:02d}:{base_wake_m:02d}"
        sleep_t = f"{base_sleep_h:02d}:{base_sleep_m:02d}"
        return wake, sleep_t

    # ----- 三餐生成 -----
    def _generate_meals(self, day: DaySchedule, mood: int, fatigue: int):
        """为一日三餐做决策，插入日程"""
        windows = {
            "breakfast": (self._add_minutes(day.wake_time, 10), "08:30"),
            "lunch": ("11:30", "13:00"),
            "dinner": ("17:30", "19:00"),
        }

        for meal_type, (win_start, win_end) in windows.items():
            eat_prob = 0.8
            if meal_type == "breakfast":
                wake_h = int(day.wake_time[:2])
                if wake_h >= 9: eat_prob -= 0.2
                if wake_h >= 10: eat_prob -= 0.3
                if fatigue > 70: eat_prob -= 0.15
            if mood < 30: eat_prob -= 0.2
            if mood > 70: eat_prob += 0.1
            decided = random.random() < max(0.1, min(0.95, eat_prob))

            what = ""
            where = ""
            with_who = ""
            if decided:
                pool = BREAKFAST_OPTIONS if meal_type == "breakfast" else LUNCH_DINNER_OPTIONS
                what = random.choice(pool)
                where = random.choice(["食堂", "外卖", "宿舍", "校外", "室友带的"])
                with_who = random.choice(["一个人", "和室友一起", "和朋友"])

            # 找30分钟不冲突的空档
            ws = int(win_start[:2]) * 60 + int(win_start[3:])
            we = int(win_end[:2]) * 60 + int(win_end[3:])
            occupied = sorted([
                (int(a.start_time[:2]) * 60 + int(a.start_time[3:]),
                 int(a.end_time[:2]) * 60 + int(a.end_time[3:]))
                for a in day.activities
            ])
            slot_start = None
            t = ws
            while t + 30 <= we:
                blocked = any(t < oe and t + 30 > os for os, oe in occupied)
                if not blocked:
                    slot_start = t
                    break
                t += 10
            if slot_start is None:
                decided = False
                slot_start = ws

            md = MealDecision(
                meal_type=meal_type, decided=decided,
                location=where, with_whom=with_who, what=what,
            )
            day.meals[meal_type] = md

            # 只有决定吃了才插入活动（不吃的餐不占时间）
            if decided:
                meal_labels = {"breakfast": "早饭", "lunch": "午饭", "dinner": "晚饭"}
                label = meal_labels[meal_type]
                slot_end = slot_start + 30
                day.activities.append(ScheduleActivity(
                    start_time=f"{slot_start // 60:02d}:{slot_start % 60:02d}",
                    end_time=f"{slot_end // 60:02d}:{slot_end % 60:02d}",
                    name=f"吃{label}",
                    activity_type="meal",
                    location=where,
                    tag="正常吃",
                    reply_mode="delayed",
                    meal_decision=md,
                ))

    # ----- 自由时段填充（AI） -----
    def _find_free_slots(self, day: DaySchedule) -> List[Tuple[str, str]]:
        """找出已排活动之间的 ≥30分钟空档（合并重叠区间后）"""
        # 转为分钟数
        occupied_mins = sorted([
            (int(a.start_time[:2]) * 60 + int(a.start_time[3:]),
             int(a.end_time[:2]) * 60 + int(a.end_time[3:]))
            for a in day.activities
        ])

        # 合并重叠区间
        merged = []
        for s, e in occupied_mins:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))

        day_start = int(day.wake_time[:2]) * 60 + int(day.wake_time[3:]) + 15
        day_end = int(day.sleep_time[:2]) * 60 + int(day.sleep_time[3:])

        free_slots = []
        prev_end = day_start
        for ms, me in merged:
            if ms - prev_end >= 30:
                free_slots.append((
                    f"{prev_end // 60:02d}:{prev_end % 60:02d}",
                    f"{ms // 60:02d}:{ms % 60:02d}",
                ))
            prev_end = max(prev_end, me)

        if day_end - prev_end >= 30:
            free_slots.append((
                f"{prev_end // 60:02d}:{prev_end % 60:02d}",
                f"{day_end // 60:02d}:{day_end % 60:02d}",
            ))

        return free_slots

    def _ai_generate_free_activities(self,
                                      free_slots: List[Tuple[str, str]],
                                      weekend: bool, mood: int, fatigue: int,
                                      cycle: str, prev_ctx: str) -> List[ScheduleActivity]:
        """AI 为自由时段生成具体活动"""
        if not free_slots:
            return []

        mood_desc = "开心" if mood > 70 else ("低落" if mood < 35 else "一般")
        fatigue_desc = "很累" if fatigue > 75 else ("精力好" if fatigue < 30 else "正常")

        slots_desc = "\n".join(
            f"时段{i+1}: {s}-{e} (共{self._slot_minutes(s,e)}分钟)"
            for i, (s, e) in enumerate(free_slots[:4])
        )

        system = (
            "你是冬，俄语专业大四女生。现在要安排你今天的自由时间。\n"
            "输出格式：每行一个活动，格式为「HH:MM-HH:MM | 活动名 | 地点 | 情绪标签」\n"
            "要求：\n"
            "- 活动要具体、自然、有生活气息，不要抽象概括。可以是练琴/自习/逛街/打游戏/躺尸/刷剧/和室友聊天/取快递/洗衣服/发呆/给花浇水/整理书架等等\n"
            "- 考虑当前状态：如果很累就安排休息类活动，心情低落安排些能让自己好起来的事\n"
            "- 每天要有变化感，不要总是同样的安排\n"
            "- 只输出活动行，不要解释"
        )

        user = (
            f"今天是{'周末' if weekend else '工作日'}，已排好课程和用餐。\n"
            f"当前心情{mood_desc}({mood})、疲劳{fatigue_desc}({fatigue})、周期{cycle}。\n"
            f"{'昨天：'+prev_ctx if prev_ctx else ''}\n"
            f"以下空档需要填充：\n{slots_desc}\n"
            f"请安排："
        )

        result = _call_schedule_ai(system, user)
        if not result:
            # 降级：用预设模板
            return self._fallback_free_activities(free_slots, weekend, fatigue)

        activities = []
        for line in result.strip().split("\n"):
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                continue
            time_part = parts[0].replace("：", ":")
            if "-" not in time_part:
                continue
            start_t, end_t = time_part.split("-")
            name = parts[1]
            location = parts[2] if len(parts) > 2 else "宿舍"
            tag = parts[3] if len(parts) > 3 else ""

            activities.append(ScheduleActivity(
                start_time=start_t.strip(), end_time=end_t.strip(),
                name=name, activity_type="free",
                location=location, tag=tag,
                reply_mode="ok",
            ))

        return activities if activities else self._fallback_free_activities(free_slots, weekend, fatigue)

    def _fallback_free_activities(self, free_slots, weekend, fatigue) -> List[ScheduleActivity]:
        """AI 失败时的降级活动生成"""
        weekend_pool = [
            ("逛街", "摩尔城", "开心"), ("打游戏", "宿舍", "专注"),
            ("看电影", "电影院", "开心"), ("躺尸", "宿舍", "瘫"),
            ("跟室友吃饭", "校外", "开心"), ("睡午觉", "宿舍", "舒服"),
        ]
        weekday_pool = [
            ("自习", "图书馆", "平静"), ("练琴", "琴房", "开心"),
            ("写论文", "宿舍", "烦躁"), ("跑步", "操场", "累"),
            ("躺尸看剧", "宿舍", "瘫"), ("刷手机", "宿舍", "放空"),
            ("收拾房间", "宿舍", "勤快"),
        ]
        pool = weekend_pool if weekend else weekday_pool
        if fatigue > 70:
            pool = [a for a in pool if a[1] == "宿舍"]

        activities = []
        for s, e in free_slots[:3]:
            act = random.choice(pool)
            activities.append(ScheduleActivity(
                start_time=s, end_time=e,
                name=act[0], activity_type="free",
                location=act[1], tag=act[2],
                reply_mode="ok",
            ))
        return activities

    # ----- 事件切换：AI 填充已完成活动的细节 -----
    def on_activity_transition(self, from_activity: ScheduleActivity,
                                to_activity: ScheduleActivity) -> Optional[str]:
        """
        当冬从一个活动切换到下一个时调用。
        如果今天还没填满3-5次，用 AI 为刚结束的活动生成具体细节。

        Returns:
            生成的细节文本，或 None（不需要填充时）
        """
        if not self.today:
            return None
        if self.today.ai_fill_count >= 5:
            return None
        if from_activity.activity_type in ("wake", "sleep", "transit"):
            return None

        # 决定是否填充（每段20%概率，但每天最多5次）
        if random.random() > 0.25 and self.today.ai_fill_count >= 3:
            return None

        self.today.ai_fill_count += 1
        detail = self._ai_fill_detail(from_activity, to_activity)
        if detail:
            details_before = from_activity.detail
            from_activity.detail = (details_before + "；" + detail) if details_before else detail
            self._save_today()
        return detail

    def _ai_fill_detail(self, from_act: ScheduleActivity,
                         to_act: ScheduleActivity) -> str:
        """AI 为一节课/活动生成当天的具体细节"""
        system = (
            "你是冬，俄语专业大四女生。你刚经历了一个活动，现在要用1-2句话描述刚才发生了什么有趣或印象深刻的事。\n"
            "要求：\n"
            "- 具体、细节化、口语化\n"
            "- 不同天的同一课要有不同的事发生（被点名/拖堂/走神/听懂了一个难点等）\n"
            "- 不要泛泛地说'今天课还行'，要说具体一件事\n"
            "- 直接输出描述，不要加引号或前缀"
        )

        tag = from_act.tag
        if "严" in tag:
            extra = "这门课比较严"
        elif "无聊" in tag or "水" in tag:
            extra = "这门课比较水/无聊"
        elif "有趣" in tag:
            extra = "这老师讲课挺有意思"
        elif "点名" in tag:
            extra = "小班课会点名"
        else:
            extra = ""

        user = (
            f"刚才{from_act.end_time}结束了「{from_act.name}」（{from_act.location}），{extra if extra else ''}。\n"
            f"接下来要去「{to_act.name}」（{to_act.location if to_act.location else ''} @{to_act.start_time}）。\n"
            f"刚才这节课/这个活动里发生了什么？"
        )

        result = _call_schedule_ai(system, user)
        return result if result else ""

    # ----- 过渡微事件 -----
    def maybe_micro_event(self, from_act: ScheduleActivity,
                           to_act: ScheduleActivity) -> Optional[str]:
        """在活动切换时，有概率触发一个过渡微事件"""
        if random.random() > 0.25:
            return None

        from_loc = from_act.location
        to_loc = to_act.location

        # 根据移动场景选池
        going_to_campus = any(w in to_loc for w in ["教学楼", "综合楼", "外语楼", "教室", "图书馆", "琴房", "操场"])
        in_dorm = "宿舍" in from_loc and "宿舍" in to_loc
        evening = int(from_act.end_time[:2]) >= 20

        pool = MICRO_EVENTS_CAMPUS
        if in_dorm:
            pool = MICRO_EVENTS_DORM
        elif evening:
            pool = MICRO_EVENTS_EVENING
        elif not going_to_campus and "宿舍" in from_loc:
            pool = MICRO_EVENTS_CAMPUS

        ev = random.choice(pool)
        if self.today:
            self.today.micro_events.append(ev)
        return ev

    # ----- 当前活动查询 -----
    def get_current_activity(self, now: datetime = None) -> Optional[ScheduleActivity]:
        """返回当前时段冬正在做的活动"""
        if not self.today:
            return None
        now = now or datetime.now()
        hm = now.strftime("%H:%M")
        curr = int(hm[:2]) * 60 + int(hm[3:])
        for a in self.today.activities:
            start = int(a.start_time[:2]) * 60 + int(a.start_time[3:])
            end = int(a.end_time[:2]) * 60 + int(a.end_time[3:])
            if start <= curr < end:
                return a
        return None

    def get_next_activity(self, now: datetime = None) -> Optional[ScheduleActivity]:
        """返回下一个活动"""
        if not self.today:
            return None
        now = now or datetime.now()
        hm = now.strftime("%H:%M")
        curr = int(hm[:2]) * 60 + int(hm[3:])
        for a in self.today.activities:
            start = int(a.start_time[:2]) * 60 + int(a.start_time[3:])
            if start > curr:
                return a
        return None

    # ----- Prompt 注入 -----
    def get_schedule_prompt(self, uid: int = None) -> str:
        """生成注入到对话 prompt 的日程信息"""
        if not self.today:
            return ""

        now = datetime.now()
        curr = self.get_current_activity(now)
        next_act = self.get_next_activity(now)

        if not curr:
            return ""

        parts = []

        # 基础活动
        parts.append(f"当前活动：{curr.name}（{curr.location}）")

        # 细节
        if curr.detail:
            parts.append(f"细节：{curr.detail}")

        # 回复模式
        if curr.reply_mode == "no":
            parts.append("当前消息不回复或只回1-2字")
        elif curr.reply_mode == "short":
            parts.append("可以回复但尽量简短，正在做事")
        elif curr.reply_mode == "delayed":
            parts.append(f"正在{curr.name}，回复可适当延迟")

        # 刚结束的活动
        if self._prev_activity and self._prev_activity != curr:
            prev_end = int(self._prev_activity.end_time[:2]) * 60 + int(self._prev_activity.end_time[3:])
            curr_min = int(now.strftime("%H:%M")[:2]) * 60 + int(now.strftime("%H:%M")[3:])
            if 0 < curr_min - prev_end <= 20:
                parts.append(f"刚结束{self._prev_activity.name}，可自然提及")

        # 接下来
        if next_act:
            parts.append(f"下一步：{next_act.start_time} {next_act.name}（{next_act.location}）")

        # 三餐状态（只报告时间已过的餐食，避免"预知未来"）
        meal_end_times = {"breakfast": "09:00", "lunch": "13:30", "dinner": "19:30"}
        now_str = now.strftime("%H:%M")
        for mt, md in self.today.meals.items():
            if now_str < meal_end_times.get(mt, "23:59"):
                continue  # 还没到吃饭时间，不注入
            label = {"breakfast": "早饭", "lunch": "午饭", "dinner": "晚饭"}[mt]
            if md.decided and md.what:
                parts.append(f"{label}：吃了{md.what}")
            elif not md.decided:
                parts.append(f"{label}：没吃/跳过")

        # 今天产生的微事件
        if self.today.micro_events:
            recent_micro = self.today.micro_events[-2:]
            if recent_micro:
                parts.append(f"今天路上：{'；'.join(recent_micro)}")

        return "【日程】" + "，".join(parts)

    # ----- 归档 -----
    def archive_today(self):
        """入睡时将今天的日程+经历打包存档"""
        if not self.today:
            return

        archive_data = {
            "date": self.today.date,
            "weekday": self.today.weekday,
            "wake": self.today.wake_time,
            "sleep": self.today.sleep_time,
            "activities": [a.to_dict() for a in self.today.activities],
            "meals": {
                mt: {
                    "decided": md.decided, "what": md.what,
                    "where": md.location, "with": md.with_whom,
                    "detail": md.detail,
                }
                for mt, md in self.today.meals.items()
            },
            "micro_events": self.today.micro_events,
            "ai_fill_count": self.today.ai_fill_count,
            "archived_at": datetime.now().isoformat(),
        }

        filepath = os.path.join(ARCHIVE_DIR, f"dong_schedule_{self.today.date}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(archive_data, f, ensure_ascii=False, indent=2)
        log(f"日程已归档 → {filepath}")

    def get_state_snapshot(self) -> Dict:
        """提取今天的状态快照，供明天生成日程参考"""
        if not self.today:
            return {}
        unfinished = []
        for a in self.today.activities:
            if a.activity_type == "free" and "论文" in a.name and not a.detail:
                unfinished.append(a.name)
        return {
            "date": self.today.date,
            "wake_time": self.today.wake_time,
            "activities_count": len(self.today.activities),
            "meals_skipped": [mt for mt, md in self.today.meals.items() if not md.decided],
            "unfinished": unfinished,
            "micro_event_count": len(self.today.micro_events),
        }

    # ----- 导出（仪表盘）-----
    def get_dashboard_state(self) -> Dict:
        """提供给仪表盘的日程可视化数据"""
        if not self.today:
            return {"active": False}

        now = datetime.now()
        curr = self.get_current_activity(now)
        next_act = self.get_next_activity(now)

        timeline = []
        for a in self.today.activities:
            timeline.append({
                "time": a.start_time,
                "name": a.name,
                "type": a.activity_type,
                "detail": a.detail[:40] if a.detail else "",
                "tag": a.tag,
            })

        meals_status = {}
        for mt, md in self.today.meals.items():
            label = {"breakfast": "早饭", "lunch": "午饭", "dinner": "晚饭"}[mt]
            meals_status[label] = f"{'吃了'+md.what if md.decided else '没吃'}"

        return {
            "active": True,
            "date": self.today.date,
            "wake": self.today.wake_time,
            "sleep": self.today.sleep_time,
            "current": curr.name if curr else "空闲",
            "current_detail": curr.detail if curr else "",
            "next": f"{next_act.start_time} {next_act.name}" if next_act else "--",
            "timeline": timeline,
            "meals": meals_status,
            "micro_events": self.today.micro_events[-3:],
            "ai_fills": self.today.ai_fill_count,
        }

    # ----- 工具 -----
    def _build_prev_context(self, sleep_q: str, mood_r: str, unfinished: str) -> str:
        parts = []
        if sleep_q and sleep_q != "一般":
            parts.append(f"昨晚{sleep_q}")
        if mood_r:
            parts.append(f"情绪残留：{mood_r}")
        if unfinished:
            parts.append(f"未完：{unfinished}")
        return "；".join(parts) if parts else ""

    @staticmethod
    def _add_minutes(time_str: str, minutes: int) -> str:
        h, m = int(time_str[:2]), int(time_str[3:])
        total = h * 60 + m + minutes
        return f"{total // 60 % 24:02d}:{total % 60:02d}"

    @staticmethod
    def _slot_minutes(start: str, end: str) -> int:
        sh, sm = int(start[:2]), int(start[3:])
        eh, em = int(end[:2]), int(end[3:])
        return (eh * 60 + em) - (sh * 60 + sm)

    # ----- 持久化 -----
    def _save_today(self):
        """保存当日日程到磁盘"""
        if not self.today:
            return
        data = {
            "date": self.today.date,
            "weekday": self.today.weekday,
            "is_weekend": self.today.is_weekend,
            "wake_time": self.today.wake_time,
            "sleep_time": self.today.sleep_time,
            "activities": [a.to_dict() for a in self.today.activities],
            "meals": {
                mt: {
                    "decided": md.decided, "what": md.what,
                    "where": md.location, "with": md.with_whom,
                    "detail": md.detail,
                }
                for mt, md in self.today.meals.items()
            },
            "micro_events": self.today.micro_events,
            "prev_day_context": self.today.prev_day_context,
            "ai_fill_count": self.today.ai_fill_count,
        }
        with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_today(self) -> Optional[DaySchedule]:
        """从磁盘加载当日日程"""
        if not os.path.exists(SCHEDULE_FILE):
            return None

        try:
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            today_str = datetime.now().strftime("%Y-%m-%d")
            if data.get("date") != today_str:
                # 旧日程，归档
                old_path = os.path.join(ARCHIVE_DIR, f"dong_schedule_{data.get('date', 'unknown')}.json")
                if not os.path.exists(old_path):
                    os.rename(SCHEDULE_FILE, old_path)
                return None

            day = DaySchedule(
                date=data["date"],
                weekday=data.get("weekday", 0),
                is_weekend=data.get("is_weekend", False),
                wake_time=data.get("wake_time", "07:30"),
                sleep_time=data.get("sleep_time", "23:30"),
                activities=[ScheduleActivity.from_dict(a) for a in data.get("activities", [])],
                micro_events=data.get("micro_events", []),
                prev_day_context=data.get("prev_day_context", ""),
                ai_fill_count=data.get("ai_fill_count", 0),
            )
            for mt, md in data.get("meals", {}).items():
                day.meals[mt] = MealDecision(
                    meal_type=mt,
                    decided=md.get("decided", False),
                    location=md.get("where", ""),
                    with_whom=md.get("with", ""),
                    what=md.get("what", ""),
                    detail=md.get("detail", ""),
                )
            self.today = day
            return day
        except Exception as e:
            log(f"加载日程失败: {e}")
            return None


# ============ 全局实例 + 向后兼容函数 ============
_schedule_system = ScheduleSystem()

# 旧版兼容的元组列表
_daily_schedule: List[Tuple] = []


def generate_daily_schedule(prev_sleep: str = "一般",
                            prev_mood: str = "",
                            prev_unfinished: str = "",
                            mood: int = 50, fatigue: int = 50,
                            cycle: str = "日常") -> List[Tuple]:
    """生成今日日程，保持旧版兼容返回格式"""
    global _daily_schedule
    day = _schedule_system.generate_today(
        prev_sleep_quality=prev_sleep,
        prev_mood_residue=prev_mood,
        prev_unfinished=prev_unfinished,
        current_mood=mood,
        current_fatigue=fatigue,
        cycle_phase=cycle,
    )
    _daily_schedule = [
        (a.start_time, a.end_time, a.name, a.location,
         a.reply_mode, 0, 0, a.tag)
        for a in day.activities
    ]
    return _daily_schedule


def get_schedule_prompt(uid: int = None) -> str:
    return _schedule_system.get_schedule_prompt(uid)


def get_schedule_state() -> Dict:
    return _schedule_system.get_dashboard_state()


def get_current_event(now=None):
    """旧版兼容"""
    a = _schedule_system.get_current_activity(now)
    if a:
        return (a.start_time, a.end_time, a.name, a.location,
                a.reply_mode, 0, 0, a.tag)
    return None


def should_skip_event(event=None):
    """旧版兼容 — 新版已由三餐决策和AI自由活动替代"""
    return False


def archive_today_schedule():
    """存档今日日程"""
    _schedule_system.archive_today()


def on_event_transition():
    """事件切换时调用：触发AI细节填充和过渡微事件"""
    now = datetime.now()
    curr = _schedule_system.get_current_activity(now)
    prev = getattr(_schedule_system, '_prev_activity', None)

    result = {"detail": None, "micro": None, "proactive_signal": None}

    if prev and curr and prev != curr:
        result["detail"] = _schedule_system.on_activity_transition(prev, curr)
        result["micro"] = _schedule_system.maybe_micro_event(prev, curr)
        # P2: 生成主动事件信号
        result["proactive_signal"] = {
            "from_activity": prev.name if prev else "",
            "to_activity": curr.name if curr else "",
            "label": f"{prev.name}→{curr.name}" if prev and curr else "",
            "detail": result["detail"],
            "ts": time.time(),
        }

    _schedule_system._prev_activity = curr
    # P2: 将信号推送到 status 模块供 interaction 读取
    if result["proactive_signal"]:
        try:
            from .status import set_proactive_signal
            set_proactive_signal(result["proactive_signal"])
        except Exception:
            pass
    return result
