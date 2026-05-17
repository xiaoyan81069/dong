"""
冬 · 日志模块
"""
import logging

LOG_FILE = os.path.join(os.path.dirname(__file__), "dong_master.log")

_logger = logging.getLogger("dong")

# 确保至少有一条日志能输出（setup_logging 调用前）
_initialized = False


def _ensure_handler():
    global _initialized
    if not _initialized and not _logger.handlers:
        import sys
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                         datefmt="%H:%M:%S"))
        _logger.addHandler(h)
        _logger.setLevel(logging.INFO)
    _initialized = True


def log(msg):
    _ensure_handler()
    _logger.info(msg)
    # 兼容：同时写入历史日志文件
    try:
        from datetime import datetime
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n"
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass
