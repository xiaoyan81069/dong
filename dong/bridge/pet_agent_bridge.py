"""
Pet/Agent 融合桥接 — HTTP API 暴露 Agent 能力给桌宠
不改 Pet 的 PyQt5 框架，通过 HTTP 桥接通信。

端点:
  GET  /pet/status          → Agent状态 + 当前会话信息
  POST /pet/agent/chat      → 发送消息给Agent，返回回复
  GET  /pet/agent/events    → 获取待推送事件（告警/建议）

桌宠端调用:
  - 右键菜单"让冬执行" → POST /pet/agent/chat {"text": "/d fix ..."}
  - 聊天气泡输入自动补 /d → 前端逻辑
  - 陪伴模式截屏分析 → 检测报错触发 /d fix
  - 检测到主人卡窗口 → 气泡建议

运行: python -m dong.bridge.pet_agent_bridge
默认端口: 18720
"""
import json, os, time, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, Optional
from datetime import datetime

from ..log import log

PORT = int(os.environ.get("PET_AGENT_PORT", "18720"))
_EVENTS: list = []  # 待推送事件队列
_MAX_EVENTS = 100

# 事件类型常量
EVENT_SUGGESTION = "suggestion"   # 建议（要不要帮你看看）
EVENT_ALERT = "alert"             # 告警（编译错误）
EVENT_FIX_DONE = "fix_done"       # 修复完成
EVENT_REPORT = "report"           # 日报/周报


def push_event(event_type: str, message: str, priority: str = "low"):
    """向桌宠推送事件"""
    _EVENTS.append({
        "type": event_type,
        "message": message,
        "priority": priority,
        "time": time.time(),
    })
    if len(_EVENTS) > _MAX_EVENTS:
        _EVENTS[:] = _EVENTS[-_MAX_EVENTS:]


def pop_events(since: float = 0) -> list:
    """取出待推送事件"""
    global _EVENTS
    if since > 0:
        result = [e for e in _EVENTS if e["time"] > since]
    else:
        result = list(_EVENTS)
    _EVENTS = [e for e in _EVENTS if e not in result]
    return result


def get_agent_status() -> Dict[str, Any]:
    """获取Agent当前状态"""
    try:
        from ..agent import _SESSION_MESSAGES, _TASK_STATE, _SUGGEST_ENABLED
        sessions = {str(k): len(v) for k, v in _SESSION_MESSAGES.items()}
        return {
            "ok": True,
            "sessions": sessions,
            "task": _TASK_STATE.get("description", "") if _TASK_STATE else "",
            "task_status": _TASK_STATE.get("status", "idle") if _TASK_STATE else "idle",
            "suggest_enabled": _SUGGEST_ENABLED,
            "events_pending": len(_EVENTS),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def agent_chat(text: str, uid: int = 0) -> Dict[str, Any]:
    """发送消息给Agent"""
    try:
        from ..agent import _handle_direct_agent_command
        result = _handle_direct_agent_command(text, uid)
        return {"ok": True, "reply": result or "(已处理)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def agent_chat_async(text: str, uid: int = 0):
    """异步发送消息给Agent（完整agent流程）"""
    try:
        import asyncio as _asyncio
        from ..agent import process_agent_command, _get_agent_config
        api_key, _, _ = _get_agent_config()
        if not api_key:
            return {"ok": False, "error": "API Key未配置"}
        result = _asyncio.run(process_agent_command(uid, text))
        return {"ok": True, "reply": result[:2000]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class PetAgentHandler(BaseHTTPRequestHandler):
    """HTTP请求处理器"""

    def _json_response(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        if path == "/pet/status":
            return self._json_response(get_agent_status())

        if path == "/pet/agent/events":
            since = float(self.path.split("since=")[-1].split("&")[0]) if "since=" in self.path else 0
            events = pop_events(since)
            return self._json_response({"ok": True, "events": events})

        self._json_response({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}

        if path == "/pet/agent/chat":
            text = data.get("text", "")
            uid = data.get("uid", 0)
            async_mode = data.get("async", False)
            if async_mode:
                result = agent_chat_async(text, uid)
            else:
                result = agent_chat(text, uid)
            return self._json_response(result)

        self._json_response({"ok": False, "error": "not found"}, 404)

    def log_message(self, format, *args):
        """静默HTTP日志"""
        pass


def start_server():
    """启动HTTP桥接服务（后台线程）"""
    server = HTTPServer(("127.0.0.1", PORT), PetAgentHandler)
    log(f"[pet-bridge] HTTP桥接启动: http://127.0.0.1:{PORT}")
    print(f"[Pet/Agent桥接] http://127.0.0.1:{PORT}/pet/status")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def start_background():
    """后台启动桥接服务"""
    t = threading.Thread(target=start_server, daemon=True, name="pet-agent-bridge")
    t.start()
    return t


if __name__ == "__main__":
    start_server()
