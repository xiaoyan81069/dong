"""
冬 · 状态系统子包
按功能拆分为：
  _core.py    — 心境流内核（compute_drives, compute_mood_vector, mood_vector_to_text）
  weather.py  — 天气系统（WeatherSystem + 兼容函数）
  hormones.py — 激素系统（UserStatus, HormoneState, HormoneSystem）
  manager.py  — StatusManager 状态管理器（包含所有兼容旧接口函数）
  compat.py   — 兼容层：从各子文件 re-export 所有符号，保持 from .status import xxx 不变
"""
from .compat import *