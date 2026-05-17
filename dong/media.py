"""
冬 · 媒体模块
- 媒体下载 / base64编码（支持下载队列）
- 图片索引（盗图仓库）
- 表情包系统
- 视觉识别 (chat_vision)
- 语音转文字 (ASR)
- 文字转语音 (TTS + 声音克隆)
- OneBot media API
- PCM→WAV 包装

重构优化：添加下载队列，避免并发冲突
"""
import base64
import json
import os
import random
import shutil
import subprocess
import struct
import io
import uuid
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from queue import Queue, Empty
from threading import Lock
from typing import Optional, Dict, List, Tuple, Any, Callable

import requests

from .config import (
    MEDIA_DIR, IMAGE_DIR, AUDIO_RECEIVED_DIR, AUDIO_GENERATED_DIR,
    IMAGE_INDEX_FILE, EMOJI_DIR, EMOJI_INDEX_FILE,
    CLONED_VOICE_PATH, SENDBOT_API, _get_cfg,
)
from .log import log


# ============ 下载队列系统 ============
@dataclass
class DownloadTask:
    """下载任务"""
    url: str
    save_path: str
    timeout: int = 10
    callback: Optional[Callable] = None
    priority: int = 0  # 优先级，数字越大优先级越高


class DownloadQueue:
    """下载队列管理器"""
    
    def __init__(self, max_workers: int = 3, max_retries: int = 2):
        self._queue: List[DownloadTask] = []
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._max_retries = max_retries
        self._running = False
        self._results: Dict[str, Any] = {}
    
    def add_task(self, task: DownloadTask) -> str:
        """添加下载任务，返回任务ID"""
        with self._lock:
            task_id = f"{task.url}_{datetime.now().timestamp()}"
            self._queue.append(task)
            # 按优先级排序
            self._queue.sort(key=lambda x: x.priority, reverse=True)
            return task_id
    
    def add_tasks_batch(self, tasks: List[DownloadTask]) -> List[str]:
        """批量添加任务"""
        task_ids = []
        for task in tasks:
            task_id = self.add_task(task)
            task_ids.append(task_id)
        return task_ids
    
    def get_next_task(self) -> Optional[DownloadTask]:
        """获取下一个任务"""
        with self._lock:
            if self._queue:
                return self._queue.pop(0)
            return None
    
    def get_result(self, task_id: str) -> Any:
        """获取任务结果"""
        return self._results.get(task_id)
    
    def _execute_download(self, task: DownloadTask) -> Tuple[bool, Any]:
        """执行下载"""
        for attempt in range(self._max_retries):
            r = None
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                r = requests.get(task.url, headers=headers, timeout=task.timeout, stream=True)
                if r.status_code == 200:
                    os.makedirs(os.path.dirname(task.save_path), exist_ok=True)
                    with open(task.save_path, "wb") as f:
                        for chunk in r.iter_content(8192):
                            f.write(chunk)
                    if os.path.getsize(task.save_path) > 0:
                        if task.callback:
                            task.callback(True, task.save_path)
                        return True, task.save_path
                log(f"  媒体下载失败: status={r.status_code}, 重试({attempt + 1}/{self._max_retries})")
            except Exception as e:
                log(f"  媒体下载异常: {e}, 重试({attempt + 1}/{self._max_retries})")
            finally:
                if r is not None:
                    r.close()
        if task.callback:
            task.callback(False, None)
        return False, None
    
    def process_all(self, wait: bool = True) -> Dict[str, Any]:
        """处理队列中的所有任务"""
        results = {}
        while True:
            task = self.get_next_task()
            if task is None:
                break
            task_id = f"{task.url}_{id(task)}"
            success, result = self._execute_download(task)
            results[task_id] = {"success": success, "result": result, "task": task}
            self._results[task_id] = results[task_id]
        return results
    
    def shutdown(self):
        """关闭下载队列"""
        self._executor.shutdown(wait=wait)


# 全局下载队列实例
download_queue = DownloadQueue(max_workers=3)


# ============ 目录初始化 ============
def init_media_dirs():
    for d in [IMAGE_DIR, AUDIO_RECEIVED_DIR, AUDIO_GENERATED_DIR,
              os.path.join(MEDIA_DIR, "images", "generated")]:
        os.makedirs(d, exist_ok=True)


# ============ 媒体下载与编码 ============
def download_media(url, save_path, timeout=10):
    # === URL安全检查 ===
    from urllib.parse import urlparse
    parsed = urlparse(url)
    # 1. 只允许 http/https
    if parsed.scheme not in ("http", "https"):
        log(f"  媒体下载拒绝(非http/https): {parsed.scheme}")
        return False
    # 2. 禁止内网IP
    hostname = (parsed.hostname or "").lower()
    import ipaddress
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            log(f"  媒体下载拒绝(内网IP): {hostname}")
            return False
    except ValueError:
        pass  # 非IP地址，允许
    # 3. 限制最大20MB
    MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024
    # ===
    r = None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(url, headers=headers, timeout=timeout, stream=True)
        if r.status_code == 200:
            total_size = 0
            with open(save_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
                    total_size += len(chunk)
                    if total_size > MAX_DOWNLOAD_SIZE:
                        f.close()
                        try:
                            os.remove(save_path)
                        except Exception:
                            pass
                        log(f"  媒体下载拒绝(超过20MB): {url[:80]}")
                        return False
            if os.path.getsize(save_path) > 0:
                return True
        log(f"  媒体下载失败: status={r.status_code}")
    except Exception as e:
        log(f"  媒体下载异常: {e}")
    finally:
        if r is not None:
            r.close()
    return False


def image_to_base64(file_path):
    try:
        with open(file_path, "rb") as f:
            img_bytes = f.read()
        b64 = base64.b64encode(img_bytes).decode("ascii")
        ext = os.path.splitext(file_path)[1].lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}.get(ext, "jpeg")
        return f"data:image/{mime};base64,{b64}"
    except Exception as e:
        log(f"  base64编码失败: {e}")
        return None


def audio_to_base64(file_path):
    try:
        with open(file_path, "rb") as f:
            audio_bytes = f.read()
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        return b64
    except Exception as e:
        log(f"  audio base64编码失败: {e}")
        return None


# ============ 图片索引（盗图模式） ============
_image_index = None


def _load_image_index():
    global _image_index
    if _image_index is not None:
        return _image_index
    if os.path.exists(IMAGE_INDEX_FILE):
        try:
            with open(IMAGE_INDEX_FILE, "r", encoding="utf-8") as f:
                _image_index = json.load(f)
        except Exception:
            _image_index = {"images": []}
    else:
        _image_index = {"images": []}
    return _image_index


