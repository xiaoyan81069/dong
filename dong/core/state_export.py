"""
状态 JSON 构建器 — _build_state_export
从 __init__.py _export_status 的 dict 构建部分提取
纯函数：接收所有收集好的数据，返回 state dict，不调任何外部模块
"""
from datetime import datetime


def _build_state_export(
    mood, fatigue, sleeping, hormones,
    bot_state, grudges, recent, intimacy_snap, cycle_info,
    amygdala_state, l4_panel, finance_snap, mail_snap, game_snap,
    weather_temp, weather_mood, today_summary, recent_uid,
    overwhelm_state, msg_count, last_msg_time,
    update_info, schedule_state, expression_export, expression_feature, optimizer_export,
):
    """组装完整状态字典（纯函数，不写盘不推事件）"""
    return {
        "time": datetime.now().strftime("%H:%M:%S"),
        "bot_state": bot_state,
        "mood": mood,
        "fatigue": fatigue,
        "sleeping": sleeping,
        "weather": weather_temp,
        "weather_mood": weather_mood,
        "grudges": grudges,
        "recent": recent[-10:],
        "today_summary": today_summary,
        "last_uid": str(recent_uid) if recent_uid else None,
        "hormones": hormones,
        "hormone_interactions": hormones.get("_interactions", {}),
        "hormone_event": hormones.get("_last_event", ""),
        "overwhelm": overwhelm_state,
        "amygdala": amygdala_state,
        "intimacy": intimacy_snap,
        "cycle": cycle_info,
        "_msg_count": msg_count,
        "_last_msg_time": last_msg_time,
        "_last_update_ts": datetime.now().timestamp(),
        "update_info": update_info,
        "schedule": schedule_state,
        "expression": expression_export,
        "expression_feature": expression_feature,
        "optimizer": optimizer_export,
        # 桌面宠物前端数据
        "l4_panel": l4_panel,
        "finance": finance_snap,
        "mail": mail_snap,
        "game": game_snap,
    }
