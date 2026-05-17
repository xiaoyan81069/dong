"""
冬 · QQ机器人
模块化架构：config / log / status / schedule / memory / persona / media / interaction / api
"""
import asyncio
import hashlib
import json
import os
import random
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime

import websockets

from .overwhelm import check_overwhelm, get_overwhelm_prompt, get_overwhelm_state, \
    should_retract, fragment_text, get_unstable_delay, \
    generate_physiological_monologue, maybe_trigger_breakthrough

from .config import (
    ALLOWED_USERS, MASTER_UID, SENDBOT_API, NAPCAT_DIR,
    is_late_night, is_weekend,
    describe_time_gap, describe_time_of_day, get_current_time_baseline,
)
from .log import log
from .status import (
    init_weather, load_status, update_fatigue, update_mood,
    detect_mood_change, check_sleep, get_status_prompt,
    get_cycle_prompt, get_pushpull_prompt, get_offline_prompt,
    should_trigger_voice, get_voice_state_prompt,
    process_offline_life, update_mood_cycle, update_pushpull,
    apply_mood_ripple, note_interaction, clear_offline_events,
    should_refresh_weather, build_weather_system,
    maybe_life_fragment, save_day_summary, get_cycle_proactive_bonus,
    get_pushpull_proactive_bonus,
    # 新功能
    get_time_sense_prompt, get_date_awareness_prompt,
    get_habits_prompt, should_terminate_conversation,
    apply_external_mood,
    # 新增
    get_sleep_transition, get_bedtime_message, get_morning_message,
    check_weather_care, reset_daily_flags,
    # 激素系统
    detect_hormone_event, update_hormones, get_hormone_snapshot, _ai_hormone_correct,
)
from .schedule import (
    generate_daily_schedule, get_schedule_prompt,
    archive_today_schedule, on_event_transition,
    get_schedule_state,
)
from .memory import (
    get_history, add_history, get_memory_text,
    retrieve_relevant_memories, auto_summarize_turn,
    should_remember, add_memory, save_chat_history, save_bot_reply,
    set_visual_memory, tick_visual_memory, get_visual_memory_prompt,
    conversation_history, last_active_time,
    # 新功能
    maybe_memory_flashback, maybe_recall_monologue,
    store_internal_monologue,
    # 记忆系统升级
    log_experience, archive_daily_experiences,
    generate_daily_summary, get_today_summary,
)
from .factory import load_factory_archive, get_factory_prompt, append_to_archive
from .grudge import mark_grudge, resolve_grudge, get_active_grudges, get_grudge_ammo, get_grudge_prompt
from .amygdala import process_amygdala, amygdala_learn, get_amygdala_state
from .persona import replace_cq_faces, absorb_user_language
from .media import (
    init_media_dirs, download_media, image_to_base64,
    register_image, get_image_file, get_record_file,
    audio_to_text, chat_vision, text_to_speech,
    _auto_load_cloned_voice,
)
from .interaction import (
    send_msg, send_msg_lines, send_image_message,
    send_voice_message, send_emoji_message,
    maybe_recall, withdraw_message, should_not_reply, get_reply_delay,
    is_difficult_question, process_reply,
    maybe_proactive_event, events_today_count, last_event_time,
    should_chase_up, get_chase_up_question,
    was_ignored, get_late_night_system,
    PROACTIVE_EVENTS,
)
from .media import (
    pick_image, pick_emoji, should_send_emoji,
)
from .api import chat
from .intimacy import (
    load_intimacy, get_intimacy_prompt, auto_intimacy_change,
    modify_intimacy, get_intimacy_level,
)
from .update import log_update, get_update_info
from .core.startup_check import run_startup_checks, bind_from_config_module
from .core.health_registry import CheckLevel
from .expression import (
    resolve_expression, _active_expression, clear_post_breakthrough,
    get_expression_state_export, _reset_daily as expression_reset_daily,
)
# 烟雾测试：@register_check 装饰器注册7项检查
from .tests import smoke_test as _smoke  # noqa: F401 — import即注册
from .tests import check_hormone_stuck as _chs  # noqa: F401 — import即注册
# 模块自动发现（新模块用 @bus.on_phase 注册即可被发现）
from .core.module_loader import discover_and_load
from .core.napcat_launcher import is_port_open, start_napcat, wait_for_napcat
from .core.dashboard_panel import _get_bot_state_label, _get_optimizer_state_export
from .core.dashboard_panel import _build_l4_panel as _blp
from .core.media_handler import _process_image_message, _process_voice_message
from .core.system_prompt import _build_system_prompt as _bsp
from .core.state_export import _build_state_export
from .core.message_helpers import _merge_if_related, _generate_test_reply
from .core.qq_rescue import do_qq_rescue

# ============ 消息去重缓存 ============
recent_messages = {}

# ============ 杏仁核显著性标记 ============
_pending_amygdala_salient = False

# ============ 休眠守护：最后活跃时间（用于启动时睡眠检测） ============
_last_msg_ts = 0.0   # 最近一条消息的时间戳
_last_msg_uid = 0    # 最近一条消息的发送者
_need_sleep_goodbye = False     # 启动后需要先告别再睡
_sleep_goodbye_uid = 0          # 告别的对象
_startup_ts = datetime.now().timestamp()  # 系统启动时间戳
_qq_rescue_sent = False         # QQ断线自救已触发（防重复）
_last_export_ts = 0               # _export_status 最小间隔（秒），防高频写盘
_last_fatal_notify = {}          # FATAL健康告警去重: {check_name: last_notified_failures}
_claude_sessions = {}            # Claude桥接会话: {uid: enter_time}

def _load_claude_sessions():
    """从文件恢复Claude会话（冬重启后不丢）"""
    global _claude_sessions
    try:
        sf = os.path.join(os.path.dirname(__file__), "claude_sessions.json")
        if os.path.exists(sf):
            with open(sf, "r", encoding="utf-8") as f:
                raw = json.load(f)
                # JSON key都是字符串，转回整数uid
                _claude_sessions = {int(k): v for k, v in raw.items()}
    except Exception:
        log("加载 Claude sessions 失败", exc_info=True)

def _save_claude_sessions():
    try:
        sf = os.path.join(os.path.dirname(__file__), "claude_sessions.json")
        with open(sf, "w") as f:
            json.dump(_claude_sessions, f)
    except Exception:
        log("保存 Claude sessions 失败", exc_info=True)

# ============ 仪表盘运行时统计 ============
_msg_count_today = 0
_last_msg_time_str = "--"

# ============ 缓存清理阈值 ============
MESSAGE_CACHE_MAX_SIZE = 1000
MESSAGE_CACHE_TTL = 300  # 5分钟

# ============ 辅助函数 ============


def _cleanup_message_cache():
    """清理过期的消息缓存"""
    global recent_messages
    now = datetime.now().timestamp()
    recent_messages = {
        k: v for k, v in recent_messages.items()
        if now - v < MESSAGE_CACHE_TTL
    }


def _grudge_check(uid, text):
    """#11 记仇系统 — 杏仁核已处理威胁标记，这里只检测道歉/和解"""
    text_stripped = text.strip()

    # 道歉/和解 → 解决记仇
    apology_words = {"对不起", "我错了", "原谅", "别生气了", "哄你", "不要生气", "我不好", "抱歉"}
    if any(w in text_stripped for w in apology_words):
        resolve_grudge(uid)
        return


