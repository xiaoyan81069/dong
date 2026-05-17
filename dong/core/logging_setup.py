"""
冬 · 结构化日志 — 终端人读 + 文件JSON + 自动轮转
"""
import json, logging, os, sys

__all__ = ["setup_logging"]

from logging.handlers import RotatingFileHandler
from pathlib import Path

class _JsonFormatter(logging.Formatter):
    def format(self, record):
        obj = {"ts": self.formatTime(record, self.datefmt),
               "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        if record.exc_info and record.exc_info[0]:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False)

class _TerminalFormatter(logging.Formatter):
    COLORS = {"DEBUG":"\033[36m","INFO":"\033[32m","WARNING":"\033[33m","ERROR":"\033[31m","CRITICAL":"\033[1;31m"}
    RESET = "\033[0m"
    def format(self, record):
        c = self.COLORS.get(record.levelname, "")
        ts = self.formatTime(record, self.datefmt)
        return f"{c}{ts} {record.levelname:<7}{self.RESET} [{record.name}] {record.getMessage()}"

def setup_logging(log_file="", level=logging.INFO, max_bytes=50*1024*1024, backup_count=10):
    root = logging.getLogger("dong")
    root.setLevel(level)
    root.handlers.clear()
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    term = logging.StreamHandler(sys.stdout)
    term.setLevel(level); term.setFormatter(_TerminalFormatter(datefmt="%H:%M:%S"))
    root.addHandler(term)
    if not log_file:
        log_file = str(Path(__file__).resolve().parent.parent / "dong.log")
    try:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        fh.setLevel(level); fh.setFormatter(_JsonFormatter(datefmt="%Y-%m-%dT%H:%M:%S"))
        root.addHandler(fh)
    except Exception:
        root.warning("文件日志初始化失败: %s", log_file)
    for noisy in ("urllib3","requests","websockets","httpcore","httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