def _save_image_index():
    try:
        with open(IMAGE_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(_image_index, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"  保存图片索引失败: {e}")


def register_image(file_path, uid):
    idx = _load_image_index()
    rel_path = os.path.relpath(file_path, MEDIA_DIR)
    for img in idx["images"]:
        if img.get("path") == rel_path:
            return
    idx["images"].append({
        "path": rel_path.replace("\\", "/"),
        "from_uid": uid,
        "added": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "used_count": 0,
    })
    _save_image_index()
    log(f"  图片入库: {rel_path}")


def pick_image(uid=None):
    idx = _load_image_index()
    if not idx["images"]:
        return None
    if uid:
        user_imgs = [img for img in idx["images"] if img["from_uid"] == uid]
        if user_imgs and random.random() < 0.7:
            chosen = random.choice(user_imgs)
        else:
            chosen = random.choice(idx["images"])
    else:
        chosen = random.choice(idx["images"])
    chosen["used_count"] = chosen.get("used_count", 0) + 1
    _save_image_index()
    return os.path.join(MEDIA_DIR, chosen["path"])


# ============ 表情包系统 ============
_emoji_index = None


def load_emoji_index():
    global _emoji_index
    if _emoji_index is not None:
        return _emoji_index
    try:
        if os.path.exists(EMOJI_INDEX_FILE):
            with open(EMOJI_INDEX_FILE, "r", encoding="utf-8") as f:
                _emoji_index = json.load(f)
            log(f"表情包索引已加载: {len(_emoji_index)} 个")
        else:
            log("表情包索引文件不存在，请先运行 classify_emojis.py")
            _emoji_index = {}
    except Exception as e:
        log(f"表情包索引加载失败: {e}")
        _emoji_index = {}
    return _emoji_index


def pick_emoji(uid, reply_text="", context_text=""):
    idx = load_emoji_index()
    if not idx:
        return None, None

    # 延迟导入避免循环
    from .status import _status
    mood = _status.get("mood", 50)
    fatigue = _status.get("fatigue", 50)
    hour = datetime.now().hour
    is_late = hour >= 23 or hour < 6

    target_emotion = None
    reply_lower = reply_text.lower() + context_text.lower()

    emotion_map = {
        "happy": ["哈哈", "嘿嘿", "开心", "真好", "太好", "棒", "高兴", "笑"],
        "shy": ["害羞", "不好意思", "别说了", "讨厌", "不要说"],
        "angry": ["生气", "烦", "滚", "无语", "够了", "别烦"],
        "upset": ["难受", "伤心", "不开心", "不好", "唉"],
        "cute": ["撒娇", "喵", "呜呜", "贴贴", "蹭蹭"],
        "love": ["喜欢", "想你了", "爱你", "亲亲", "抱抱"],
        "speechless": ["...", "……", "无语", "栓q", "服了"],
        "tsundere": ["哼", "才不是", "随便", "谁要", "不管你"],
        "sleepy": ["困", "睡", "晚安", "早点休息"],
        "laugh": ["笑死", "绷不住", "草", "哈哈哈"],
        "bye": ["晚安", "拜拜", "再见", "睡了"],
        "eyeroll": ["白眼", "离谱", "麻了"],
    }

    for emotion, keywords in emotion_map.items():
        if any(kw in reply_lower for kw in keywords):
            target_emotion = emotion
            break

    if not target_emotion:
        if is_late and uid == 1592741204:
            target_emotion = random.choice(["sleepy", "love", "shy"])
        elif mood > 75:
            target_emotion = random.choice(["happy", "cute", "laugh"])
        elif mood < 35:
            target_emotion = random.choice(["upset", "speechless", "tired"])
        elif fatigue > 70:
            target_emotion = "tired"
        else:
            target_emotion = random.choice(["happy", "tsundere", "cute", "shy"])

    candidates = []
    for filename, info in idx.items():
        if info.get("emotion") == target_emotion:
            candidates.append((filename, info))

    if not candidates:
        candidates = [(fn, info) for fn, info in idx.items()]

    if not candidates:
        return None, None

    if uid == 1592741204:
        if random.random() < 0.30 and target_emotion not in ("cute", "love", "shy"):
            cute_candidates = [(fn, info) for fn, info in candidates
                             if info.get("emotion") in ("cute", "love", "shy")]
            if cute_candidates:
                candidates = cute_candidates
    else:
        filtered = [(fn, info) for fn, info in candidates
                    if info.get("emotion") not in ("love", "shy")]
        if filtered:
            candidates = filtered

    chosen_name, chosen_info = random.choice(candidates)
    filepath = os.path.join(EMOJI_DIR, chosen_name)
    if os.path.exists(filepath):
        log(f"  选表情包: {chosen_name} [{chosen_info.get('emotion')}]")
        return filepath, chosen_name
    return None, None


def should_send_emoji(uid, reply_text):
    if not reply_text:
        return False
    idx = load_emoji_index()
    if not idx:
        return False

    from .status import _status
    mood = _status.get("mood", 50)

    base_chance = 0.40 if uid == 1592741204 else 0.25
    if mood > 70:
        base_chance += 0.10
    elif mood < 30:
        base_chance += 0.05
    hour = datetime.now().hour
    if hour >= 23 or hour < 6:
        base_chance += 0.10
    if len(reply_text) < 20:
        base_chance += 0.10
    emotion_words = ["哈哈", "嘿嘿", "哼", "唉", "困", "晚安", "嘻嘻", "呜呜", "讨厌", "开心", "烦"]
    if any(w in reply_text for w in emotion_words):
        base_chance += 0.15

    return random.random() < base_chance


# ============ OneBot Media API ============
def get_record_file(file_id):
    try:
        r = requests.post(
            f"{SENDBOT_API}/get_record",
            json={"file": file_id, "out_format": "mp3"},
            timeout=15
        )
        data = r.json()
        if data.get("status") == "ok":
            return data["data"]["file"]
        log(f"  get_record失败: {data}")
    except Exception as e:
        log(f"  get_record异常: {e}")
    return None


def get_image_file(file_id):
    try:
        r = requests.post(
            f"{SENDBOT_API}/get_image",
            json={"file": file_id},
            timeout=15
        )
        data = r.json()
        if data.get("status") == "ok":
            return data["data"]["file"]
    except Exception as e:
        log(f"  get_image异常: {e}")
    return None


# ============ PCM → WAV ============
def _pcm_to_wav(pcm_data, sample_rate=24000, num_channels=1, bits_per_sample=16):
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + len(pcm_data)))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))
    buf.write(struct.pack("<H", 1))
    buf.write(struct.pack("<H", num_channels))
    buf.write(struct.pack("<I", sample_rate))
    buf.write(struct.pack("<I", byte_rate))
    buf.write(struct.pack("<H", block_align))
    buf.write(struct.pack("<H", bits_per_sample))
    buf.write(b"data")
    buf.write(struct.pack("<I", len(pcm_data)))
    buf.write(pcm_data)
    return buf.getvalue()


