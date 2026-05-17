"""
冬 · 状态系统 — 天气系统
WeatherSystem 类 + 天气兼容函数
"""
import requests
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional


# ============ 天气系统 ============
@dataclass
class WeatherSystem:
    """天气系统类"""
    lat: float = 49.01
    lon: float = 119.48

    _cache: Optional[Dict] = field(default=None, repr=False)
    _timestamp: Optional[datetime] = field(default=None, repr=False)
    _force_refresh: bool = field(default=False, repr=False)

    # 天气代码映射
    CODE_MAP = {
        0: ("晴", "心情不错"), 1: ("晴", "心情不错"),
        2: ("多云", "还行"), 3: ("阴", "有点丧"),
        45: ("雾", "闷闷的"), 48: ("雾", "闷闷的"),
        51: ("小雨", "烦躁想宅"), 53: ("小雨", "烦躁想宅"), 55: ("小雨", "烦躁想宅"),
        61: ("中雨", "烦躁"), 63: ("中雨", "烦躁"), 65: ("大雨", "烦躁"),
        71: ("小雪", "冷但兴奋"), 73: ("中雪", "冷"), 75: ("大雪", "冷"), 77: ("雪", "冷但兴奋"),
        80: ("阵雨", "烦躁"), 81: ("阵雨", "烦躁"), 82: ("阵雨", "烦躁"),
        85: ("阵雪", "冷"), 86: ("阵雪", "冷"),
        95: ("雷雨", "烦死"), 96: ("雷雨", "烦死"), 99: ("雷雨", "烦死"),
    }

    TRIGGERS = ["天气", "冷", "热", "下雨", "下雪", "气温", "温度", "晴天", "阴天"]

    def init(self):
        """初始化天气"""
        self._force_refresh = True
        self.get_data(force=True)
        from ..log import log
        log(f"天气已初始化：{self._cache}")

    def get_data(self, force: bool = False) -> Dict:
        """获取天气数据"""
        now = datetime.now()
        if not force and not self._force_refresh:
            if self._cache and self._timestamp:
                hours_diff = (now - self._timestamp).total_seconds() / 3600
                if hours_diff < 6:
                    return self._cache

        try:
            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": self.lat, "longitude": self.lon,
                "current_weather": True, "daily": "sunset", "timezone": "Asia/Shanghai",
            }
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
            weather_code = data["current_weather"]["weathercode"]
            weather_text, mood = self.CODE_MAP.get(weather_code, ("未知", "一般"))
            self._cache = {
                "温度": data["current_weather"]["temperature"],
                "天气": weather_text, "心情": mood,
                "日落": data["daily"]["sunset"][0] if "daily" in data and "sunset" in data["daily"] else "18:00",
                "更新时间": now.strftime("%H:%M"),
            }
            self._timestamp = now
            self._force_refresh = False
            return self._cache
        except Exception as e:
            from ..log import log
            log(f"天气获取失败: {e}")
            return {"温度": "未知", "天气": "未知", "心情": "一般", "日落": "18:00", "更新时间": "未知"}

    def should_refresh(self, text: str) -> bool:
        """检查是否需要刷新天气"""
        return any(kw in text for kw in self.TRIGGERS)

    def build_system_prompt(self) -> str:
        """构建天气系统提示"""
        if self._cache is None:
            self.get_data(force=True)
        w = self._cache or {"天气": "未知", "温度": "未知", "日落": "18:00", "心情": "一般"}
        return f"""【当前位置】你在呼伦贝尔。
当前时间：{datetime.now().strftime("%H:%M")}。
当前天气：{w['天气']}，气温{w['温度']}度，日落时间大约{w['日落']}。
你现在的状态：{w['心情']}。"""

    @property
    def current_weather(self) -> str:
        """获取当前天气文本"""
        return self._cache.get("天气", "未知") if self._cache else "未知"


# 全局天气系统实例
weather_system = WeatherSystem()


# ============ 兼容旧接口 weather_cache ============
class _WeatherCacheCompat:
    """weather_cache 兼容层"""
    def __init__(self, ws):
        self._ws = ws

    def __getitem__(self, key):
        if key == "force_refresh":
            return self._ws._force_refresh
        return {}

    def __setitem__(self, key, value):
        if key == "force_refresh":
            self._ws._force_refresh = value

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, TypeError):
            return default


weather_cache = _WeatherCacheCompat(weather_system)


def init_weather():
    """初始化天气（兼容旧接口）"""
    weather_system.init()


def get_weather_data(force=False):
    """获取天气数据（兼容旧接口）"""
    return weather_system.get_data(force)


def should_refresh_weather(text):
    """检查天气刷新（兼容旧接口）"""
    return weather_system.should_refresh(text)


def build_weather_system():
    """构建天气提示（兼容旧接口）"""
    return weather_system.build_system_prompt()