def _build_l4_panel(hormones, grudges, recent, amygdala_state):
    """代理：注入 _startup_ts 后调用 core.dashboard_panel._build_l4_panel"""
    return _blp(hormones, grudges, recent, amygdala_state, _startup_ts)


def _export_status(recent_uid=None):
    """导出机器人实时状态到 JSON 文件（供仪表盘读取）"""
    global _last_export_ts
    now = time.time()
    if now - _last_export_ts < 30:
        return
    _last_export_ts = now
    try:
        from .status import _status, weather_cache
        from .grudge import get_active_grudges
        from .memory import get_today_summary, conversation_history

        mood = _status.get("mood", 50)
        fatigue = _status.get("fatigue", 50)
        sleeping = _status.get("sleeping", False)
        hormones = _status.get("hormones", {})
        voice_state = _status.get("_voice_state", {})

        # 整体状态标签
        bot_state = _get_bot_state_label(sleeping, mood, fatigue)

        # 收集记仇
        grudges = {}
        for u in ALLOWED_USERS:
            g_list = get_active_grudges(u)
            if g_list:
                grudges[str(u)] = [{"reason": g["reason"], "context": g["context"][:50], "days_left": g.get("days_left", "?")} for g in g_list]

        # 最近对话（最多10条）
        recent = []
        for uid_key, hx in list(conversation_history.items())[-3:]:
            for q, a, t in hx[-3:]:
                recent.append({"uid": str(uid_key), "q": q[:60], "a": a[:60], "t": t.strftime("%H:%M:%S")})

        # 亲密度快照
        intimacy_snap = {}
        try:
            from .intimacy import _intimacy
            for u in ALLOWED_USERS:
                if u in _intimacy:
                    intimacy_snap[str(u)] = _intimacy[u].get("level", 0)
        except Exception:
            pass

        # 周期阶段
        cycle_info = {}
        try:
            from .status import get_cycle_prompt
            cp = get_cycle_prompt()
            cycle_info["prompt"] = cp if cp else ""
        except Exception:
            pass

        # 杏仁核状态（L4面板也需要）
        amygdala_state = get_amygdala_state()

        # L4面板数据
        l4_panel = _build_l4_panel(hormones, grudges, recent, amygdala_state)
        # 金融/信件/游戏快照
        finance_snap = {}
        mail_snap = {}
        game_snap = {}
        try:
            from .finance import get_finance_snapshot
            finance_snap = get_finance_snapshot()
        except Exception:
            pass
        try:
            from .mail import get_mail_snapshot
            mail_snap = get_mail_snapshot()
        except Exception:
            pass
        try:
            from .game import get_game_snapshot
            game_snap = get_game_snapshot()
        except Exception:
            pass

        # 预计算状态导出所需的函数调用结果
        weather_temp = weather_cache.get("温度", "?") if hasattr(weather_cache, "get") else "?"
        weather_mood = weather_cache.get("心情", "") if hasattr(weather_cache, "get") else ""
        today_summary_val = get_today_summary()
        overwhelm_state = get_overwhelm_state()
        update_info_val = get_update_info()
        schedule_state = get_schedule_state()
        expression_export = get_expression_state_export()
        expression_feature = _active_expression._current.feature if _active_expression._current else "default"
        optimizer_export = _get_optimizer_state_export()

        state = _build_state_export(
            mood=mood, fatigue=fatigue, sleeping=sleeping,
            hormones=hormones,
            bot_state=bot_state, grudges=grudges,
            recent=recent, intimacy_snap=intimacy_snap,
            cycle_info=cycle_info, amygdala_state=amygdala_state,
            l4_panel=l4_panel, finance_snap=finance_snap,
            mail_snap=mail_snap, game_snap=game_snap,
            weather_temp=weather_temp, weather_mood=weather_mood,
            today_summary=today_summary_val, recent_uid=recent_uid,
            overwhelm_state=overwhelm_state,
            msg_count=_msg_count_today, last_msg_time=_last_msg_time_str,
            update_info=update_info_val, schedule_state=schedule_state,
            expression_export=expression_export,
            expression_feature=expression_feature,
            optimizer_export=optimizer_export,
        )

        from .config import BASE_DIR
        status_file = os.path.join(BASE_DIR, "dong_status.json")
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        try:
            from .dong_connector import push_event
            push_event("status_update", {"mood": mood, "fatigue": fatigue, "sleeping": sleeping, "bot_state": bot_state})
        except Exception as e:
            log(f"push_event status_update 失败: {e}")
    except Exception as e:
        log(f"状态导出异常: {e}")


def _build_system_prompt(uid, late, user_text=""):
    """代理：注入 last_active_time 后调用 core.system_prompt._build_system_prompt"""
    return _bsp(uid, late, user_text, last_active_time)


