"""
冬的工具调用系统

路线：Prompt注入 + 结构化标签 [TOOL:name]params[/TOOL]
- 任何API后端都能用，不依赖function calling
- 工具名白名单 + 参数校验 → 三道防线
- 单轮最多1次工具调用
"""
import os
import re
import json
import ast
import sys
import shutil
import subprocess
import time
import ctypes
from ctypes import wintypes
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Callable, Any

# ── 抢焦点：绕过 Windows 前台锁 ──
_user32 = ctypes.WinDLL("user32", use_last_error=True)

def _force_foreground(hwnd: int):
    """AttachThreadInput + SetForegroundWindow 强行抢焦点"""
    fg = _user32.GetForegroundWindow()
    cur_tid = _user32.GetCurrentThreadId()
    fore_tid = _user32.GetWindowThreadProcessId(fg, 0)
    attached = _user32.AttachThreadInput(cur_tid, fore_tid, True)
    try:
        _user32.ShowWindow(hwnd, 9)
        _user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003)
        _user32.SetForegroundWindow(hwnd)
        _user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0003)
        _user32.BringWindowToTop(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(cur_tid, fore_tid, False)

def _find_hwnd_by_title(keyword: str) -> Optional[int]:
    """枚举顶层窗口，按标题关键字找句柄"""
    found = None
    def _enum(hwnd, _):
        nonlocal found
        if found: return False
        length = _user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            if keyword.lower() in buf.value.lower():
                found = hwnd
                return False
        return True
    _user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_enum), 0)
    return found

def _ensure_window_focused(keyword: str, retries: int = 3) -> bool:
    """确保目标窗口在前台"""
    for _ in range(retries):
        fg = _user32.GetForegroundWindow()
        length = _user32.GetWindowTextLengthW(fg)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(fg, buf, length + 1)
            if keyword.lower() in buf.value.lower():
                return True
        hwnd = _find_hwnd_by_title(keyword)
        if hwnd:
            _force_foreground(hwnd)
            time.sleep(0.2)
        else:
            return False
    return False

# ── 窗口操作辅助 ──
def _list_windows() -> list:
    """枚举所有可见窗口，返回 [{title, x, y, w, h, active}]"""
    try:
        import pygetwindow as _gw
        wins = []
        for w in _gw.getAllWindows():
            if w.title.strip() and w.width > 50 and w.height > 50:
                wins.append({"title": w.title, "x": w.left, "y": w.top,
                             "w": w.width, "h": w.height, "active": w.isActive})
        return wins
    except ImportError:
        return []

def _find_window(name: str) -> Optional[dict]:
    """按名称模糊匹配窗口"""
    for w in _list_windows():
        if name.lower() in w["title"].lower():
            return w
    return None

def _launch_app(name: str) -> str:
    """按名称启动应用"""
    app_map = {
        "微信": ["start", "weixin://"],
        "wechat": ["start", "weixin://"],
        "qq": ["start", "qq://"],
        "记事本": ["notepad.exe"],
        "notepad": ["notepad.exe"],
        "计算器": ["calc.exe"],
        "calc": ["calc.exe"],
        "cmd": ["cmd.exe"],
        "终端": ["cmd.exe"],
        "浏览器": ["start", "https://"],
        "文件管理器": ["explorer.exe"],
        "资源管理器": ["explorer.exe"],
    }
    cmd = app_map.get(name.lower())
    if cmd:
        try:
            subprocess.Popen(cmd)  # 白名单固定命令，无shell=True
            time.sleep(1.5)  # 等窗口出现
            # 自动OCR扫描，找常见按钮
            btns = _scan_buttons()
            if btns:
                return f"[成功] 已启动 {name}\n可见按钮: {btns}\n用 click_element 点击目标按钮。"
            return f"[成功] 已启动 {name}"
        except Exception as e:
            return f"[错误] 启动{name}失败: {e}"
    # 拒绝未注册的name — 只允许白名单固定路径
    return f"[错误] 未知应用 '{name}'，不在白名单中。请尝试 screenshot+click 方式"


