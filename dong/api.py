"""
冬 · API模块
- chat() 主聊天API（自动路由切换 + 熔断）
- apply_output_filter() 输出格式安全锁
- 请求缓存（避免重复请求）
"""
import hashlib
import random
import re
from datetime import datetime

import requests

from .config import _get_cfg, _switch_api, API_CONFIGS
from .log import log


# ============ 请求缓存 ============
import threading

_API_CACHE = {}  # {hash: (response, timestamp)}
_API_CACHE_LOCK = threading.Lock()
_API_CACHE_TTL = 60  # 缓存有效期（秒）
_API_CACHE_MAX_SIZE = 100  # 最大缓存条数


def _get_cache_key(text, uid, extra_system=None):
    """生成请求缓存键"""
    import json as _json
    extra_hash = hashlib.sha256(
        (extra_system if extra_system else "").encode()
    ).hexdigest()[:16] if extra_system else ""
    key_data = f"{uid}:{text}:{extra_hash}"
    return hashlib.md5(key_data.encode()).hexdigest()


def _get_cached_response(cache_key):
    """获取缓存的响应（线程安全）"""
    with _API_CACHE_LOCK:
        if cache_key in _API_CACHE:
            response, timestamp = _API_CACHE[cache_key]
            if (datetime.now() - timestamp).total_seconds() < _API_CACHE_TTL:
                return response
            else:
                del _API_CACHE[cache_key]
    return None


def _set_cached_response(cache_key, response):
    """设置缓存的响应（线程安全）"""
    with _API_CACHE_LOCK:
        if len(_API_CACHE) >= _API_CACHE_MAX_SIZE:
            oldest_keys = sorted(_API_CACHE.items(), key=lambda x: x[1][1])[:10]
            for k, _ in oldest_keys:
                del _API_CACHE[k]
        _API_CACHE[cache_key] = (response, datetime.now())


# ============ 输出格式安全锁 ============
# 【已废弃】2026-05 — 以下 _sanitize_response_content / apply_output_filter
# 已被 core/api_gateway.py 的网关内建过滤替代。此处保留作为回退参考，不再被调用。
# 视角安全：防止备用模型返回包含对话历史/用户发言的内容
def _sanitize_response_content(content: str) -> str:
    """防止模型视角错乱：若返回内容包含User/用户发言，只取最后一段Assistant内容"""
    if not content or len(content) < 10:
        return content
    markers = ["User:", "Assistant:", "用户：", "冬：", "user:", "assistant:"]
    found_markers = [m for m in markers if m in content]
    if len(found_markers) >= 2:
        parts = re.split(r'(?:User:|Assistant:|用户：|冬：|user:|assistant:)\s*', content)
        # 取最后一段有意义的内容
        for p in reversed(parts):
            p = p.strip()
            if p and len(p) >= 2:
                log(f"  视角安全锁: 从多轮内容中截取最后一段 ({len(content)}→{len(p)}字)")
                return p
    return content
_SAFE_PARENS = {"不是", "bushi", "没有", "真的", "别", "来", "没", "嗯", "啊",
                "哦", "哈", "好", "行", "对", "错", "啥", "咋", "诶", "哼", "呀"}

_ACTION_VERBS = ["夹着", "顿了一下", "挑眉", "思考", "手指", "从兜", "深吸",
                 "吐出", "站起身", "转过头", "看了一眼", "皱眉", "嘴角", "抬起",
                 "放下", "摸出", "拿起", "指了指", "叹了口气", "捋了", "撩了",
                 "踢了", "踩了", "推了", "拉了", "拍了拍", "敲了", "点开", "滑动",
                 "打字", "耸了耸肩", "点了根", "叼着", "呼出一口", "掐灭", "弹了弹",
                 "推门", "躺在", "翻了个身", "打了个", "揉了揉", "瞥了一眼"]

_ACTION_SINGLE = {"笑", "哭", "叹", "抖", "愣", "呆", "看", "盯", "瞥",
                  "站", "坐", "躺", "走", "跑", "跳", "爬", "喊", "叫",
                  "咳", "喘", "哼", "啧", "嘶", "嘘", "吹", "点"}