# ============ LongCat Omni 多模态 ============
def _get_omni_cfg():
    return _get_cfg("vision")


def chat_vision(image_b64, user_text, uid=None):
    """调多模态API做图片识别（通用OpenAI Vision格式）"""
    try:
        cfg = _get_omni_cfg()
        prompt = user_text or "描述这张图片里有什么，用中文简要回答"
        # 通用OpenAI Vision格式
        messages = [
            {"role": "system", "content": "你是图片描述助手。用中文客观描述图片内容，包括物体、颜色、文字、场景等。不要角色扮演。"},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": prompt}
            ]}
        ]
        def _call():
            r = requests.post(
                f"{cfg.api_base}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                json={
                    "model": cfg.model,
                    "messages": messages,
                    "max_tokens": 300,
                    "temperature": 0.9
                },
                timeout=30
            )
            return r.json()
        result = _call()
        if "choices" in result:
            return result["choices"][0]["message"]["content"]
        log(f"  vision错误: {result.get('error', result)}")
    except Exception as e:
        log(f"  vision异常: {e}")
    return None


def audio_to_text(audio_path):
    """调 LongCat Omni 做语音转文字"""
    try:
        cfg = _get_omni_cfg()
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        if audio_bytes[:4] != b"RIFF" and audio_bytes[:3] != b"ID3" and audio_bytes[:2] != b"\xff\xfb":
            audio_bytes = _pcm_to_wav(audio_bytes)
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        content = [
            {"type": "input_audio", "input_audio": {"type": "base64", "data": b64}},
            {"type": "text", "text": "转写这段语音为中文文字，只输出转写结果"}
        ]
        messages = [{"role": "user", "content": content}]
        def _call():
            r = requests.post(
                f"{cfg.api_base}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                json={
                    "model": cfg.model,
                    "messages": messages,
                    "output_modalities": ["text"],
                    "stream": False,
                    "max_tokens": 300,
                    "temperature": 0.3
                },
                timeout=30
            )
            return r.json()
        result = _call()
        if "choices" in result:
            text = result["choices"][0]["message"]["content"].strip()
            return text if text else None
        log(f"  Omni ASR错误: {result.get('error', result)}")
    except Exception as e:
        log(f"  Omni ASR异常: {e}")
    return None


# ============ 声音克隆 ============
_cloned_voice_ref = None
_cloned_voice_name = None
REF_AUDIO_SAMPLE_RATE = 24000


def _read_wav_24k_pcm(audio_path):
    import wave
    try:
        with wave.open(audio_path, "rb") as w:
            ch = w.getnchannels()
            sw = w.getsampwidth()
            sr = w.getframerate()
            nf = w.getnframes()
            raw = w.readframes(nf)
        if ch == 1 and sw == 2 and sr == REF_AUDIO_SAMPLE_RATE:
            dur = nf / sr
            return raw, dur
        else:
            return None, f"格式不匹配(需24kHz/16bit/mono, 当前{sr}Hz/{sw*8}bit/{ch}ch)"
    except wave.Error as we:
        return None, f"WAV读取失败: {we}"
    except Exception as e:
        return None, f"异常: {e}"