def _scan_buttons() -> str:
    """OCR快速扫描常见按钮文字"""
    try:
        from .gui_agent import find_text_on_screen
        keywords = ["登录", "确定", "发送", "搜索", "取消", "关闭", "确认", "开始", "下一步", "进入"]
        found = []
        for kw in keywords:
            el = find_text_on_screen(kw)
            if el:
                found.append(f"{kw}@({el.get('x',0)},{el.get('y',0)})")
        return ", ".join(found[:6]) if found else ""
    except Exception:
        return ""

# ── 工具定义 ───────────────────────────────────────────

@dataclass
class ToolDefinition:
    name: str
    description: str          # 简短描述（注入 prompt）
    params_desc: str          # 参数说明
    handler: Callable         # async (params, uid) -> str
    validate: Callable = None # (params) -> Optional[str]（返回错误信息或None）
    requires_master: bool = False  # 是否需要主人权限

    def to_prompt_line(self) -> str:
        return f"- {self.name}: {self.description} 用法：[TOOL:{self.name}]{self.params_desc}[/TOOL]"


# ── 参数解析 ───────────────────────────────────────────

def _parse_params(raw: str) -> Dict[str, str]:
    """解析工具参数：key=value,key2=value2"""
    params = {}
    if not raw.strip():
        return params
    # 按逗号分割（注意value中可能含逗号）
    parts = re.split(r",(?=\s*\w+=)", raw)
    for part in parts:
        m = re.match(r"(\w+)\s*=\s*(.+)", part.strip())
        if m:
            params[m.group(1)] = m.group(2).strip()
    return params


def parse_tool_call(text: str) -> Optional[Tuple[str, Dict[str, str]]]:
    """从LLM输出中提取工具调用
    格式：[TOOL:工具名]key=value,...[/TOOL]
    返回 (tool_name, params_dict) 或 None
    """
    m = re.search(r"\[TOOL:(\w+)\](.*?)\[/TOOL\]", text, re.DOTALL)
    if not m:
        return None
    name = m.group(1).strip()
    raw_params = m.group(2).strip()
    params = _parse_params(raw_params)
    return name, params


# ── 工具实现 ───────────────────────────────────────────

# --- 1. search_memory ---

def _tool_search_memory(params: Dict[str, str], uid: int) -> str:
    """搜索冬的记忆库"""
    query = params.get("query", params.get("q", ""))
    if not query:
        return "[错误] 请提供查询关键词，如 query=上次提到的猫"

    try:
        from .memory import retrieve_relevant_memories
        result = retrieve_relevant_memories(uid, query, top_k=3)
        if result and result.strip():
            return result[:300]
        return "记忆中没有找到相关内容。"
    except Exception as e:
        return f"[错误] 记忆搜索失败: {e}"


# --- 2. check_schedule ---

_SCHEDULE_FILE = os.path.join(os.path.dirname(__file__), "dong_today_schedule.json")

