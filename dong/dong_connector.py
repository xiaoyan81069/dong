"""
冬 · 前端连接器 — 旅行青蛙风格微缩场景 API 服务器

轻量级本地API，读取冬的状态文件，为前端页面提供实时数据。
零外部依赖，直接放进冬的项目目录，python dong_connector.py 启动。

端点:
  GET /api/status   — 激素、杏仁核、超限、日程、亲密度 全量状态
  GET /api/scene    — 当前应展示的场景（根据状态推断）
  GET /api/weather  — 心情驱动的天气参数（雪花密度/速度/颜色）
  GET /api/ping     — 健康检查
"""

import json
import os
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
from urllib.parse import urlparse
from threading import Thread, Lock
from collections import deque

# ============ 配置 ============
HOST = "127.0.0.1"
PORT = 5120
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATUS_FILE = os.path.join(BASE_DIR, "dong_status.json")
SCHEDULE_FILE = os.path.join(BASE_DIR, "dong_today_schedule.json")
EXPRESSION_FILE = os.path.join(BASE_DIR, "dong_expression_state.json")
INTIMACY_FILE = os.path.join(BASE_DIR, "dong_intimacy.json")
MEMORY_FILE = os.path.join(BASE_DIR, "dong_memory.json")
FINANCE_FILE = os.path.join(BASE_DIR, "dong_finance.json")
MAIL_FILE = os.path.join(BASE_DIR, "dong_mail.json")
GAME_FILE = os.path.join(BASE_DIR, "dong_game.json")
GLOBAL_STATE_FILE = os.path.join(BASE_DIR, "dong_cycle.json")


# ============ 文件读取（容错） ============
def _load_json(path, default=None):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}


def _get_status():
    return _load_json(STATUS_FILE, {})


def _get_schedule():
    return _load_json(SCHEDULE_FILE, {})


def _get_expression():
    return _load_json(EXPRESSION_FILE, {})


def _get_intimacy():
    return _load_json(INTIMACY_FILE, {})


def _get_global_state():
    return _load_json(GLOBAL_STATE_FILE, {})


# ============ 回复保证：事件队列 ============
_event_queue: deque = deque(maxlen=200)
_event_lock = Lock()
_event_seq = 0
_last_frontend_poll = 0.0


def push_event(event_type: str, data: dict = None):
    global _event_seq
    with _event_lock:
        _event_seq += 1
        _event_queue.append({"seq": _event_seq, "type": event_type, "data": data or {}, "ts": datetime.now().isoformat()})


def get_events_since(since_seq: int = 0) -> list:
    with _event_lock:
        return [e for e in _event_queue if e["seq"] > since_seq]


def is_frontend_alive() -> bool:
    return (time.time() - _last_frontend_poll) < 60.0


def _register_health_probe():
    try:
        from .core.health_registry import registry, CheckLevel
        registry.register("frontend_poll", is_frontend_alive, interval=30, level=CheckLevel.WARN)
    except ImportError:
        pass


