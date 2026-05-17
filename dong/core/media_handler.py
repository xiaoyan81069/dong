"""
媒体消息处理器 — 图片识别 / 语音转写
从 __init__.py L167-225 提取
"""
import asyncio
import os
import random
import shutil
from datetime import datetime

from ..config import IMAGE_DIR, AUDIO_RECEIVED_DIR
from ..log import log
from ..media import (
    download_media, get_image_file, image_to_base64, chat_vision,
    register_image, audio_to_text, get_record_file,
)
from ..memory import set_visual_memory
from ..interaction import send_msg


async def _process_image_message(img_data, uid):
    """处理图片消息，返回识别后的文本"""
    img_url = img_data.get("url", "")
    img_file = img_data.get("file", "")
    log(f"收到图片: file={img_file} url={img_url[:50]}...")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    img_save = os.path.join(IMAGE_DIR, f"{uid}_{ts}.jpg")

    downloaded = False
    if img_url:
        downloaded = await asyncio.to_thread(download_media, img_url, img_save)
    if not downloaded and img_file:
        local_path = await asyncio.to_thread(get_image_file, img_file)
        if local_path and os.path.exists(local_path):
            shutil.copy(local_path, img_save)
            downloaded = True

    if downloaded:
        register_image(img_save, uid)
        img_b64 = await asyncio.to_thread(image_to_base64, img_save)
        if img_b64:
            caption = await asyncio.to_thread(chat_vision, img_b64, "", uid)
            if caption:
                text = "[图片] " + caption
                log(f"  图片识别: {caption[:30]}")
                set_visual_memory(uid, caption)
                return text, True, img_save

    return "[图片]", False, ""


async def _process_voice_message(rec_data, uid, ws):
    """处理语音消息，返回(文本, 是否媒体)"""
    rec_file = rec_data.get("file", "")
    rec_url = rec_data.get("url", "")
    log(f"收到语音: file={rec_file}")

    local_audio = await asyncio.to_thread(get_record_file, rec_file)
    if not local_audio or not os.path.exists(local_audio):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        local_audio = os.path.join(AUDIO_RECEIVED_DIR, f"{uid}_{ts}.mp3")
        if rec_url:
            await asyncio.to_thread(download_media, rec_url, local_audio)

    if local_audio and os.path.exists(local_audio):
        asr_text = await asyncio.to_thread(audio_to_text, local_audio)
        if asr_text:
            log(f"  语音转写: {asr_text[:30]}")
            return asr_text, True
        else:
            await send_msg(ws, uid, random.choice(["没听清", "你再发一遍听听", "听不清诶"]))
    else:
        await send_msg(ws, uid, random.choice(["听不到", "你再发一遍"]))
    return None
