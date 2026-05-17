"""语音合成 — 抄自妹居 tts.py edge-tts 方案，免费免Key"""
import asyncio
import os
import tempfile
import threading

import edge_tts
import pygame

from config import TTS_VOICE

_tts_lock = threading.Lock()
_init_done = False


def _init_pygame_mixer():
    """pygame mixer 只需初始化一次"""
    global _init_done
    if not _init_done:
        try:
            pygame.mixer.init(frequency=24000, size=-16, channels=2)
            _init_done = True
        except Exception:
            pass


async def _synthesize(text: str, output_path: str):
    """edge-tts 合成 mp3"""
    communicate = edge_tts.Communicate(text, TTS_VOICE)
    await communicate.save(output_path)


def _play_audio_blocking(file_path: str):
    """阻塞式播放 mp3"""
    _init_pygame_mixer()
    try:
        pygame.mixer.music.load(file_path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)
    except Exception as e:
        print(f"[TTS] 播放失败: {e}")


def play_tts(text: str):
    """异步播放语音（子线程，不阻塞UI）"""
    text = text.strip()
    if not text:
        return

    def _run():
        with _tts_lock:
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
                os.close(fd)
                asyncio.run(_synthesize(text, tmp_path))
                print(f"[TTS] 合成完成: {text[:20]}... → {os.path.getsize(tmp_path)} bytes")
                _play_audio_blocking(tmp_path)
            except Exception as e:
                print(f"[TTS] 异常: {e}")
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

    threading.Thread(target=_run, daemon=True).start()
