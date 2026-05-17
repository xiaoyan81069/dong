"""
检查项: hormone_stuck — 检测多巴胺是否卡在100超30分钟
级别: WARN / 间隔: 3600秒
"""
import json
import os
import time

from ..core.health_registry import CheckLevel, register_check

STATUS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dong_status.json")


@register_check("hormone_stuck", interval=3600, level=CheckLevel.WARN)
def check_hormone_stuck() -> bool:
    """如果多巴胺连续30分钟卡在100，说明衰减机制可能失效。"""
    if not os.path.exists(STATUS_FILE):
        return True  # 文件不存在时不误报，交给 smoke_data_files 处理

    try:
        stat = os.stat(STATUS_FILE)
        file_age_minutes = (time.time() - stat.st_mtime) / 60

        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 休眠期间多巴胺不衰减，卡100属正常，跳过检查
        if data.get("sleeping", False):
            return True

        hormones = data.get("hormones", {})
        dopamine = hormones.get("dopamine", 0)

        if dopamine == 100 and file_age_minutes > 30:
            import logging
            logging.getLogger("dong.tests.check_hormone_stuck").warning(
                "多巴胺卡在100已超30分钟（状态文件%dm前更新），衰减机制可能失效",
                int(file_age_minutes),
            )
            return False

        return True
    except (json.JSONDecodeError, OSError) as e:
        import logging
        logging.getLogger("dong.tests.check_hormone_stuck").warning(
            "无法读取状态文件: %s", e
        )
        return True  # 读取失败交给 smoke_data_files 处理，不重复告警