def _tool_check_schedule(params: Dict[str, str], uid: int) -> str:
    """查看冬的今日日程"""
    try:
        if os.path.exists(_SCHEDULE_FILE):
            with open(_SCHEDULE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            return "今天还没有安排日程。"

        lines = ["今日日程："]
        activities = data.get("activities", [])
        if not activities:
            return "今天还没有安排日程。"

        now_hour = datetime.now().hour
        for a in activities:
            time_str = a.get("time", "?")
            desc = a.get("description", a.get("name", "?"))
            done = "✓" if a.get("done") else ""
            marker = " ← 现在" if a.get("hour", -1) == now_hour else ""
            lines.append(f"  {time_str} {desc} {done}{marker}")

        return "\n".join(lines)[:300]
    except Exception as e:
        return f"[错误] 日程读取失败: {e}"


# --- 3. web_search ---

def _tool_web_search(params: Dict[str, str], uid: int) -> str:
    """搜索互联网（DuckDuckGo Lite）"""
    import requests

    query = params.get("query", params.get("q", ""))
    if not query:
        return "[错误] 请提供搜索关键词，如 query=最近新闻"

    try:
        r = requests.get(
            "https://lite.duckduckgo.com/lite/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        if r.status_code != 200:
            return f"[错误] 搜索请求失败: {r.status_code}"

        # 提取搜索结果摘要
        from html.parser import HTMLParser

        class ResultParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.results = []
                self.current = {}
                self.in_link = False
                self.in_desc = False
                self.link_text = ""

            def handle_starttag(self, tag, attrs):
                attrs_dict = dict(attrs)
                cls = attrs_dict.get("class", "")
                if tag == "a" and "result-link" in cls:
                    self.in_link = True
                    self.link_text = ""
                elif tag == "td" and "result-snippet" in cls:
                    self.in_desc = True
                    self.current = {"desc": ""}

            def handle_endtag(self, tag):
                if tag == "a" and self.in_link:
                    self.in_link = False
                    self.current["title"] = self.link_text.strip()
                elif tag == "td" and self.in_desc:
                    self.in_desc = False
                    if self.current.get("title") and self.current.get("desc"):
                        self.results.append(self.current)
                    self.current = {}

            def handle_data(self, data):
                if self.in_link:
                    self.link_text += data
                elif self.in_desc:
                    self.current["desc"] += data

        parser = ResultParser()
        parser.feed(r.text)

        if not parser.results:
            return "没有找到相关搜索结果。"

        lines = [f"搜索「{query}」的结果："]
        for i, res in enumerate(parser.results[:3]):
            title = res.get("title", "?")[:60]
            desc = res.get("desc", "")[:80]
            lines.append(f"{i+1}. {title}\n   {desc}")

        return "\n".join(lines)[:400]
    except requests.Timeout:
        return "[错误] 搜索超时，稍后再试。"
    except Exception as e:
        return f"[错误] 搜索失败: {e}"


# --- 4. modify_self ---

def _tool_modify_self(params: Dict[str, str], uid: int) -> str:
    """修改冬自己的代码（封装optimizer的编辑+验证流程）"""
    from .config import MASTER_UID

    if uid != MASTER_UID:
        return "[拒绝] 只有主人才可以让我修改代码。"

    file_name = params.get("file", "")
    issue = params.get("issue", params.get("description", ""))

    if not file_name:
        return "[错误] 请指定要修改的文件，如 file=persona.py"
    if not issue:
        return "[错误] 请描述要修改的问题，如 issue=傲娇概率太低"

    # 白名单检查
    from .optimizer import _is_edit_allowed, _check_syntax, _parse_edits_response

    base_dir = os.path.dirname(__file__)
    full_path = os.path.join(base_dir, file_name)

    if not os.path.exists(full_path):
        return f"[错误] 文件不存在: {file_name}"

    if not _is_edit_allowed(file_name):
        from .optimizer import EDIT_WHITELIST_FILES
        return f"[拒绝] {file_name} 不在可编辑白名单中。允许的文件: {', '.join(sorted(EDIT_WHITELIST_FILES))}"

    try:
        # 1. 备份（文件名+时间戳）
        backup_dir = os.path.join(base_dir, "dong_backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"{file_name}.tool_backup_{ts}")
        shutil.copy2(full_path, backup_path)

        # 2. 读取源码
        with open(full_path, "r", encoding="utf-8") as f:
            source_code = f.read()

        # 3. 调用修改API（复用optimizer的提示词和API逻辑）
        from .optimizer import _run_modify_api, _MODIFY_SAFETY_RULES
        import asyncio

        suggestion = {"description": issue, "goal": "工具调用修改", "severity": "中调"}

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在事件循环中，创建新任务
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(
                        lambda: asyncio.run(_run_modify_api(source_code, file_name, [suggestion]))
                    )
                    edits = future.result(timeout=60)
            else:
                edits = asyncio.run(_run_modify_api(source_code, file_name, [suggestion]))
        except RuntimeError:
            edits = asyncio.run(_run_modify_api(source_code, file_name, [suggestion]))

        if not edits:
            # 回滚备份
            os.remove(backup_path)
            return "[跳过] AI分析后认为不需要修改，或无法生成合适的编辑方案。"

        # 4. 应用编辑
        content = source_code
        applied = 0
        for edit in edits:
            old_text = edit.get("old", "")
            new_text = edit.get("new", "")
            reason = edit.get("reason", "")

            if not old_text:
                continue
            if content.count(old_text) != 1:
                continue

            content = content.replace(old_text, new_text, 1)
            applied += 1

        if applied == 0:
            os.remove(backup_path)
            return "[跳过] 无法应用任何编辑（匹配失败或重复）。"

        # 5. AST验证
        ok, err = _check_syntax(content)
        if not ok:
            os.remove(backup_path)
            return f"[回滚] AST语法验证失败: {err}。备份已删除，原文件未修改。"

        # 6. 写入修改
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        # 7. 记录更新日志
        try:
            from .update import log_update
            log_update(f"工具调用修改 {file_name}: {issue[:50]}", update_type="tool_modify")
        except Exception:
            pass

        return f"[成功] 已修改 {file_name}：{applied}处编辑。\n原因：{issue[:100]}\n备份：{backup_path}"

    except Exception as e:
        # 尝试回滚
        try:
            if os.path.exists(backup_path):
                shutil.copy2(backup_path, full_path)
        except Exception:
            pass
        return f"[错误] 修改失败: {e}"


# --- 5. adb_tap ---

def _tool_adb_tap(params: Dict[str, str], uid: int) -> str:
    """ADB模拟屏幕点击"""
    from .config import MASTER_UID

    if uid != MASTER_UID:
        return "[拒绝] 只有主人才可以使用屏幕操作。"

    action = params.get("action", "tap")

    if action == "tap":
        x = params.get("x", "")
        y = params.get("y", "")
        if not x or not y:
            return "[错误] 请提供点击坐标，如 action=tap,x=500,y=1200"

        try:
            xi, yi = int(x), int(y)
        except ValueError:
            return "[错误] 坐标必须是整数"

        try:
            result = subprocess.run(
                ["adb", "shell", "input", "tap", str(xi), str(yi)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return f"[成功] 已点击屏幕坐标 ({xi}, {yi})"
            else:
                return f"[错误] ADB点击失败: {result.stderr.strip()}"
        except FileNotFoundError:
            return "[错误] 未找到ADB工具，请确认已安装Android Debug Bridge。"
        except subprocess.TimeoutExpired:
            return "[错误] ADB命令超时。"
        except Exception as e:
            return f"[错误] ADB执行失败: {e}"

    elif action == "swipe":
        x1 = params.get("x1", "")
        y1 = params.get("y1", "")
        x2 = params.get("x2", "")
        y2 = params.get("y2", "")
        if not all([x1, y1, x2, y2]):
            return "[错误] 滑动需要提供 x1,y1,x2,y2"

        try:
            result = subprocess.run(
                ["adb", "shell", "input", "swipe", x1, y1, x2, y2],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return f"[成功] 已滑动 ({x1},{y1}) → ({x2},{y2})"
            else:
                return f"[错误] ADB滑动失败: {result.stderr.strip()}"
        except Exception as e:
            return f"[错误] ADB执行失败: {e}"

    else:
        return f"[错误] 不支持的操作: {action}，支持 action=tap 或 action=swipe"


# --- 6. computer_control ---

def _tool_computer_control(params: Dict[str, str], uid: int) -> str:
    """控制电脑桌面：鼠标点击/移动/滚动、键盘输入、截图"""
    from .config import MASTER_UID

    if uid != MASTER_UID:
        return "[拒绝] 只有主人才可以控制电脑。"

    action = params.get("action", "")

    # 屏幕占用检测：screenshot/launch允许（不干扰用户），其他操作需用户空闲
    if action not in ("screenshot", "launch", "activate_window", "analyze"):
        try:
            from .screen_guard import is_desktop_occupied
            if is_desktop_occupied():
                return "[阻止] 主人正在操作电脑，暂不执行桌面操作。请先截图查看桌面，稍后再操作。"
        except Exception:
            pass

    if action == "click":
        x = params.get("x", "")
        y = params.get("y", "")
        if not x or not y:
            return "[错误] 请提供点击坐标，如 action=click,x=500,y=300"
        try:
            xi, yi = int(x), int(y)
        except ValueError:
            return "[错误] 坐标必须是整数"

        try:
            import pyautogui
            pyautogui.click(xi, yi)
            return f"[成功] 已点击桌面坐标 ({xi}, {yi})"
        except ImportError:
            # 回退：用ctypes模拟
            return _click_fallback(xi, yi)

    elif action == "move":
        x = params.get("x", "")
        y = params.get("y", "")
        if not x or not y:
            return "[错误] 请提供移动坐标，如 action=move,x=500,y=300"
        try:
            xi, yi = int(x), int(y)
        except ValueError:
            return "[错误] 坐标必须是整数"

        try:
            import pyautogui
            pyautogui.moveTo(xi, yi, duration=0.3)
            return f"[成功] 已移动鼠标到 ({xi}, {yi})"
        except ImportError:
            return _move_fallback(xi, yi)

    elif action == "type":
        text = params.get("text", "")
        if not text:
            return "[错误] 请提供输入文本，如 action=type,text=你好"
        # 抢焦点
        target_win = params.get("window", "")
        if target_win:
            if not _ensure_window_focused(target_win):
                return f"[警告] 未找到窗口'{target_win}'，继续尝试输入..."
        # 剪贴板粘贴，绕过输入法
        try:
            import pyautogui
            import tempfile, os as _os2
            tmp = tempfile.NamedTemporaryFile(mode='wb', suffix='.txt', delete=False)
            tmp.write(text.encode('gbk', errors='replace'))
            tmp.close()
            # 安全剪贴板写入：用文件重定向代替shell管道
            with open(tmp.name, 'r', encoding='gbk', errors='replace') as _tf:
                subprocess.run(["clip"], stdin=_tf, timeout=5)
            _os2.unlink(tmp.name)
            time.sleep(0.2)
            pyautogui.hotkey("ctrl", "v")
            return f"[成功] 已输入: {text[:30]}"
        except ImportError:
            return "[错误] 输入需要 pyautogui 库。"

    elif action == "press":
        key = params.get("key", "")
        if not key:
            return "[错误] 请提供按键名，如 action=press,key=enter"
        try:
            import pyautogui
            pyautogui.press(key)
            return f"[成功] 已按下按键: {key}"
        except ImportError:
            return "[错误] 键盘操作需要 pyautogui 库。"

    elif action == "screenshot":
        try:
            import pyautogui
            from PIL import Image as _PILImage
            screenshot_dir = os.path.join(os.path.dirname(__file__), "dong_screenshots")
            os.makedirs(screenshot_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(screenshot_dir, f"screenshot_{ts}.png")
            pyautogui.screenshot(path)

            # 视觉分析：如果有target参数，用多模态模型定位
            target = params.get("target", params.get("find", ""))
            if target:
                try:
                    from .config import _get_cfg as _gcfg
                    import requests as _req
                    import base64 as _b64
                    import io as _io

                    with open(path, "rb") as _f:
                        img_b64 = _b64.b64encode(_f.read()).decode()

                    vcfg = _gcfg("vision")
                    # 缩小图片节省token
                    _img = _PILImage.open(path)
                    _w, _h = _img.size
                    _img = _img.resize((_w // 2, _h // 2), _PILImage.LANCZOS)
                    _buf = _io.BytesIO()
                    _img.save(_buf, format="JPEG", quality=60)
                    _img_b64_small = _b64.b64encode(_buf.getvalue()).decode()

                    _r = _req.post(
                        f"{vcfg.api_base}/chat/completions",
                        headers={"Authorization": f"Bearer {vcfg.api_key}", "Content-Type": "application/json"},
                        json={
                            "model": vcfg.model,
                            "max_tokens": 80,
                            "messages": [{
                                "role": "user",
                                "content": [
                                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_img_b64_small}"}},
                                    {"type": "text", "text": f"桌面截图分辨率{pyautogui.size().width}x{pyautogui.size().height}。找到\"{target}\"，给出像素坐标(x,y)。任务栏高度约40px在底部。格式：x=数字,y=数字。只回坐标和位置描述。"}
                                ]
                            }]
                        },
                        timeout=25
                    )
                    if _r.status_code == 200:
                        _d = _r.json()
                        _analysis = _d["choices"][0]["message"]["content"]
                        return f"[成功] 截图: {path}\n视觉分析: {_analysis}"
                    else:
                        return f"[成功] 截图已保存: {path}\n视觉分析失败: {_r.status_code}"
                except Exception as _e:
                    return f"[成功] 截图已保存: {path}\n视觉分析异常: {_e}"
            return f"[成功] 截图已保存: {path}"
        except ImportError:
            return "[错误] 截图需要 pyautogui 库。"
        except Exception as e:
            return f"[错误] 截图失败: {e}"

    elif action == "scroll":
        amount = params.get("amount", "0")
        try:
            ai = int(amount)
        except ValueError:
            return "[错误] scroll的amount必须是整数（正=上滚，负=下滚）"
        try:
            import pyautogui
            pyautogui.scroll(ai)
            direction = "上" if ai > 0 else "下"
            return f"[成功] 滚轮向{direction}滚动 {abs(ai)} 格"
        except ImportError:
            return "[错误] 滚轮操作需要 pyautogui 库。"

    elif action == "analyze":
        try:
            from .gui_agent import analyze_screen
            return analyze_screen()
        except Exception as e:
            return f"[错误] 屏幕分析失败: {e}"

    elif action == "click_element":
        name = params.get("name", params.get("text", ""))
        etype = params.get("type", "")
        if not name:
            return "[错误] 请提供元素名，如 action=click_element,name=发送"
        try:
            from .gui_agent import click_element as _ce
            return _ce(name, etype)
        except Exception as e:
            return f"[错误] 点击元素失败: {e}"

    elif action == "launch":
        app = params.get("app", params.get("name", ""))
        if not app:
            return "[错误] 请提供应用名，如 action=launch,app=微信"
        return _launch_app(app)

    elif action == "activate_window":
        name = params.get("name", params.get("title", ""))
        if not name:
            wins = _list_windows()
            active = [w for w in wins if w.get("active")]
            if active:
                return f"当前活跃窗口: {active[0]['title']}"
            lines = ["当前窗口列表:"]
            for w in wins[:10]:
                lines.append(f"  - {w['title'][:60]} ({w['x']},{w['y']})")
            return "\n".join(lines)
        w = _find_window(name)
        if w:
            try:
                import pygetwindow as _gw
                win = _gw.getWindowsWithTitle(w["title"])[0]
                win.activate()
                return f"[成功] 已激活窗口: {w['title']}"
            except Exception as e:
                return f"[错误] 激活失败: {e}"
        return f"[未找到] 没有包含'{name}'的窗口"

    elif action == "hotkey":
        keys = params.get("keys", "")
        if not keys:
            return "[错误] 请提供快捷键，如 action=hotkey,keys=ctrl+f"
        target_win = params.get("window", "")
        if target_win:
            _ensure_window_focused(target_win)
        try:
            import pyautogui
            pyautogui.hotkey(*keys.split("+"))
            return f"[成功] 已发送快捷键: {keys}"
        except Exception as e:
            return f"[错误] 快捷键失败: {e}"

    else:
        return f"[错误] 不支持的操作: {action}。支持: click, move, type, press, screenshot, scroll, launch, activate_window, hotkey"


def _click_fallback(x: int, y: int) -> str:
    """ctypes回退：SendInput模拟鼠标点击"""
    import ctypes
    from ctypes import wintypes

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    try:
        ctypes.windll.user32.SetCursorPos(x, y)
        time.sleep(0.05)
        # 鼠标左键 down + up
        INPUT_MOUSE = 0
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]

        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        time.sleep(0.05)
        inp.union.mi.dwFlags = MOUSEEVENTF_LEFTUP
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        return f"[成功] 已点击桌面坐标 ({x}, {y}) [ctypes回退]"
    except Exception as e:
        return f"[错误] 点击失败（无pyautogui且ctypes回退也失败）: {e}"


def _move_fallback(x: int, y: int) -> str:
    """ctypes回退：移动鼠标"""
    try:
        import ctypes
        ctypes.windll.user32.SetCursorPos(x, y)
        return f"[成功] 已移动鼠标到 ({x}, {y}) [ctypes回退]"
    except Exception as e:
        return f"[错误] 移动失败（无pyautogui）: {e}"


# ── 工具注册表 ─────────────────────────────────────────

TOOL_REGISTRY: Dict[str, ToolDefinition] = {
    "search_memory": ToolDefinition(
        name="search_memory",
        description="搜索冬的记忆库，查找过去的聊天内容",
        params_desc="query=搜索关键词",
        handler=_tool_search_memory,
        requires_master=True,
    ),
    "check_schedule": ToolDefinition(
        name="check_schedule",
        description="查看冬今天的日程安排",
        params_desc="无需参数",
        handler=_tool_check_schedule,
        requires_master=True,
    ),
    "web_search": ToolDefinition(
        name="web_search",
        description="搜索互联网，获取最新信息",
        params_desc="query=搜索关键词",
        handler=_tool_web_search,
        requires_master=True,
    ),
    "modify_self": ToolDefinition(
        name="modify_self",
        description="修改冬自己的代码文件。调试模式进入/退出文字在 command_channel.py（_DEBUG_MODE_ENTER_MSG）。回复风格在 persona.py。工具定义在 tools.py",
        params_desc="file=文件名,issue=问题描述",
        handler=_tool_modify_self,
        requires_master=True,
    ),
    "adb_tap": ToolDefinition(
        name="adb_tap",
        description="操作手机屏幕：点击坐标或滑动屏幕",
        params_desc="action=tap,x=横坐标,y=纵坐标 或 action=swipe,x1=,y1=,x2=,y2=",
        handler=_tool_adb_tap,
        requires_master=True,
    ),
    "computer_control": ToolDefinition(
        name="computer_control",
        description="控制电脑。analyze=分析屏幕(列出所有按钮/输入框)。click_element=按文字点击按钮。launch=启动软件。type=打字。hotkey=快捷键",
        params_desc="action=analyze(分析屏幕)/click_element(name=按钮文字)/launch(app=应用名)/activate_window(name=窗口)/type(text=文本)/hotkey(keys=快捷键)/screenshot/click(x,y)",
        handler=_tool_computer_control,
        requires_master=True,
    ),
}


# ── 工具描述生成（注入system prompt）────────────────

def build_tools_prompt() -> str:
    """生成工具描述文本，注入 system prompt"""
    lines = ["【可用工具】你可以使用以下工具来更好地回复："]
    for tool in TOOL_REGISTRY.values():
        lines.append(tool.to_prompt_line())
    lines.append("")
    lines.append("使用规则：")
    lines.append("1. 当需要查信息时，在回复中插入 [TOOL:工具名]参数[/TOOL]")
    lines.append("2. 工具结果会自动反馈给你，然后你继续回复")
    lines.append("3. 每次回复最多调用1个工具，但系统会自动执行多轮直到任务完成")
    lines.append("4. 不要编造你不知道的信息——用工具查")
    lines.append("5. 通用操作流程：analyze看屏幕 → click_element点按钮 → type输入文字 → hotkey快捷键")
    lines.append("   例：打开软件 → launch微信 → activate_window微信 → analyze → click_element搜索 → type内容 → hotkey回车")
    return "\n".join(lines)


# ── 工具执行 ──────────────────────────────────────────

def execute_tool(name: str, params: Dict[str, str], uid: int) -> str:
    """执行工具调用，返回结果文本（纯同步）"""
    tool = TOOL_REGISTRY.get(name)
    if not tool:
        return f"[错误] 未知工具: {name}。可用工具: {', '.join(TOOL_REGISTRY.keys())}"

    if tool.requires_master:
        from .config import MASTER_UID
        if uid != MASTER_UID:
            return f"[拒绝] 工具 '{name}' 只有主人才可以使用。"

    try:
        result = tool.handler(params, uid)
        return result
    except Exception as e:
        return f"[错误] 工具 '{name}' 执行异常: {e}"


# ── 门控：是否提供工具 ────────────────────────────────

def should_offer_tools(uid: int, user_text: str = "") -> bool:
    """判断当前对话是否提供工具（仅主号可用）"""
    from .config import MASTER_UID
    return uid == MASTER_UID


# ── 从LLM回复中剥离工具标签，返回干净回复 ──────────────

def strip_tool_tag(text: str) -> str:
    """去除回复中的 [TOOL:xxx]...[/TOOL] 标签"""
    # 只移除工具标签本身，不误吃标签周围的正常文字空格
    result = re.sub(r"\[TOOL:\w+\].*?\[/TOOL\]", "", text, flags=re.DOTALL)
    # 合并多余空白（标签移除后可能留有连续空格）
    result = re.sub(r" {2,}", " ", result)
    return result.strip()