# ============ 场景推断 ============
def _infer_scene(status):
    """根据冬的当前状态推断应展示的场景"""
    sleeping = status.get("sleeping", False)
    overwhelm = status.get("overwhelm", {})
    amygdala = status.get("amygdala", {})
    expression = status.get("expression_feature", "default")
    schedule = status.get("schedule", {})
    mood = status.get("mood", 50)
    fatigue = status.get("fatigue", 50)
    hour = datetime.now().hour

    # 1. 睡眠 → 卧室场景
    if sleeping:
        return {
            "scene": "sleeping",
            "label": "冬正在睡觉",
            "mood_class": "peaceful",
            "background": "night_bedroom",
            "character_action": "sleeping",
        }

    # 2. 杏仁核劫持 → 防御姿态
    if amygdala.get("hijack", False):
        return {
            "scene": "hijack",
            "label": "冬被触发了防御",
            "mood_class": "tense",
            "background": "stormy_room",
            "character_action": "defensive",
        }

    # 3. 超限激活 → 冲突场景
    if overwhelm.get("active", False):
        phase = overwhelm.get("phase", "building")
        return {
            "scene": f"overwhelm_{phase}",
            "label": f"冬陷入内心冲突 ({phase})",
            "mood_class": "stormy",
            "background": "storm" if phase == "peak" else "overcast_room",
            "character_action": "conflicted",
        }

    # 4. 表演规则激活
    if expression == "wooden_stake":
        return {
            "scene": "vulnerable",
            "label": "冬的防御碎了",
            "mood_class": "tender",
            "background": "late_night_window",
            "character_action": "tearful",
        }
    if expression == "suspicion":
        return {
            "scene": "suspicious",
            "label": "冬在翻旧账",
            "mood_class": "cold",
            "background": "dark_corner",
            "character_action": "looking_away",
        }
    if expression == "body_feeling":
        return {
            "scene": "idle",
            "label": "冬在发呆",
            "mood_class": "dreamy",
            "background": "afternoon_room",
            "character_action": "stretching",
        }

    # 5. 心情驱动
    if mood < 25:
        return {
            "scene": "low_mood",
            "label": "冬心情低落",
            "mood_class": "sad",
            "background": "grey_room",
            "character_action": "slouching",
        }
    if mood > 80:
        return {
            "scene": "happy",
            "label": "冬嘎嘎开心",
            "mood_class": "happy",
            "background": "sunny_room",
            "character_action": "humming",
        }

    # 6. 日程驱动
    current = schedule.get("current", "")
    current_type = ""
    for act in schedule.get("timeline", []):
        if act.get("name") == current:
            current_type = act.get("type", "")
            break

    if current_type == "class":
        return {
            "scene": "studying",
            "label": f"冬在上课: {current}",
            "mood_class": "focused",
            "background": "classroom",
            "character_action": "writing",
        }
    if current_type == "meal":
        return {
            "scene": "eating",
            "label": f"冬在吃饭: {current}",
            "mood_class": "cozy",
            "background": "canteen",
            "character_action": "eating",
        }
    if current_type == "sleep":
        return {
            "scene": "bedtime",
            "label": "冬准备睡了",
            "mood_class": "sleepy",
            "background": "night_bedroom",
            "character_action": "yawning",
        }
    if current_type == "free":
        if fatigue > 70:
            return {
                "scene": "resting",
                "label": f"冬累了在休息",
                "mood_class": "tired",
                "background": "bedroom",
                "character_action": "lying_down",
            }
        if hour >= 23 or hour < 6:
            return {
                "scene": "late_night",
                "label": "冬深夜还没睡",
                "mood_class": "quiet",
                "background": "night_window",
                "character_action": "sitting",
            }
        return {
            "scene": "free_time",
            "label": f"冬在{current}",
            "mood_class": "relaxed",
            "background": "dorm_room",
            "character_action": "idle",
        }

    # 7. 默认
    return {
        "scene": "default",
        "label": "冬在自己的小世界里",
        "mood_class": "neutral",
        "background": "dorm_room",
        "character_action": "sitting",
    }


