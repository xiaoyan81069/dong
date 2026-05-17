"""
仪表盘面板 — 状态标签 / L4 灵魂面板 / 优化器状态导出
从 __init__.py L237-294, L421-428 提取
"""
from datetime import datetime


def _get_bot_state_label(sleeping, mood, fatigue):
    """返回机器人整体状态标签"""
    if sleeping:
        return "休眠"
    if fatigue > 80:
        return "疲倦"
    if mood < 35:
        return "低落"
    if mood > 75:
        return "兴奋"
    return "清醒"


def _build_l4_panel(hormones, grudges, recent, amygdala_state, startup_ts=0):
    """构建 L4 灵魂面板数据"""
    # 激素笔迹
    hormone_notes = {
        "dopamine": "还想再弹一会儿琴" if hormones.get("dopamine", 50) > 70 else "平静中",
        "cortisol": "有点喘不过气..." if hormones.get("cortisol", 20) > 50 else "还好，没什么压力",
        "oxytocin": "被人惦记的感觉真好" if hormones.get("oxytocin", 50) > 70 else "",
        "serotonin": "今天也是充实的一天" if hormones.get("serotonin", 50) > 70 else "有点提不起劲",
    }

    # 杏仁核橘猫状态映射
    alert = amygdala_state.get("alert", "平静")
    amygdala_cat_map = {
        "平静": "sleepy",
        "警觉": "alert",
        "紧张": "alert",
        "恐惧": "explode",
        "愤怒": "explode",
    }
    amygdala_cat = amygdala_cat_map.get(alert, "sleepy")
    if amygdala_state.get("hijack", False):
        amygdala_cat = "explode"

    # 记仇表（格式化）
    grudges_list = []
    for uid, gs in grudges.items():
        for g in gs:
            grudges_list.append({
                "who": str(uid),
                "what": g.get("reason", ""),
                "context": g.get("context", ""),
                "expire": f"{g.get('days_left', '?')}天后",
            })

    # 系统健康
    uptime_hours = round((datetime.now().timestamp() - startup_ts) / 3600, 1)
    system_health = f"心跳正常 | 已连续运行{uptime_hours}小时"

    return {
        "hormone_notes": hormone_notes,
        "amygdala_cat": amygdala_cat,
        "grudges": grudges_list,
        "recent_memories": [r.get("q", "")[:80] for r in (recent or [])[-5:]],
        "system_health": system_health,
    }


def _get_optimizer_state_export() -> dict:
    """导出优化代理状态供仪表盘"""
    try:
        from ..optimizer import get_optimizer_state
        return get_optimizer_state()
    except Exception:
        return {"enabled": False, "last_run": None, "last_result": "", "current_stage": "idle"}