def apply_output_filter(text):
    """过滤动作描写括号，保留真实口癖。返回(filtered_text, removed_chars)"""
    if not text:
        return text, 0

    def _is_speech(content):
        content = content.strip()
        if content in _SAFE_PARENS:
            return True
        if len(content) <= 2:
            if content in _ACTION_SINGLE:
                return False
            if not any(v in content for v in _ACTION_VERBS):
                return True
        return False

    def _filter(m):
        return m.group(0) if _is_speech(m.group(1).strip()) else ""

    filtered = text
    filtered = re.sub(r'（([^）]*)）', _filter, filtered)
    filtered = re.sub(r'\(([^)]*)\)', _filter, filtered)
    filtered = re.sub(r' +', ' ', filtered).strip()
    filtered = re.sub(r'\n +', '\n', filtered)

    removed = len(text.replace(" ", "")) - len(filtered.replace(" ", ""))
    if removed > 2:
        log(f"  安全锁: 删除{removed}字动作描写")
    return filtered, removed


# ============ 兜底回复 ============
_FALLBACKS_SHORT = ["哼", "才没有呢", "谁理你啊", "烦死了啦", "不要！"]
_FALLBACKS_LONG = ["才不要告诉你", "自己猜啦", "忙着呢", "才不困", "别管我"]
_FALLBACKS = _FALLBACKS_SHORT + _FALLBACKS_LONG


# ============ TTS语音文本过滤 ============
def sanitize_for_tts(text: str) -> str:
    """TTS前剥离非台词内容：内心独白、动作描写、撤回标记、括号内容"""
    # 去掉【内心独白】【心声】等标记段落
    text = re.sub(r'【[^】]*内心[^】]*】[^。！？\n]*[。！？]?', '', text)
    text = re.sub(r'【[^】]*独白[^】]*】[^。！？\n]*[。！？]?', '', text)
    text = re.sub(r'【[^】]*心声[^】]*】[^。！？\n]*[。！？]?', '', text)
    # 去掉（动作描写）
    text = re.sub(r'（[^）]*(?:夹着|顿|挑眉|思考|手指|深吸|吐出|站起身|转过头|皱眉|嘴角|抬起|放下|摸出|拿起|指了|叹了|捋了|撩了|踢了|踩了|推了|拉了|拍了拍|敲了|点开|滑动|打字|耸肩|点了根|叼着|呼出|掐灭|弹了|推门|躺在|翻身|打了|揉了揉|瞥了)[^）]*）', '', text)
    # 去掉（撤回）
    text = text.replace('（撤回）', '').replace('(撤回)', '')
    # 合并空白
    text = re.sub(r' +', ' ', text).strip()
    text = re.sub(r'\n +', '\n', text)
    return text


def _call_api(cfg, messages):
    """执行API调用的核心逻辑"""
    try:
        r = requests.post(
            f"{cfg.api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": cfg.model,
                "temperature": 1.0,
                "max_tokens": 1500,
                "messages": messages
            },
            timeout=20
        )
        log(f"API[{cfg.name}] {r.status_code}")
        if r.status_code != 200:
            log(f"API[{cfg.name}] 非200状态码: {r.status_code}, body={r.text[:200]}")
        return r
    except Exception as e:
        log(f"API异常 [{cfg.name}]: {e}")
        raise


# ============ 主聊天API ============
def chat(text, uid=None, extra_system=None):
    """调用AI——通过统一网关（路由/熔断/过滤/缓存全部由网关处理）"""
    from .core.api_gateway import gateway
    from .config import MASTER_UID as _muid_cache
    # 构建消息
    if uid:
        from .memory import build_messages_with_history
        messages = build_messages_with_history(uid, text, extra_system)
    else:
        from .persona import _get_personas
        messages = [
            {"role": "system", "content": _get_personas()["others"]},
            {"role": "user", "content": text[:200]}
        ]
    # 指令/工具调用类不走缓存
    is_command = (uid == _muid_cache) and (extra_system and "指令" in str(extra_system)[:50])
    result = gateway.call_chat(
        messages,
        task="chat",
        use_cache=not is_command,
        filter_output=True,
    )
    return result