# ============ 消息合并AI判断 ============
# ============ 机器人主循环 ============
async def robot_loop():
    log("机器人循环开始")
    load_status()
    from .status import load_last_day, load_global_state, _status
    load_last_day()
    load_global_state()
    process_offline_life()
    update_mood_cycle()
    load_intimacy()  # #31 初始化亲密度

    # 加载或生成当日日程
    from .schedule import _schedule_system
    today_loaded = _schedule_system.load_today()
    if today_loaded:
        log(f"加载今日日程: {len(today_loaded.activities)}活动 起床{today_loaded.wake_time}")
    else:
        sched_mood = _status.get("mood", 50)
        sched_fatigue = _status.get("fatigue", 50)
        generate_daily_schedule(mood=sched_mood, fatigue=sched_fatigue)

    # 加载出厂人格档案（#1 出厂记忆蒸馏）
    factory_archive = await asyncio.to_thread(load_factory_archive)
    if factory_archive:
        log(f"出厂档案已加载: v{factory_archive.get('version', '?')}, "
            f"梗{len(factory_archive.get('inside_jokes', []))}个, "
            f"争吵模式{len(factory_archive.get('argument_patterns', []))}个")
    else:
        log("出厂档案未生成，运行 python -m dong.factory 蒸馏")

    # 启动时睡眠检测（Item 1: AI判断该睡/醒/告别后睡）
    global _last_msg_ts, _last_msg_uid, _last_msg_time_str, _msg_count_today
    global _need_sleep_goodbye, _sleep_goodbye_uid
    from .sleep_guardian import startup_sleep_check as _ssc
    was_sleeping = _status.get("sleeping", False)
    interruption = (time.time() - _last_msg_ts) / 60.0 if _last_msg_ts > 0 else 999
    was_chatting = 0 < interruption < 15
    startup_decision = _ssc(was_sleeping, was_chatting, interruption)
    log(f"启动睡眠检测: sleeping={was_sleeping} 中断{int(interruption)}min 聊天中={was_chatting} → {startup_decision}")
    if startup_decision == "sleep":
        _status["sleeping"] = True
    elif startup_decision == "goodbye_then_sleep":
        _need_sleep_goodbye = True
        _sleep_goodbye_uid = _last_msg_uid or MASTER_UID

    # 检查日程是否已生成（通过schedule模块）
    from .schedule import _daily_schedule
    if not _daily_schedule:
        generate_daily_schedule()

    global events_today_count, last_event_time, _msg_count_today
    from .status import weather_cache

    last_event_date = datetime.now().date()
    _msg_count_today = 0

    # 消息合并缓冲：同一用户短时间内多条消息合并为一条处理
    _pending_msgs = {}  # uid -> {"queue": [texts], "media": bool, "last_ts": float}
    _pending_tasks = {}  # uid -> asyncio.Task，保证每uid只有一个合并协程
    BATCH_WINDOW = 2.5  # 秒，等待更多消息的窗口

    while True:
        if datetime.now().date() != last_event_date:
            # 归档昨天日程
            asyncio.create_task(asyncio.to_thread(archive_today_schedule))
            events_today_count = 0
            _msg_count_today = 0
            last_event_date = datetime.now().date()
            sched_mood = _status.get("mood", 50)
            sched_fatigue = _status.get("fatigue", 50)
            generate_daily_schedule(mood=sched_mood, fatigue=sched_fatigue)
            reset_daily_flags()
            expression_reset_daily()  # 重置有罪推定每日计数
            # #6 日程经历记忆化 + #7 昨日小结（异步，不阻塞主循环）
            asyncio.create_task(asyncio.to_thread(archive_daily_experiences))
            asyncio.create_task(asyncio.to_thread(generate_daily_summary))

        try:
            auth_url = "ws://127.0.0.1:3001"

            async with websockets.connect(
                auth_url,
                ping_interval=20,
                open_timeout=10
            ) as ws:
                log("已连接OneBot WS")

                # ===== 测试模式检测 =====
                import os as _os_module
                _test_mode = _os_module.environ.get("DONG_TEST_MODE", "")
                _test_group_id = int(_os_module.environ.get("DONG_TEST_GROUP_ID", "0"))
                _test_duration_sec = int(_os_module.environ.get("DONG_TEST_DURATION_SEC", "2700"))
                _test_messages_file = _os_module.environ.get("DONG_TEST_MESSAGES_FILE", "")

                if _test_mode == "group":
                    log(f"[测试模式] 群聊测试启动, 目标群={_test_group_id}, 时长={_test_duration_sec}s")
                    # 启动自毁定时器
                    async def _auto_shutdown():
                        await asyncio.sleep(_test_duration_sec)
                        log("[测试模式] 时间到，退出")
                        import sys as _sys_module
                        _sys_module.exit(0)
                    asyncio.create_task(_auto_shutdown())

                    # 发送测试声明
                    async def _announce_test():
                        await asyncio.sleep(2)  # 等WS稳定
                        try:
                            from .interaction import send_group_msg as _sgm
                            await _sgm(_test_group_id, "我是AI冬，来测试的")
                            log("[测试模式] 已发送测试声明")
                        except Exception as e:
                            log(f"[测试模式] 声明发送失败: {e}")
                    asyncio.create_task(_announce_test())

                # 后台心跳：定时刷新状态文件（无消息时仪表盘也能看到实时状态）
                _heartbeat_running = True
                async def _heartbeat():
                    while _heartbeat_running:
                        await asyncio.sleep(30)
                        _export_status()
                        try:
                            from .core.health_registry import registry as _hr, CheckLevel as _CL
                            await asyncio.to_thread(_hr.run_all_due)
                            # FATAL告警 → QQ通知主人
                            if _hr.has_fatal:
                                for chk in _hr.get_all():
                                    is_fatal = (
                                        chk.consecutive_failures >= 5 or
                                        (chk.level == _CL.FATAL and chk.last_result is False)
                                    )
                                    if not is_fatal:
                                        continue
                                    prev = _last_fatal_notify.get(chk.name, 0)
                                    if chk.consecutive_failures == prev:
                                        continue
                                    _last_fatal_notify[chk.name] = chk.consecutive_failures
                                    from .config import MASTER_UID as _mu
                                    alert_text = f"⚠️ 健康告警 [{chk.name}] 连续失败{chk.consecutive_failures}次"
                                    await send_msg(ws, _mu, alert_text)
                                    save_bot_reply(_mu, alert_text)
                        except Exception:
                            pass
                heartbeat_task = asyncio.create_task(_heartbeat())
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        items = data if isinstance(data, list) else [data]
                        for item in items:
                            # ---- 测试模式：只处理群聊 ----
                            if _test_mode == "group":
                                if isinstance(item, dict) and item.get("message_type") == "group":
                                    gid = item.get("group_id")
                                    if gid != _test_group_id:
                                        continue
                                    uid = item.get("user_id")
                                    # 忽略自己发的消息
                                    if str(uid) == str(item.get("self_id", "")):
                                        continue
                                    # 只处理文本消息（简化版）
                                    raw_msg = item.get("raw_message", "") or item.get("message", "")
                                    if isinstance(raw_msg, list):
                                        raw_msg = "".join([s.get("data", {}).get("text", "") for s in raw_msg if s.get("type") == "text"])
                                    text = raw_msg.strip()
                                    if not text:
                                        continue
                                    log(f"[测试] 群{_test_group_id} QQ{uid}: {text[:50]}")

                                    # 简单回复逻辑（用test persona）
                                    try:
                                        from .interaction import send_group_msg as _send_group
                                        await asyncio.sleep(1)
                                        # 构建回复
                                        reply_text = await _generate_test_reply(text)
                                        if reply_text:
                                            await _send_group(_test_group_id, reply_text)
                                            log(f"[测试] 回复: {reply_text[:50]}")
                                            # 记录测试消息
                                            if _test_messages_file:
                                                try:
                                                    with open(_test_messages_file, "a", encoding="utf-8") as tf:
                                                        tf.write(json.dumps({
                                                            "text": reply_text,
                                                            "time": datetime.now().strftime("%H:%M:%S"),
                                                            "trigger": text[:60],
                                                        }, ensure_ascii=False) + "\n")
                                                except Exception:
                                                    pass
                                    except Exception as e:
                                        log(f"[测试] 回复异常: {e}")
                                continue
                            # ---- 正常模式：只处理私聊 ----
                            if isinstance(item, dict) and item.get("message_type") == "private":
                                uid = item.get("user_id")
                                _last_msg_ts = time.time()
                                _last_msg_uid = uid

                                # ===== 解析消息段数组（支持图片/语音）=====
                                msg_array = item.get("message", [])
                                text_parts = []
                                image_segs = []
                                record_segs = []
                                is_media = False
                                for seg in msg_array:
                                    stype = seg.get("type", "")
                                    sdata = seg.get("data", {})
                                    if stype == "text":
                                        text_parts.append(sdata.get("text", ""))
                                    elif stype == "image":
                                        image_segs.append(sdata)
                                    elif stype == "record":
                                        record_segs.append(sdata)
                                text = " ".join(text_parts).strip()
                                if not text and not image_segs and not record_segs:
                                    text = item.get("raw_message", "").strip()

                                # ===== 白名单校验提前：防止非白名单用户白嫖图片/语音API =====
                                if uid not in ALLOWED_USERS:
                                    log(f"  忽略非白名单用户(图片/语音阶段): {uid}")
                                    continue

                                # ===== 处理图片消息 =====
                                if image_segs and not text:
                                    result = await _process_image_message(image_segs[0], uid)
                                    if result:
                                        text, is_media, img_path = result
                                        # ★ 图片触发器检测：匹配到指令图 → 执行预设动作
                                        if img_path:
                                            try:
                                                from .command_channel import (match_trigger_image, is_debug_mode_active,
                                                    enter_debug_mode, exit_debug_mode, register_trigger_from_received)
                                                trigger_cfg = match_trigger_image(img_path, uid)
                                                if trigger_cfg:
                                                    action = trigger_cfg.get("action", "")
                                                    log(f"  图片触发器命中 → {action}")
                                                    resp_img = trigger_cfg.get("image", "")
                                                    reply_text = trigger_cfg.get("reply", "")
                                                    if resp_img and os.path.exists(resp_img):
                                                        await send_image_message(uid, resp_img)
                                                        log(f"  触发响应发图: {resp_img}")
                                                    if action == "enter_debug":
                                                        enter_debug_mode(uid)
                                                        log(f"  进入调试模式")
                                                    elif action == "exit_debug":
                                                        exit_debug_mode()
                                                        log(f"  退出调试模式")
                                                    if reply_text:
                                                        await send_msg(ws, uid, reply_text)
                                                        save_bot_reply(uid, reply_text)
                                                    continue
                                                # 未匹配已知触发器 但在调试模式中 → 动态注册为退出触发器
                                                elif is_debug_mode_active(uid):
                                                    reg_ok = register_trigger_from_received(
                                                        "退出触发器", img_path, "exit_debug",
                                                        image="", reply="嗯～"
                                                    )
                                                    if reg_ok:
                                                        exit_debug_mode()
                                                        await send_msg(ws, uid, "嗯～")
                                                        save_bot_reply(uid, "嗯～")
                                                        log(f"  动态注册退出触发器 + 退出调试模式")
                                                        continue
                                            except Exception as e:
                                                log(f"  触发器检查异常: {e}")
                                    else:
                                        continue
                                    msg_key = f"{uid}:img:{image_segs[0].get('file', '')}"
                                    now = datetime.now().timestamp()
                                    if msg_key in recent_messages and (now - recent_messages.get(msg_key, 0)) < 5:
                                        continue
                                    recent_messages[msg_key] = now
                                    log(f"收到图片文字: {text[:30]}")

                                # ===== 处理语音消息 =====
                                elif record_segs and not text:
                                    asr_result = await _process_voice_message(record_segs[0], uid, ws)
                                    if asr_result is None:
                                        continue
                                    text, is_media = asr_result

                                if not uid or not text:
                                    continue

                                # 缓存清理：超过阈值时清理过期条目
                                if len(recent_messages) > MESSAGE_CACHE_MAX_SIZE:
                                    _cleanup_message_cache()

                                # 优先使用 OneBot message_id 去重（更可靠）
                                msg_real_id = item.get("message_id")
                                if msg_real_id is not None:
                                    msg_key = f"{uid}:msgid:{msg_real_id}"
                                else:
                                    msg_key = f"{uid}:txt:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
                                now = datetime.now().timestamp()
                                if msg_key in recent_messages and (now - recent_messages[msg_key]) < 10:
                                    continue
                                recent_messages[msg_key] = now

                                log(f"收到 {uid}: {text[:30]}")

                                # ── 消息合并窗口：同用户短时间多条消息合并处理 ──
                                if uid not in _pending_msgs:
                                    _pending_msgs[uid] = {"queue": [], "media": False, "last_ts": 0}
                                _pending_msgs[uid]["queue"].append(text)
                                if is_media:
                                    _pending_msgs[uid]["media"] = True
                                _pending_msgs[uid]["last_ts"] = time.time()

                                # 取消该uid之前的合并任务（若有），保证只有一个协程处理
                                old_task = _pending_tasks.pop(uid, None)
                                if old_task and not old_task.done():
                                    old_task.cancel()

                                # 创建新的合并协程
                                async def _do_merge(_uid, _is_media):
                                    await asyncio.sleep(BATCH_WINDOW)
                                    entry = _pending_msgs.pop(_uid, None)
                                    if not entry or not entry["queue"]:
                                        return None, False
                                    queue = entry["queue"]
                                    return queue, entry["media"]

                                merge_task = asyncio.create_task(_do_merge(uid, is_media))
                                _pending_tasks[uid] = merge_task
                                queue, is_media = await merge_task
                                if not queue:
                                    continue
                                if len(queue) >= 2:
                                    text = _merge_if_related(queue, uid)
                                else:
                                    text = queue[0]
                                log(f"  合并消息 ({len(queue)}条): {text[:50]}")

                                _msg_count_today += 1
                                _last_msg_time_str = datetime.now().strftime("%H:%M:%S")
                                text = replace_cq_faces(text)

                                # ===== #20 主号城市自动更新 =====
                                from .config import MASTER_UID, MASTER_CITY_NAMES, MASTER_CITY
                                if uid == MASTER_UID:
                                    for city_name, coords in MASTER_CITY_NAMES.items():
                                        if f"我在{city_name}" in text or f"我在 {city_name}" in text:
                                            if MASTER_CITY["name"] != city_name:
                                                MASTER_CITY["name"] = city_name
                                                MASTER_CITY["lat"], MASTER_CITY["lon"] = coords
                                                log(f"  主号城市更新: {city_name} ({coords[0]}, {coords[1]})")
                                            break

                                if uid not in ALLOWED_USERS:
                                    log(f"  忽略非白名单用户: {uid}")
                                    continue
                                if text.startswith("@"):
                                    log("  指令跳过")
                                    continue

                                # ★ Claude会话模式 + /d c 桥接（→ handlers/dc_command.py）
                                from .handlers.dc_command import handle_dc_command
                                handled = await handle_dc_command(
                                    ws, uid, text, _claude_sessions, _save_claude_sessions,
                                    send_msg, MASTER_UID)
                                if handled:
                                    continue

                                save_chat_history(uid, text)
                                absorb_user_language(uid, text)  # #16 吸收对方用语
                                auto_intimacy_change(uid, text)  # #31 亲密度自动调整

                                # ===== 杏仁核快速路径（第一站，规则驱动，毫秒级）=====
                                amy_result = process_amygdala(uid, text)
                                # 快速激素跳变
                                if amy_result.trigger_type != "neutral":
                                    from .status import apply_amygdala_spike
                                    apply_amygdala_spike(amy_result.trigger_type, amy_result.valence, amy_result.arousal)
                                # 杏仁核劫持 → 直接触发超限peak
                                if amy_result.hijack:
                                    from .overwhelm import amygdala_hijack
                                    amygdala_hijack(uid, text)
                                # 威胁检测 → 记仇标记（带威胁等级）
                                if amy_result.threat_level >= 1:
                                    mark_grudge(uid, f"杏仁核威胁: {amy_result.describe()}", text[:100], amy_result.threat_level)
                                # 显著性标记（后续记忆存储时使用）
                                _pending_amygdala_salient = amy_result.salient

                                # #11 记仇系统 — 检测道歉/和解
                                _grudge_check(uid, text)

                                last_active_time[uid] = datetime.now()
                                note_interaction()
                                if not is_media:
                                    tick_visual_memory(uid)

                                # 天气关键词检测
                                if should_refresh_weather(text):
                                    from .status import weather_cache
                                    weather_cache["force_refresh"] = True
                                    log("  触发天气强制刷新")

                                # 状态更新
                                apply_mood_ripple()
                                apply_external_mood()  # #8 检查外部动态
                                update_fatigue()
                                update_mood_cycle()
                                update_pushpull(uid)
                                # 激素系统：检测事件类型并更新
                                event_type = detect_hormone_event(text, uid)
                                if event_type:
                                    update_hormones(event_type)
                                # 后台AI修正（不阻塞主循环）
                                asyncio.create_task(_ai_hormone_correct(text, uid))

                                # 日程：事件切换检测（AI填细节 + 过渡微事件）
                                trans = on_event_transition()
                                if trans.get("detail"):
                                    log(f"  日程细节: {trans['detail'][:40]}")
                                if trans.get("micro"):
                                    log(f"  微事件: {trans['micro']}")

                                # 导出实时状态（供仪表盘读取）
                                _export_status(uid)

                                # #23 主动终止对话检测（在回复前）
                                should_term, term_msg = should_terminate_conversation(uid, is_late_night())
                                if should_term and term_msg:
                                    await send_msg(ws, uid, term_msg)
                                    log(f"  主动终止对话: {term_msg}")
                                    # 如果消息带"睡"则进入睡眠
                                    if any(w in term_msg for w in ["睡", "晚安"]):
                                        from .status import _status
                                        _status["sleeping"] = True
                                        save_day_summary()
                                        from .status import save_status
                                        save_status()
                                        _export_status(uid)
                                        # 记录克制：选择了终止对话（后悔回顾素材）
                                        try:
                                            from .regret import record_restraint
                                            record_restraint(uid, f"深夜主动终止了对话", "太困了/该睡了")
                                        except Exception:
                                            pass
                                    continue

                                # 启动后告别再睡（Item 1: goodbye_then_sleep）
                                try:
                                    _nsg = _need_sleep_goodbye
                                except UnboundLocalError:
                                    _nsg = False
                                if _nsg:
                                    _need_sleep_goodbye = False
                                    from .status import save_status
                                    gb_uid = _sleep_goodbye_uid or uid
                                    goodbye_msgs = [
                                        "不行了太困了 我先睡 明天说",
                                        "困死了...先睡了 晚安",
                                        "撑不住了 我先倒下了 明天聊",
                                    ]
                                    gb = random.choice(goodbye_msgs)
                                    await send_msg(ws, gb_uid, gb)
                                    save_bot_reply(gb_uid, gb)
                                    log(f"  启动告别后入睡: {gb}")
                                    _status["sleeping"] = True
                                    save_status()
                                    _export_status(gb_uid)
                                    continue

                                # 睡眠处理
                                slp, slp_desc = check_sleep()
                                transition = get_sleep_transition()

                                # 早安 — 刚醒时主动发（必发语音）
                                if transition == "to_wake" and slp != "sleeping":
                                    morning_msg = get_morning_message(uid)
                                    if morning_msg:
                                        await send_msg(ws, uid, morning_msg)
                                        save_bot_reply(uid, morning_msg)
                                        log(f"  早安: {morning_msg}")
                                        audio_path = await asyncio.to_thread(text_to_speech, morning_msg)
                                        if audio_path:
                                            await send_voice_message(uid, audio_path)
                                            log(f"  早安语音已发送")

                                if slp == "sleep":
                                    from .status import _status
                                    _status["sleeping"] = True
                                    save_day_summary()
                                    from .status import save_status
                                    save_status()
                                    _export_status(uid)
                                    log(f"状态: 入睡了({slp_desc})")
                                    # 睡前告别（必发语音）
                                    bedtime_msg = get_bedtime_message(uid)
                                    if bedtime_msg:
                                        await send_msg(ws, uid, bedtime_msg)
                                        save_bot_reply(uid, bedtime_msg)
                                        log(f"  睡前告别: {bedtime_msg}")
                                        audio_path = await asyncio.to_thread(text_to_speech, bedtime_msg)
                                        if audio_path:
                                            await send_voice_message(uid, audio_path)
                                            log(f"  晚安语音已发送")
                                    # 触发优化Agent（异步后台，不阻塞sleep循环）
                                    from .config import OPTIMIZER_ENABLED
                                    if OPTIMIZER_ENABLED:
                                        asyncio.create_task(_run_optimizer_background())
                                    continue
                                elif slp == "sleeping":
                                    # 休眠紧急唤醒（双通道判断）
                                    try:
                                        from .sleep_guardian import handle_message_during_sleep as _hds, \
                                            get_wake_reply_prompt as _gwp
                                        wake_decision = _hds(uid, text)
                                        if wake_decision == "wake":
                                            log(f"  休眠紧急唤醒: uid={uid}")
                                            # 简化唤醒 prompt + 迷糊回复
                                            wake_prompt = _gwp()
                                            late = is_late_night()
                                            drowsy = await chat(uid, text, late,
                                                                extra_system=wake_prompt,
                                                                temperature=0.5, max_tokens=60)
                                            if drowsy:
                                                drowsy = drowsy[:80]  # 迷糊回复要短
                                                delay = random.randint(3, 8)
                                                await asyncio.sleep(delay)
                                                await send_msg(ws, uid, drowsy)
                                                save_bot_reply(uid, drowsy)
                                                log(f"  迷糊回复: {drowsy}")
                                                # 记录后悔回顾的素材：克制了还是醒了
                                                try:
                                                    from .regret import record_restraint as _rr
                                                    _rr(uid, f"深夜被{uid}的消息叫醒了", "本来在睡觉")
                                                except Exception:
                                                    pass
                                                # 据激素决定继续睡还是失眠
                                                if _status.get("fatigue", 50) > 70 and _status.get("mood", 50) < 60:
                                                    _status["sleeping"] = True
                                                else:
                                                    _status["sleeping"] = False
                                                    log("  紧急唤醒后进入失眠状态")
                                        # else: C → 继续睡，消息已在 handle_message_during_sleep 中入队
                                    except Exception as e:
                                        log(f"  休眠守护异常: {e}")
                                    continue
                                elif slp == "insomnia_wake":
                                    log(f"状态: 失眠醒来({slp_desc})")
                                    # #6 经历记录：失眠
                                    log_experience("深夜失眠醒了", "insomnia")
                                    # 失眠醒来的深夜音乐分享
                                    if uid == MASTER_UID and _status["mood"] < 50 and random.random() < 0.15:
                                        await asyncio.sleep(random.randint(2, 5))
                                        music_msgs = [
                                            "刚练了一遍曲子 还是睡不着",
                                            "失眠了 弹了会儿琴",
                                            "睡不着...听会歌吧",
                                        ]
                                        await send_msg(ws, uid, random.choice(music_msgs))
                                        save_bot_reply(uid, random.choice(music_msgs))
                                elif slp == "early_wake":
                                    log(f"状态: 早醒({slp_desc})")

                                from .status import _voice_state
                                voice_negotiating = _voice_state.get(uid, {}).get("state") == "requested"
                                if not is_media and not voice_negotiating and should_not_reply(text):
                                    log(f"  触发随机不回")
                                    continue

                                delay = get_reply_delay(text, get_history(uid))
                                frag = maybe_life_fragment()
                                if frag:
                                    if frag["delay_bonus"] > 0:
                                        delay += frag["delay_bonus"]
                                        log(f"  碎片延迟: +{frag['delay_bonus']}s")
                                log(f"  延迟 {delay} 秒")
                                await asyncio.sleep(delay)

                                late = is_late_night()

                                # 超限检测（在构建prompt前，让超限状态影响prompt）
                                hist = get_history(uid)
                                await check_overwhelm(uid, text, hist)

                                # ★ 表演规则调度（在构建system prompt前）
                                expr_context = {
                                    "is_late": late,
                                    "mood": _status.get("mood", 50),
                                    "fatigue": _status.get("fatigue", 50),
                                    "overwhelm_state": get_overwhelm_state(),
                                    "intimacy_level": get_intimacy_level(uid),
                                    "history": get_history(uid),
                                    "trans": trans,
                                    "last_active": last_active_time.get(uid),
                                    "amy_hijack": amy_result.hijack,
                                }
                                expression = resolve_expression(uid, text, expr_context)
                                _active_expression._current = expression

                                # 构建system提示
                                extra_system = _build_system_prompt(uid, late, text)

                                # ★ QQ指令通道：主人/d前缀 → 指令模式
                                try:
                                    from .command_channel import detect_command, build_command_prompt, resolve_image
                                    cmd_text = detect_command(text, uid)
                                    if cmd_text:
                                        # 退出调试模式标记
                                        if cmd_text == "__EXIT_DEBUG__":
                                            await send_msg(ws, uid, "嗯～")
                                            save_bot_reply(uid, "嗯～")
                                            log(f"  文本退出调试模式")
                                            continue
                                        log(f"  指令通道: {cmd_text[:50]}")

                                        # ★ Agent Loop 路由
                                        from . import agent_loop as _al

                                        # 恢复暂停的任务
                                        if cmd_text.strip() in ["继续", "好了", "已完成", "已处理", "好了继续"]:
                                            if _al.has_paused(uid):
                                                agent_result = await _al.resume_agent(uid)
                                                reply = agent_result.get("message", "已继续")
                                                await send_msg(ws, uid, reply)
                                                save_bot_reply(uid, reply)
                                                log(f"  → Agent 恢复: {reply[:50]}")
                                            else:
                                                await send_msg(ws, uid, "没有暂停中的任务哦")
                                            continue

                                        # /d health 健康检查命令（含自然语言总结）
                                        if cmd_text.strip() in ("health", "健康", "状态检查", "状态"):
                                            try:
                                                from .core.health_summary import generate_health_summary
                                                summary = generate_health_summary()
                                            except Exception:
                                                summary = "总结生成失败"
                                            from .core.health_registry import registry as _hr
                                            snap = _hr.snapshot()
                                            lines = [summary, "--- 详细 ---"]
                                            for n, info in snap.items():
                                                icon = "OK" if info.get("last_result") else ("FIX" if info.get("has_auto_fix") else "FAIL")
                                                cf = info.get("consecutive_failures", 0)
                                                lines.append(f"  [{icon}] {n}" + (f" fail={cf}" if cf else ""))
                                            try:
                                                from .core.api_gateway import gateway
                                                gs = gateway.stats()
                                                for pn, ci in gs.get("circuits", {}).items():
                                                    state_icon = {"CLOSED":"OK","HALF_OPEN":"WARN","OPEN":"FAIL"}.get(ci.get("state",""),"?")
                                                    lines.append(f"  [{state_icon}] API:{pn} fail={ci.get('failures',0)}")
                                                if gs.get("global_cooldown"):
                                                    lines.append(f"  [FAIL] 全局冷却 {gs.get('cooldown_remaining',0):.0f}s")
                                            except Exception:
                                                pass
                                            reply = "\n".join(lines)
                                            await send_msg(ws, uid, reply)
                                            save_bot_reply(uid, reply)
                                            continue

                                        # /d quality 回复质量采样
                                        if cmd_text.strip() in ("quality", "质量", "质量检查"):
                                            await send_msg(ws, uid, "正在采样回复质量...")
                                            try:
                                                from .core.quality_monitor import run_quality_sample, record_quality, check_quality_drift
                                                metrics = await asyncio.to_thread(run_quality_sample)
                                                record_quality(metrics)
                                                drift = check_quality_drift()
                                                lines = [
                                                    f"质量采样 ({metrics.sample_count}条)",
                                                    f"  均长: {metrics.avg_length:.1f}字  短回复率: {metrics.short_rate:.0%}",
                                                    f"  口癖率: {metrics.tsundere_rate:.0%}  AI套话: {metrics.ai_cliche_rate:.0%}",
                                                ]
                                                if drift:
                                                    lines.append(f"  {drift}")
                                                reply = "\n".join(lines)
                                            except Exception as e:
                                                reply = f"质量采样失败: {e}"
                                            await send_msg(ws, uid, reply)
                                            save_bot_reply(uid, reply)
                                            continue

                                        # 新任务
                                        agent_keywords = ["打开", "发消息给", "帮我看", "帮我查", "帮我发", "操作",
                                                         "登录", "保存", "复制", "粘贴", "搜索", "订阅",
                                                         "输入", "打字", "点击", "下载", "上传", "看看"]
                                        is_agent_task = any(kw in cmd_text for kw in agent_keywords)
                                        if is_agent_task and slp not in ("sleeping", "sleep"):
                                            # 如果有旧暂停任务，清理
                                            if _al.has_paused(uid):
                                                _al.clear_paused(uid)
                                            await send_msg(ws, uid, "收到，我来操作...")
                                            log(f"  → Agent Loop 接管")
                                            agent_result = await _al.run_computer_task(cmd_text, uid)
                                            reply = agent_result.get("message", "没完成") if isinstance(agent_result, dict) else str(agent_result)
                                            await send_msg(ws, uid, reply)
                                            save_bot_reply(uid, reply)
                                            continue

                                        from .tools import build_tools_prompt, parse_tool_call, execute_tool, strip_tool_tag
                                        cmd_system = build_command_prompt() + "\n\n" + build_tools_prompt()
                                        cmd_rep = await asyncio.to_thread(chat, cmd_text, uid, cmd_system)
                                        # 工具调用循环（最多3轮，支持多步操作如 截图→找坐标→点击）
                                        tool_used = False
                                        MAX_CMD_TOOLS = 8
                                        for attempt in range(MAX_CMD_TOOLS):
                                            if not cmd_rep:
                                                break
                                            try:
                                                ti = parse_tool_call(cmd_rep or "")
                                                if ti:
                                                    tool_used = True
                                                    t_name, t_params = ti
                                                    log(f"  指令工具[{attempt+1}/{MAX_CMD_TOOLS}]: {t_name} {t_params}")
                                                    t_result = await asyncio.to_thread(execute_tool, t_name, t_params, uid)
                                                    log(f"  指令结果: {t_result[:120]}...")
                                                    is_last = (attempt >= MAX_CMD_TOOLS - 1)
                                                    if is_last:
                                                        cmd_context = cmd_system + f"\n\n【工具调用结果】\n结果：\n{t_result}\n\n请基于结果简短回复（≤50字）。不要再调用工具。"
                                                    else:
                                                        cmd_context = cmd_system + f"\n\n【工具调用结果】\n结果：\n{t_result}\n\n如果还需要其他操作（如点击）可以继续调用工具，否则总结结果（≤50字）。"
                                                    cmd_rep2 = await asyncio.to_thread(chat, cmd_text, uid, cmd_context)
                                                    if cmd_rep2 and cmd_rep2.strip():
                                                        cmd_rep = cmd_rep2
                                                    else:
                                                        break
                                                else:
                                                    if attempt == 0:
                                                        log(f"  指令未用工具，强制重试...")
                                                        cmd_system_retry = cmd_system + "\n\n【重要】你必须调用工具来执行指令！直接插入 [TOOL:工具名]参数[/TOOL]。不知道该用哪个也选最相关的。"
                                                        cmd_rep = await asyncio.to_thread(chat, cmd_text, uid, cmd_system_retry)
                                                    else:
                                                        break  # 后续轮次不用工具是正常的，可能是总结回复
                                            except Exception as e:
                                                log(f"  指令工具异常: {e}")
                                                break
                                        if not tool_used:
                                            log(f"  指令未执行：LLM未调用工具")
                                        cmd_rep = strip_tool_tag(cmd_rep or "")
                                        # 检测 [IMAGE:图名] → 发送图片
                                        cmd_text_clean, img_path = resolve_image(cmd_rep)
                                        if img_path and os.path.exists(img_path):
                                            await send_image_message(uid, img_path)
                                            log(f"  指令发图: {img_path}")
                                        if cmd_text_clean:
                                            await send_msg(ws, uid, cmd_text_clean)
                                            save_bot_reply(uid, cmd_text_clean)
                                            log(f"  指令回复: {cmd_text_clean[:50]}")
                                        continue
                                except Exception as e:
                                    log(f"  指令通道异常: {e}")

                                if was_ignored(get_history(uid)) and random.random() < 0.15:
                                    log("  触发情感波动：不回复")
                                    continue

                                if is_difficult_question(text, uid):
                                    await send_msg(ws, uid, random.choice(["等等让我想想", "嗯...我想想", "等下哈", "让我想想哈"]))
                                    async def generate_reply(q=text, u=uid, es=extra_system, salient=_pending_amygdala_salient, amy_val=amy_result.valence, amy_aro=amy_result.arousal):
                                        rep = await asyncio.to_thread(chat, q, u, es)
                                        if rep:
                                            # ★ 普通陪聊路径：跳过工具调用 (防止注入，工具只在/d命令通道生效)
                                            from .tools import strip_tool_tag
                                            rep = strip_tool_tag(rep or "")
                                            # ★ 继续原有后处理
                                            rep, had_hand, had_cute, mistake = process_reply(u, rep, q, get_history(u), late)
                                            if should_chase_up(rep, q, get_history(u)):
                                                rep = rep + " " + get_chase_up_question(q)

                                            add_history(u, q, rep)
                                            sent_lines, msg_ids = await send_msg_lines(ws, u, rep)
                                            await _finalize_reply(ws, u, rep, q, sent_lines, msg_ids, late,
                                                                   amy_val, amy_aro, salient, had_hand, had_cute)
                                    asyncio.create_task(generate_reply())
                                else:
                                    rep = await asyncio.to_thread(chat, text, uid, extra_system)
                                    # ★ 普通陪聊路径：跳过工具调用解析 (防止OCR/网页注入攻击)
                                    # 工具调用只在 /d 命令通道生效
                                    from .tools import strip_tool_tag
                                    try:
                                        rep = strip_tool_tag(rep or "")
                                    except Exception as e:
                                        log(f"  剥离工具标签异常: {e}")
                                    if rep:
                                        rep, had_hand, had_cute, mistake = process_reply(uid, rep, text, get_history(uid), late)
                                        if should_chase_up(rep, text, get_history(uid)):
                                            rep = rep + " " + get_chase_up_question(text)

                                        # 超限：检测超越反应
                                        maybe_trigger_breakthrough(rep)

                                        # 超限：生理反应独白记录
                                        phys_mono = generate_physiological_monologue()
                                        if phys_mono:
                                            store_internal_monologue(phys_mono, "overwhelm")

                                        add_history(uid, text, rep)

                                        # 超限：碎片化分段发送
                                        frags = fragment_text(rep)
                                        if len(frags) > 1:
                                            log(f"  超限碎片化: {len(frags)}段")
                                            msg_ids = []
                                            for i, frag in enumerate(frags):
                                                if i > 0:
                                                    ow_delay = get_unstable_delay()
                                                    if ow_delay > 0:
                                                        await asyncio.sleep(ow_delay)
                                                sent = await send_msg(ws, uid, frag)
                                                if sent:
                                                    msg_ids.append(sent)
                                                # 超限：随机撤回某一段
                                                if should_retract():
                                                    await asyncio.sleep(random.uniform(1, 4))
                                                    rid = msg_ids[-1] if msg_ids else sent
                                                    if rid:
                                                        await withdraw_message(rid)
                                                        log(f"  超限撤回: msg_id={rid}")
                                            sent_lines, _ = None, msg_ids
                                        else:
                                            sent_lines, msg_ids = await send_msg_lines(ws, uid, rep)
                                            # 超限：随机撤回
                                            if should_retract() and msg_ids:
                                                await asyncio.sleep(random.uniform(1, 4))
                                                for mid in msg_ids:
                                                    await withdraw_message(mid)
                                                log(f"  超限撤回: msg_ids={msg_ids}")

                                        await _finalize_reply(ws, uid, rep, text, sent_lines, msg_ids, late,
                                                               amy_result.valence, amy_result.arousal,
                                                               _pending_amygdala_salient, had_hand, had_cute)

                                for uid2 in ALLOWED_USERS:
                                    # 身体碎片(silence)替代本轮主动事件，只跳过当前 uid
                                    if uid2 == uid and expression and expression.feature == "body_feeling" and expression.params.get("gap_type") == "silence":
                                        continue
                                    await maybe_proactive_event(ws, uid2)
                                # 清除破防后标记（下一轮不再慌张）
                                clear_post_breakthrough()
                                # 冲突追踪器 → 通知主人
                                try:
                                    from .conflict_tracker import get_pending_notification as _ct_notify
                                    notify_text = _ct_notify()
                                    if notify_text:
                                        from .config import MASTER_UID as _muid
                                        await send_msg(ws, _muid, notify_text)
                                except Exception:
                                    pass

                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        log(f"消息处理异常: {e}")
                        import traceback as _tb2
                        log(_tb2.format_exc())

                # WS循环结束 → 停心跳
                _heartbeat_running = False
                try:
                    heartbeat_task.cancel()
                except NameError:
                    pass
        except websockets.exceptions.ConnectionClosed as e:
            log(f"WS断开: {e}")
            _heartbeat_running = False
            try:
                heartbeat_task.cancel()
            except NameError:
                pass
        except Exception as e:
            log(f"连接失败: {e}")
            try:
                _heartbeat_running = False
            except NameError:
                pass
            try:
                heartbeat_task.cancel()
            except NameError:
                pass
        # ★ QQ断线自救：用Agent能力打开微信通知主人（仅触发一次，防重复）
        global _qq_rescue_sent
        if not _qq_rescue_sent:
            _qq_rescue_sent = True
            try:
                await do_qq_rescue()
                log("QQ断线自救已触发")
            except Exception:
                _qq_rescue_sent = False  # 失败则允许下次重试

        log("5秒后重连...")
        await asyncio.sleep(5)