def set_cloned_voice(audio_path, voice_name="自定义"):
    global _cloned_voice_ref, _cloned_voice_name
    try:
        if not os.path.exists(audio_path):
            log(f"声音克隆失败: 文件不存在 {audio_path}")
            return False

        raw_pcm = None
        duration_s = 0

        raw_pcm, result = _read_wav_24k_pcm(audio_path)
        if raw_pcm is not None:
            duration_s = result
            log(f"[声音克隆] 从WAV直接读取 PCM | {duration_s:.1f}秒")
        elif not shutil.which("ffmpeg"):
            log(f"声音克隆失败: 未安装ffmpeg, 且 {result}")
            return False
        else:
            log(f"[声音克隆] WAV直读失败({result}), 用ffmpeg转换...")
            proc = subprocess.run(
                ["ffmpeg", "-i", audio_path,
                 "-f", "s16le", "-acodec", "pcm_s16le",
                 "-ar", str(REF_AUDIO_SAMPLE_RATE), "-ac", "1",
                 "-t", "15",
                 "pipe:1"],
                capture_output=True,
                timeout=30
            )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="replace")[-200:]
                log(f"声音克隆 ffmpeg 失败: {err}")
                return False
            raw_pcm = proc.stdout
            if len(raw_pcm) == 0:
                log("声音克隆失败: ffmpeg输出为空")
                return False
            duration_s = len(raw_pcm) / (REF_AUDIO_SAMPLE_RATE * 2)

        if duration_s < 1.0:
            log(f"声音克隆失败: 音频太短 ({duration_s:.1f}秒)")
            return False

        _cloned_voice_ref = base64.b64encode(raw_pcm).decode("utf-8")
        _cloned_voice_name = voice_name
        log(f"声音克隆成功: {voice_name} | {duration_s:.1f}秒 | {len(raw_pcm)}字节PCM")
        return True

    except Exception as e:
        log(f"声音克隆异常: {e}")
        return False


def clear_cloned_voice():
    global _cloned_voice_ref, _cloned_voice_name
    _cloned_voice_ref = None
    _cloned_voice_name = None
    log("已清除克隆声音，恢复默认音色")


def _auto_load_cloned_voice():
    if CLONED_VOICE_PATH and os.path.exists(CLONED_VOICE_PATH):
        ok = set_cloned_voice(CLONED_VOICE_PATH, voice_name="我的冬")
        if ok:
            log("已自动加载克隆声音: 我的冬")
        else:
            log("自动加载克隆声音失败, 使用默认音色")
    elif CLONED_VOICE_PATH:
        log(f"克隆声音文件不存在: {CLONED_VOICE_PATH}")


def has_cloned_voice():
    return _cloned_voice_ref is not None


def text_to_speech(text):
    """调 LongCat Omni 做文字转语音，返回本地wav文件路径"""
    try:
        cfg = _get_omni_cfg()

        # 情绪动态控制 speed + 语气引导
        from .status import _status_manager
        mood = _status_manager._status.mood if _status_manager else 60
        if mood > 75:
            speed = 55
            prefix = "用轻快的语气说："
        elif mood < 35:
            speed = 40
            prefix = "用低沉、没精神的语气说："
        else:
            speed = 50
            prefix = "用自然的语气说："

        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": f"{prefix}{text}"}
            ]}
        ]
        audio_params = {"voice": "linjiajiejie", "speed": speed}
        if _cloned_voice_ref:
            audio_params["reference_audio"] = {
                "type": "base64",
                "data": _cloned_voice_ref
            }
            audio_params["reference_text"] = ""
        def _call():
            r = requests.post(
                f"{cfg.api_base}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                json={
                    "model": cfg.model,
                    "messages": messages,
                    "output_modalities": ["text", "audio"],
                    "stream": False,
                    "audio": audio_params,
                    "max_tokens": 500
                },
                timeout=30
            )
            return r.json()
        result = _call()
        if "choices" in result:
            msg = result["choices"][0]["message"]
            audio = msg.get("audio")
            if audio:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                save_path = os.path.join(AUDIO_GENERATED_DIR, f"tts_{ts}.wav")
                audio_bytes = None
                if isinstance(audio, dict):
                    a_type = audio.get("type", "")
                    a_data = audio.get("data", "")
                    if a_type == "url":
                        download_media(a_data, save_path, timeout=20)
                        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                            with open(save_path, "rb") as f:
                                audio_bytes = f.read()
                    elif a_type == "base64":
                        audio_bytes = base64.b64decode(a_data)
                elif isinstance(audio, str):
                    audio_bytes = base64.b64decode(audio)
                if audio_bytes:
                    if audio_bytes[:4] != b"RIFF":
                        audio_bytes = _pcm_to_wav(audio_bytes, sample_rate=24000)
                    with open(save_path, "wb") as f:
                        f.write(audio_bytes)
                if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                    log(f"  TTS生成: {save_path} ({os.path.getsize(save_path)} bytes)")
                    return save_path
        log(f"  Omni TTS错误: {result.get('error', result)}")
    except Exception as e:
        log(f"  Omni TTS异常: {e}")
    return None