# ============ 天气推断（心情驱动） ============
def _infer_weather(status):
    """
    冬内心的"天气"由心情驱动，用雪花参数来可视化。
    mood 越高 → 雪越少越慢（晴朗）
    mood 越低 → 雪越多越快（暴风雪）
    """
    mood = status.get("mood", 50)
    overwhelm = status.get("overwhelm", {})
    amygdala = status.get("amygdala", {})

    # 基础：心情映射到雪花密度和速度
    if mood >= 80:
        density = max(0, 0.5 - (mood - 80) * 0.025)    # 0 ~ 0.5
        speed = max(0, 0.3 - (mood - 80) * 0.015)        # 0 ~ 0.3
        weather_type = "sunny"
        sky_tint = "#fff8e7"
    elif mood >= 60:
        density = 0.5 + (80 - mood) * 0.025              # 0.5 ~ 1.0
        speed = 0.3 + (80 - mood) * 0.015                 # 0.3 ~ 0.6
        weather_type = "light_snow"
        sky_tint = "#e8e0f0"
    elif mood >= 40:
        density = 1.0 + (60 - mood) * 0.05               # 1.0 ~ 2.0
        speed = 0.6 + (60 - mood) * 0.02                  # 0.6 ~ 1.0
        weather_type = "snow"
        sky_tint = "#d0c8e0"
    elif mood >= 25:
        density = 2.0 + (40 - mood) * 0.1                # 2.0 ~ 3.5
        speed = 1.0 + (40 - mood) * 0.04                  # 1.0 ~ 1.6
        weather_type = "heavy_snow"
        sky_tint = "#b0a0c8"
    else:
        density = 3.5 + (25 - mood) * 0.15               # 3.5 ~ 7.0
        speed = 1.6 + (25 - mood) * 0.06                  # 1.6 ~ 3.0
        weather_type = "blizzard"
        sky_tint = "#8a7aaa"

    # 超限 → 天气加码
    if overwhelm.get("active", False):
        phase = overwhelm.get("phase", "")
        if phase == "peak":
            density *= 1.6
            speed *= 1.5
            weather_type = "storm"
            sky_tint = "#6a5a8a"
        elif phase == "recovery":
            density *= 1.2
            speed *= 1.2

    # 劫持 → 突然暴风
    if amygdala.get("hijack", False):
        density = max(density, 5.0)
        speed = max(speed, 2.5)
        weather_type = "hijack_storm"
        sky_tint = "#4a3a5a"

    # 雪花颜色：mood低时偏冷蓝紫
    if mood >= 60:
        snow_color = "#ffffff"
    elif mood >= 35:
        snow_color = "#e8e0f8"
    else:
        snow_color = "#d0c0f0"

    return {
        "type": weather_type,
        "snowflake_density": round(density, 2),
        "snowflake_speed": round(speed, 2),
        "snowflake_size": round(1.0 + density * 0.3, 2),
        "snow_color": snow_color,
        "sky_tint": sky_tint,
        "mood": mood,
        "wind": round(speed * 0.7, 2),
    }


# ============ 状态汇总 ============
def build_status_response():
    status = _get_status()
    schedule = _get_schedule()
    expression = _get_expression()
    intimacy = _get_intimacy()

    hormones = status.get("hormones", {})
    amygdala = status.get("amygdala", {})
    overwhelm = status.get("overwhelm", {})

    # 找当前日程活动
    current_activity = status.get("schedule", {}).get("current", "")
    next_activity = status.get("schedule", {}).get("next", "")
    timeline = status.get("schedule", {}).get("timeline", [])

    # 亲密度
    intimacy_data = intimacy.get("users", {})

    return {
        "timestamp": datetime.now().isoformat(),
        "bot_state": status.get("bot_state", "清醒"),
        "mood": status.get("mood", 50),
        "fatigue": status.get("fatigue", 50),
        "sleeping": status.get("sleeping", False),

        # 激素
        "hormones": {
            "dopamine": hormones.get("dopamine", 50),
            "adrenaline": hormones.get("adrenaline", 30),
            "cortisol": hormones.get("cortisol", 20),
            "oxytocin": hormones.get("oxytocin", 50),
            "serotonin": hormones.get("serotonin", 60),
            "dominant": hormones.get("dominant", "neutral"),
        },

        # 杏仁核
        "amygdala": {
            "valence": amygdala.get("last_valence", 0),
            "arousal": amygdala.get("last_arousal", 0),
            "alert": amygdala.get("alert", "平静"),
            "hijack": amygdala.get("hijack", False),
            "threat_level": amygdala.get("threat_level", 0),
            "reward_type": amygdala.get("reward_type", ""),
        },

        # 超限
        "overwhelm": {
            "active": overwhelm.get("active", False),
            "phase": overwhelm.get("phase", "building"),
            "conflict": overwhelm.get("conflict", ""),
        },

        # 日程
        "schedule": {
            "current_activity": current_activity,
            "next_activity": next_activity,
            "wake_time": schedule.get("wake_time", ""),
            "sleep_time": schedule.get("sleep_time", ""),
            "is_weekend": schedule.get("is_weekend", False),
            "today_summary": status.get("today_summary", ""),
        },

        # 表演
        "expression_feature": status.get("expression_feature", "default"),
        "expression": {
            "breakthrough_monthly": expression.get("breakthrough_this_month", 0),
            "breakthrough_max": expression.get("breakthrough_max_per_month", 3),
            "suspicion_today": expression.get("suspicion_today_count", 0),
        },

        # 亲密度
        "intimacy": intimacy_data,

        # 消息
        "msg_count": status.get("_msg_count", 0),
        "last_msg_time": status.get("_last_msg_time", "--"),
    }