# ============ 优化代理 ============

_optimizer_last_run = None  # datetime
OPTIMIZER_MIN_INTERVAL_HOURS = 4


async def _finalize_reply(ws, uid, rep, text, sent_lines, msg_ids, late,
                           amy_val, amy_aro, salient, had_hand, had_cute):
    """回复后处理：日志+保存+杏仁核+记忆+撤回+语音+图+表情+闪回"""
    log(f"  回复: {rep[:50]}")
    save_bot_reply(uid, rep)
    try:
        from .dong_connector import push_event
        push_event("reply", {"uid": str(uid), "text": rep[:100]})
    except Exception as e:
        log(f"push_event reply 失败: {e}")
    if amy_val != 0 or amy_aro > 0.2:
        amygdala_learn(uid, text, amy_val, amy_aro)
    asyncio.create_task(auto_summarize_turn(uid, text, rep))
    if should_remember(text):
        add_memory(uid, text, is_important=True, amygdala_salient=salient)
        log("  重要记忆已保存")
    await maybe_recall(ws, uid, sent_lines, msg_ids, had_hand or had_cute)
    clear_offline_events()
    # 发语音
    if not should_retract():
        voice_action, _ = should_trigger_voice(uid, rep, text)
        if voice_action == "send":
            from .api import sanitize_for_tts
            tts_text = sanitize_for_tts(rep)
            if tts_text and len(tts_text) >= 2:
                audio_path = await asyncio.to_thread(text_to_speech, tts_text)
                if audio_path:
                    await send_voice_message(uid, audio_path)
    # 盗图发图
    if any(kw in rep for kw in ["给你看", "你看", "照片", "发张图", "看看"]) and random.random() < 0.15:
        img_path = pick_image(uid)
        if img_path and os.path.exists(img_path):
            await send_image_message(uid, img_path)
    # 发表情包
    if should_send_emoji(uid, rep):
        emoji_path, emoji_name = pick_emoji(uid, rep, text)
        if emoji_path:
            await send_emoji_message(uid, emoji_path)
    # 记忆闪回
    fb = maybe_memory_flashback(uid)
    if fb:
        await asyncio.sleep(random.randint(2, 5))
        await send_msg(ws, uid, fb)
        save_bot_reply(uid, fb)
    # 独白提取
    mono = maybe_recall_monologue(uid)
    if mono:
        await asyncio.sleep(random.randint(2, 5))
        await send_msg(ws, uid, mono)
        save_bot_reply(uid, mono)