# ============ 动作函数（兼容独立运行和包导入） ============
def _import_module(name):
    """导入dong子模块，兼容独立脚本和包内运行"""
    try:
        return __import__(f"dong.{name}", fromlist=[name])
    except (ImportError, ValueError):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(BASE_DIR, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _call_game_buy(item_id):
    game = _import_module("game")
    return game.buy_item(item_id)


def _call_game_equip(item_id, slot):
    game = _import_module("game")
    return game.equip_item(item_id, slot)


def _call_mail_send(to_name, content, from_uid):
    mail = _import_module("mail")
    return mail.send_letter(to_name, content, from_uid)


def _call_finance_recharge(amount):
    finance = _import_module("finance")
    new_balance = finance.add_transaction("充值", amount, "income")
    return {"success": True, "new_balance": new_balance}


def _call_game_item_detail(item_id):
    game = _import_module("game")
    return game.get_item_detail(item_id)


# ============ HTTP 处理器 ============
class DongAPIHandler(BaseHTTPRequestHandler):

    def _set_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, data, code=200):
        import math as _m
        def _sanitize(obj):
            if isinstance(obj, dict):
                return {k: _sanitize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_sanitize(v) for v in obj]
            if isinstance(obj, float) and (_m.isnan(obj) or _m.isinf(obj)):
                return 0.0
            return obj
        safe = _sanitize(data)
        body = json.dumps(safe, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._set_cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, msg, code=500):
        self._send_json({"error": True, "message": msg}, code)

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            if path == "/api/status":
                self._send_json(build_status_response())

            elif path == "/api/scene":
                status = _get_status()
                scene = _infer_scene(status)
                scene["mood"] = status.get("mood", 50)
                scene["fatigue"] = status.get("fatigue", 50)
                scene["timestamp"] = datetime.now().isoformat()
                self._send_json(scene)

            elif path == "/api/weather":
                status = _get_status()
                weather = _infer_weather(status)
                weather["timestamp"] = datetime.now().isoformat()
                self._send_json(weather)

            elif path == "/api/ping":
                self._send_json({"pong": True, "time": datetime.now().isoformat()})

            elif path == "/api/events":
                global _last_frontend_poll
                _last_frontend_poll = time.time()
                since = 0
                try:
                    since = int(parsed.query.split("=")[1]) if "since=" in parsed.query else 0
                except Exception:
                    pass
                events = get_events_since(since)
                with _event_lock:
                    seq = _event_seq
                self._send_json({"events": events, "latest_seq": seq})

            elif path == "/api/full":
                status = _get_status()
                scene = _infer_scene(status)
                weather = _infer_weather(status)

                # 直接从源文件读取（不依赖dong_status.json的定时导出）
                finance_raw = _load_json(FINANCE_FILE, {"balance": 500.0, "currency_name": "雪花币", "transactions": []})
                mail_raw = _load_json(MAIL_FILE, {"letters": []})
                game_raw = _load_json(GAME_FILE, {"scene_id": "dorm_room", "coins": 100, "inventory": {"equipped": {}, "items": []}})

                # 格式化信件快照
                now_ts = time.time()
                delivered_unread = sum(
                    1 for l in mail_raw.get("letters", [])
                    if not l.get("read") and l.get("deliver_at", 0) <= now_ts
                )
                in_transit = sum(
                    1 for l in mail_raw.get("letters", [])
                    if not l.get("read") and l.get("deliver_at", 0) > now_ts
                )
                mail_snap = {
                    "has_new_mail": delivered_unread > 0,
                    "unread_count": delivered_unread,
                    "in_transit_count": in_transit,
                    "sent_status": "new_mail" if delivered_unread > 0 else "idle",
                }

                # 格式化游戏快照
                game_inv = game_raw.get("inventory", {})
                # 从 game.py 加载商店目录（静态数据，不存JSON）
                try:
                    game_mod = _import_module("game")
                    catalog = game_mod.SHOP_CATALOG
                except Exception:
                    catalog = []
                game_snap = {
                    "scene_id": game_raw.get("scene_id", "dorm_room"),
                    "inventory": {
                        "coins": game_raw.get("coins", 100),
                        "equipped": game_inv.get("equipped", {}),
                        "items": game_inv.get("items", []),
                    },
                    "shop_catalog": catalog,
                }

                self._send_json({
                    "timestamp": datetime.now().isoformat(),
                    "status": build_status_response(),
                    "scene": scene,
                    "weather": weather,
                    "l4_panel": status.get("l4_panel", {}),
                    "finance": finance_raw,
                    "mail": mail_snap,
                    "game": game_snap,
                })

            else:
                self._send_json({
                    "error": True,
                    "message": f"未知端点: {path}",
                    "available": [
                        "/api/status", "/api/scene", "/api/weather",
                        "/api/ping", "/api/full",
                        "POST /api/game/buy", "POST /api/game/equip",
                        "POST /api/mail/send", "POST /api/finance/recharge",
                        "POST /api/game/item_detail",
                    ],
                }, 404)

        except Exception as e:
            self._send_error_json(str(e))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        content_length = int(self.headers.get("Content-Length", 0))
        body = b""
        if content_length > 0:
            body = self.rfile.read(content_length)

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_error_json("请求体不是有效JSON", 400)
            return

        try:
            if path == "/api/game/buy":
                result = _call_game_buy(data.get("item_id", ""))
                self._send_json(result)

            elif path == "/api/game/equip":
                result = _call_game_equip(data.get("item_id", ""), data.get("slot"))
                self._send_json(result)

            elif path == "/api/mail/send":
                result = _call_mail_send(
                    data.get("to_name", "冬"),
                    data.get("content", ""),
                    data.get("from_uid"),
                )
                self._send_json(result)

            elif path == "/api/finance/recharge":
                amount = data.get("amount", 0)
                if amount <= 0:
                    self._send_error_json("充值金额必须大于0", 400)
                    return
                result = _call_finance_recharge(amount)
                self._send_json(result)

            elif path == "/api/game/item_detail":
                result = _call_game_item_detail(data.get("item_id", ""))
                self._send_json(result)

            else:
                self._send_json({
                    "error": True,
                    "message": f"未知POST端点: {path}",
                }, 404)

        except Exception as e:
            self._send_error_json(str(e))


# ============ 启动 ============
def start_server():
    server = ThreadingHTTPServer((HOST, PORT), DongAPIHandler)
    print(f"[dong_connector] 冬前端连接器已启动 → http://{HOST}:{PORT}")
    print(f"[dong_connector] 端点:")
    print(f"  GET  /api/status   — 全量状态")
    print(f"  GET  /api/scene    — 场景推断")
    print(f"  GET  /api/weather  — 心情天气")
    print(f"  GET  /api/ping     — 健康检查")
    print(f"  GET  /api/full     — 全部数据(含l4/金融/信件/游戏)")
    print(f"  POST /api/game/buy        — 购买物品")
    print(f"  POST /api/game/equip      — 穿戴物品")
    print(f"  POST /api/mail/send       — 投递信件")
    print(f"  POST /api/finance/recharge — 充值")
    print(f"  POST /api/game/item_detail — 物品详情")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dong_connector] 已关闭")
        server.shutdown()


if __name__ == "__main__":
    start_server()