async def _run_optimizer_background():
    """异步后台运行优化代理。不阻塞主循环。"""
    global _optimizer_last_run

    now = datetime.now()
    if _optimizer_last_run:
        hours_since = (now - _optimizer_last_run).total_seconds() / 3600
        if hours_since < OPTIMIZER_MIN_INTERVAL_HOURS:
            log(f"[优化] 跳过: 距上次仅{hours_since:.1f}h")
            return

    _optimizer_last_run = now
    log("===== 优化代理启动 =====")

    try:
        from .optimizer import run_optimizer
        deployed = await run_optimizer()
        log(f"===== 优化代理完成: {'已部署新版本' if deployed else '未部署'} =====")
    except Exception as e:
        log(f"===== 优化代理异常: {e} =====")
        import traceback as tb
        log(tb.format_exc())


async def main():
    from .core.logging_setup import setup_logging
    setup_logging()
    log("===== 冬 全隐藏版启动 =====")

    # ── 优雅关闭：SIGINT → 保存状态后退出 ──
    _shutdown_done = False
    async def _shutdown():
        nonlocal _shutdown_done
        if _shutdown_done:
            return
        _shutdown_done = True
        log("收到关闭信号，保存状态...")
        _save_claude_sessions()
        try:
            from .status import save_status
            save_status()
        except Exception:
            log("保存状态失败", exc_info=True)
        try:
            save_day_summary()
        except Exception:
            pass
        log("状态已保存，退出")
        import os as _os
        _os._exit(0)

    def _signal_handler():
        import asyncio as _aio
        try:
            loop = _aio.get_event_loop()
            loop.call_soon_threadsafe(lambda: _aio.ensure_future(_shutdown()))
        except Exception:
            pass

    import signal
    signal.signal(signal.SIGINT, lambda sig, frame: _signal_handler())
    _load_claude_sessions()
    log_update("系统启动", update_type="startup")

    # ★ Claude守护进程（GLM方案四：watchdog监听cmd→CLI处理→冬发QQ）
    try:
        import threading as _th
        def _start_daemon():
            from .bridge.daemon import start_daemon
            start_daemon()
        _th.Thread(target=_start_daemon, daemon=True, name="claude-daemon").start()
        log("  Claude守护进程已启动")
    except Exception as _de:
        log(f"  Claude守护进程启动失败: {_de}")
    init_media_dirs()
    log(f"  ffmpeg: {'可用' if shutil.which('ffmpeg') else '未安装（ASR降级模式）'}")

    # 不变式2：API配置名字化迁移 + 网关绑定（必须在健康检查之前）
    from .config import migrate_to_registry
    migrate_to_registry()
    from .core.config_naming import registry as _cfg_registry
    from .core.api_gateway import bind_registry
    bind_registry(_cfg_registry)
    log("API网关已绑定配置注册表")

    # ★ 启动健康检查（数据自愈 + NapCat + API + 依赖）
    bind_from_config_module()
    startup_report = run_startup_checks()
    log(startup_report.summary())
    if startup_report.has_fatal:
        log("⛔ FATAL 检查未通过，阻止启动")
        return
    if startup_report.total_repairs > 0:
        log(f"启动自愈: 共修复 {startup_report.total_repairs} 处数据")
    # 模块自动发现（扫描新模块的 @bus.on_phase 注册）
    discover_and_load("dong")

    # 启动仪表盘（daemon线程，随主进程退出）
    from .dashboard import start as start_dashboard, DASHBOARD_PORT
    dash_thread = threading.Thread(target=start_dashboard, daemon=True)
    dash_thread.start()
    await asyncio.sleep(0.3)
    if is_port_open(port=DASHBOARD_PORT):
        log(f"仪表盘已启动 → http://localhost:{DASHBOARD_PORT}")
    else:
        log("仪表盘启动中...")

    # 启动前端连接器（daemon线程，给桌宠提供 HTTP API）
    from .dong_connector import start_server as start_connector, PORT as CONNECTOR_PORT
    conn_thread = threading.Thread(target=start_connector, daemon=True)
    conn_thread.start()
    await asyncio.sleep(0.3)
    if is_port_open(port=CONNECTOR_PORT):
        log(f"前端连接器已启动 → http://localhost:{CONNECTOR_PORT}")
    else:
        log("前端连接器启动中...")

    init_weather()
    _auto_load_cloned_voice()

    if not is_port_open():
        start_napcat()
        if not wait_for_napcat():
            log("NapCat无法启动，退出")
            return
    else:
        log("NapCat已在运行")
    await robot_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        log("冬已退出")
