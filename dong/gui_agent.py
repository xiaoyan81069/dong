"""
GUI Agent — 屏幕分析 + 元素定位 + 虚拟操作 + SoM视觉标注
三层定位：UIA(原生控件) → OCR(屏幕文字) → Vision(视觉描述)
"""
import os
import time
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from PIL import Image, ImageDraw, ImageFont

_WIN_OCR_AVAILABLE = None  # None=未检测, True=可用, False=不可用

def _ocr_via_windows(img_path: str = None, pil_img = None) -> list:
    """Windows内置OCR：返回 [(text, cx, cy, confidence), ...]"""
    global _WIN_OCR_AVAILABLE
    if _WIN_OCR_AVAILABLE is False:
        return []
    try:
        import winrt.windows.media.ocr as wocr
        import winrt.windows.graphics.imaging as wimg
        import winrt.windows.storage.streams as wstreams
        from PIL import Image as _PILImage
        import io, os, pyautogui

        # 获取图像
        if pil_img is None:
            if img_path and os.path.exists(img_path):
                pil_img = _PILImage.open(img_path)
            else:
                pil_img = pyautogui.screenshot()

        # 转PNG字节流
        buf = io.BytesIO()
        pil_img.save(buf, format='PNG')
        buf.seek(0)
        stream = wstreams.InMemoryRandomAccessStream()
        writer = wstreams.DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(buf.read())
        writer.store_async().get()
        writer.close()

        # OCR识别
        decoder = wimg.BitmapDecoder.create_async(stream).get()
        bitmap = decoder.get_software_bitmap_async().get()
        engine = wocr.OcrEngine.try_create_from_user_profile_languages()
        if not engine:
            _WIN_OCR_AVAILABLE = False
            return []
        result = engine.recognize_async(bitmap).get()

        results = []
        w, h = pil_img.size
        for line in result.lines:
            for word in line.words:
                rect = word.bounding_rect
                cx = rect.x + rect.width // 2
                cy = rect.y + rect.height // 2
                results.append((word.text, cx, cy, 0.9))
        _WIN_OCR_AVAILABLE = True
        return results
    except ImportError:
        _WIN_OCR_AVAILABLE = False
        return []
    except Exception:
        _WIN_OCR_AVAILABLE = False
        return []

def _get_ocr():
    """检查OCR是否可用。Windows OCR不需要加载模型。"""
    global _WIN_OCR_AVAILABLE
    if _WIN_OCR_AVAILABLE is None:
        # 首次检测
        try:
            import winrt.windows.media.ocr as wocr
            engine = wocr.OcrEngine.try_create_from_user_profile_languages()
            _WIN_OCR_AVAILABLE = engine is not None
        except Exception:
            _WIN_OCR_AVAILABLE = False
    return _WIN_OCR_AVAILABLE  # 返回True/False，非None


# ── 屏幕分析 ──
def analyze_screen() -> str:
    """分析当前屏幕，返回结构化描述（UIA + OCR + Vision）"""
    lines = []

    # 1. UIA
    lines.append(_analyze_uia())

    # 2. OCR 文字扫描
    lines.append(_analyze_ocr())

    # 3. 视觉描述
    desc = _capture_and_describe()
    if desc:
        lines.append(f"【视觉描述】{desc}")

    return "\n".join(lines)


def _analyze_uia() -> str:
    lines = []
    try:
        import uiautomation as _uia
        desktop = _uia.GetRootControl()
        elements = _find_interactive_elements(desktop, max_depth=8, max_results=60)
        lines.append(f"【UIA控件】共{len(elements)}个")
        for el in elements[:25]:
            t = el.get("type", "?")
            name = el.get("name", "")[:35]
            rect = el.get("rect", {})
            cx = (rect.get("left", 0) + rect.get("right", 0)) // 2 if rect else 0
            cy = (rect.get("top", 0) + rect.get("bottom", 0)) // 2 if rect else 0
            lines.append(f"  [{t}] {name} @({cx},{cy})")
    except ImportError:
        lines.append("【UIA不可用】")
    except Exception as e:
        lines.append(f"【UIA异常】{e}")
    return "\n".join(lines)


def _analyze_ocr() -> str:
    """OCR扫描屏幕文字，返回每段文字的坐标。优先Windows OCR，fallback easyocr。"""
    lines = []

    # 1. Windows OCR（内置，不下载模型）
    results = _ocr_via_windows()
    if results:
        lines.append(f"【OCR文字】共{len(results)}段 (Windows OCR)")
        for text, cx, cy, conf in results[:30]:
            lines.append(f"  \"{text[:25]}\" @({cx},{cy})")
        return "\n".join(lines)

    # 2. Fallback: easyocr
    reader = _get_ocr()
    if not reader or reader is True:  # True表示Windows OCR已检测但无结果
        return "【OCR不可用】"
    try:
        import pyautogui
        from PIL import Image as _PILImage
        import io

        buf = io.BytesIO()
        img = pyautogui.screenshot()
        w, h = img.size
        img = img.resize((w // 2, h // 2), _PILImage.LANCZOS)
        img.save(buf, format='PNG')
        img_bytes = buf.getvalue()

        results = reader.readtext(img_bytes)
        lines.append(f"【OCR文字】共{len(results)}段 (easyocr)")
        for bbox, text, conf in results[:30]:
            pts = [(int(p[0]) * 2, int(p[1]) * 2) for p in bbox]
            cx = sum(p[0] for p in pts) // 4
            cy = sum(p[1] for p in pts) // 4
            if conf > 0.3:
                lines.append(f"  \"{text[:25]}\" @({cx},{cy}) conf={conf:.1f}")
    except Exception as e:
        lines.append(f"【OCR异常】{e}")
    return "\n".join(lines)


def _capture_and_describe() -> Optional[str]:
    """截图并用多模态模型描述屏幕"""
    try:
        import pyautogui
        from PIL import Image as _PILImage
        import base64, io, requests
        from .config import _get_cfg

        w, h = pyautogui.size()
        screenshot_dir = os.path.join(os.path.dirname(__file__), "dong_screenshots")
        os.makedirs(screenshot_dir, exist_ok=True)
        path = os.path.join(screenshot_dir, f"analyze_{datetime.now().strftime('%H%M%S')}.png")
        pyautogui.screenshot(path)

        img = _PILImage.open(path)
        img = img.resize((w // 2, h // 2), _PILImage.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=50)
        img_b64 = base64.b64encode(buf.getvalue()).decode()

        vcfg = _get_cfg("vision")
        r = requests.post(
            f"{vcfg.api_base}/chat/completions",
            headers={"Authorization": f"Bearer {vcfg.api_key}", "Content-Type": "application/json"},
            json={
                "model": vcfg.model,
                "max_tokens": 120,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    {"type": "text", "text": "描述桌面：窗口布局、输入框位置、可见按钮。不超过60字。"}
                ]}]
            },
            timeout=25
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
    except Exception:
        pass
    return None


# ── UIA 元素枚举 ──
def _find_interactive_elements(control, max_depth=6, max_results=50, _depth=0, _results=None) -> List[Dict]:
    if _results is None:
        _results = []
    if _depth > max_depth or len(_results) >= max_results:
        return _results
    try:
        ct = control.ControlTypeName
        name = control.Name or ""
        if ct and name and ct not in ("Pane", "Window", "Group", "ToolBar"):
            rect = control.BoundingRectangle
            _results.append({
                "type": ct,
                "name": str(name)[:50],
                "rect": {
                    "left": rect.left, "top": rect.top,
                    "right": rect.right, "bottom": rect.bottom
                } if rect else {}
            })
        children = control.GetChildren()
        for child in children:
            _find_interactive_elements(child, max_depth, max_results, _depth + 1, _results)
            if len(_results) >= max_results:
                break
    except Exception:
        pass
    return _results


# ── 语义树 + 纯净截图（两阶段决策）──

def _node_semantic_key(node: Dict) -> str:
    parts = [node.get("type", ""), node.get("name", "").strip(), node.get("parent_win", "").strip()]
    return "|".join(p for p in parts if p)


def generate_ui_tree_and_screenshot(task_desc: str = "") -> Tuple[Optional[str], str, str, List[Dict]]:
    """
    截取纯净截图 + 生成裁剪纸后的语义树。
    返回: (截图路径, VLM精简树文本, 完整树文本, 完整节点列表)
    """
    try:
        import pyautogui
        import uiautomation as _uia
    except ImportError:
        return None, "", "", []

    screenshot_dir = os.path.join(os.path.dirname(__file__), "dong_screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    ts = datetime.now().strftime('%H%M%S')
    img_path = os.path.join(screenshot_dir, f"ui_{ts}.png")
    pyautogui.screenshot(img_path)

    desktop = _uia.GetRootControl()
    uia_elements = _find_interactive_elements_v2(desktop, max_depth=8, max_results=60)

    full_nodes = []
    for idx, el in enumerate(uia_elements, 1):
        name = el.get("name", "").strip()
        el_type = el.get("type", "")
        if not name and el_type not in ("Button", "Edit", "EditControl", "DocumentControl", "MenuItem", "TabItem"):
            continue
        rect = el.get("rect", {})
        x1, y1 = rect.get("left", 0), rect.get("top", 0)
        x2, y2 = rect.get("right", 0), rect.get("bottom", 0)
        if x1 >= x2 or y1 >= y2:
            continue
        node = {
            "id": idx, "type": el_type, "name": name,
            "parent_win": el.get("parent_win", ""),
            "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            "rect": (x1, y1, x2, y2),
        }
        node["semantic_key"] = _node_semantic_key(node)
        full_nodes.append(node)

    # 格式化
    def _fmt(nodes):
        lines = []
        for n in nodes:
            lines.append(f'[{n["id"]}] {n["type"]} "{n["name"]}" @({n["center"][0]},{n["center"][1]})')
        return "\n".join(lines)

    full_text = _fmt(full_nodes) if full_nodes else "(无元素)"

    # 裁剪给VLM的精简树
    vlm_nodes = _filter_for_vlm(full_nodes, task_desc, max_ele=25, max_ch=1200)
    vlm_text = _fmt(vlm_nodes) if vlm_nodes else full_text[:1200]

    return img_path, vlm_text, full_text, full_nodes


def _filter_for_vlm(nodes: List[Dict], task: str, max_ele: int, max_ch: int) -> List[Dict]:
    """按任务相关性 + 控件优先级裁剪"""
    import re as _re
    keywords = set(_re.findall(r"[\u4e00-\u9fa5_a-zA-Z0-9]+", task)) if task else set()

    def _prio(n):
        if n["type"] == "Window":
            return 0
        if any(k in n["name"] for k in keywords):
            return 1
        if n["type"] in ("Button", "Edit", "EditControl", "MenuItem", "TabItem", "CheckBox", "DocumentControl"):
            return 2
        return 3

    sorted_nodes = sorted(nodes, key=_prio)
    result, total = [], 0
    for n in sorted_nodes:
        line = f'[{n["id"]}] {n["type"]} "{n["name"]}"\n'
        if total + len(line) > max_ch or len(result) >= max_ele:
            continue
        result.append(n)
        total += len(line)
    return result


def _find_interactive_elements_v2(control, max_depth=8, max_results=60, _depth=0, _results=None, _parent_win="") -> List[Dict]:
    """增强版UIA枚举：带父窗口标题"""
    if _results is None:
        _results = []
    if _depth > max_depth or len(_results) >= max_results:
        return _results
    try:
        ct = control.ControlTypeName
        name = control.Name or ""
        # 跟踪窗口标题
        current_win = _parent_win
        if ct == "WindowControl" and name:
            current_win = str(name)[:50]

        if ct and name and ct not in ("Pane", "Window", "Group", "ToolBar"):
            rect = control.BoundingRectangle
            _results.append({
                "type": ct, "name": str(name)[:50], "parent_win": current_win,
                "rect": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom} if rect else {}
            })
        children = control.GetChildren()
        for child in children:
            _find_interactive_elements_v2(child, max_depth, max_results, _depth + 1, _results, current_win)
            if len(_results) >= max_results:
                break
    except Exception:
        pass
    return _results


# ── SoM 视觉标注（保留备用）──

def generate_som_screenshot() -> Tuple[Optional[str], List[Dict]]:
    """
    截屏并用 SoM (Set-of-Mark) 标注所有可交互元素。
    在截图上画红框+数字标签，VLM只需说"点[3]"而非猜坐标。
    返回: (标注图片路径, 元素映射表 [{id, type, name, center, bbox}, ...])
    """
    try:
        import pyautogui
        import uiautomation as _uia
    except ImportError:
        return None, []

    screenshot_dir = os.path.join(os.path.dirname(__file__), "dong_screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    ts = datetime.now().strftime('%H%M%S')
    raw_path = os.path.join(screenshot_dir, f"som_raw_{ts}.png")
    som_path = os.path.join(screenshot_dir, f"som_{ts}.png")

    pyautogui.screenshot(raw_path)
    img = Image.open(raw_path)
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("msyh.ttc", 14)
    except Exception:
        try:
            font = ImageFont.truetype("arial.ttf", 14)
        except Exception:
            font = ImageFont.load_default()

    # 收集 UIA 元素
    desktop = _uia.GetRootControl()
    uia_elements = _find_interactive_elements(desktop, max_depth=8, max_results=50)

    # 合并 + 去重
    elements_map = []
    element_id = 1

    for el in uia_elements:
        rect = el.get("rect", {})
        if not rect:
            continue
        x1, y1 = rect.get("left", 0), rect.get("top", 0)
        x2, y2 = rect.get("right", 0), rect.get("bottom", 0)
        if x1 >= x2 or y1 >= y2 or x2 - x1 > 800 or y2 - y1 > 600:
            continue
        _draw_som_box(draw, x1, y1, x2, y2, str(element_id), font)
        elements_map.append({
            "id": element_id,
            "type": el.get("type", ""),
            "name": el.get("name", "")[:30],
            "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            "bbox": (x1, y1, x2, y2)
        })
        element_id += 1

    img.save(som_path)
    return som_path, elements_map


def _draw_som_box(draw, x1, y1, x2, y2, label, font):
    """画 SoM 红框 + 数字标签"""
    draw.rectangle([x1, y1, x2, y2], outline="red", width=2)
    try:
        bbox = draw.textbbox((x1, y1 - 18), label, font=font)
        draw.rectangle([bbox[0] - 2, bbox[1] - 2, bbox[2] + 2, bbox[3] + 2], fill="red")
        draw.text((x1, y1 - 18), label, fill="white", font=font)
    except Exception:
        draw.rectangle([x1, y1 - 16, x1 + 20, y1], fill="red")
        draw.text((x1 + 2, y1 - 16), label, fill="white", font=font)


# ── 文字查找（OCR优先，UIA回退）──
def find_text_on_screen(text: str) -> Optional[Dict]:
    """在屏幕上查找文字，返回坐标。Windows OCR → easyocr → UIA"""
    # 1. Windows OCR（内置，最快）
    results = _ocr_via_windows()
    if results:
        text_lower = text.lower()
        best = None
        for t, cx, cy, conf in results:
            if text_lower in t.lower():
                best = {"type": "OCR文字", "name": t,
                        "rect": {"left": cx-20, "top": cy-10, "right": cx+20, "bottom": cy+10},
                        "x": cx, "y": cy, "center": (cx, cy)}
                break
        if best:
            return best

    # 2. Fallback: easyocr
    reader = _get_ocr()
    if reader and reader is not True:
        try:
            import pyautogui
            from PIL import Image as _PILImage
            import io

            buf = io.BytesIO()
            img = pyautogui.screenshot()
            w, h = img.size
            img = img.resize((w // 2, h // 2), _PILImage.LANCZOS)
            img.save(buf, format='PNG')
            results = reader.readtext(buf.getvalue())

            text_lower = text.lower()
            best, best_conf = None, 0
            for bbox, t, conf in results:
                if text_lower in t.lower() and conf > best_conf:
                    pts = [(int(p[0]) * 2, int(p[1]) * 2) for p in bbox]
                    cx = sum(p[0] for p in pts) // 4
                    cy = sum(p[1] for p in pts) // 4
                    best = {"type": "OCR文字", "name": t, "rect": {
                        "left": min(p[0] for p in pts), "top": min(p[1] for p in pts),
                        "right": max(p[0] for p in pts), "bottom": max(p[1] for p in pts)
                    }, "x": cx, "y": cy}
                    best_conf = conf
            if best:
                return best
        except Exception:
            pass

    # 2. UIA回退
    return find_element(text)


# ── 元素查找（UIA）──
def find_element(name: str = "", elem_type: str = "") -> Optional[Dict]:
    try:
        import uiautomation as _uia
        desktop = _uia.GetRootControl()
        elements = _find_interactive_elements(desktop, max_depth=8, max_results=100)

        best = None
        name_lower = name.lower()
        for el in elements:
            el_name = el.get("name", "").lower()
            if name_lower and name_lower not in el_name:
                continue
            if elem_type and elem_type.lower() not in el.get("type", "").lower():
                continue
            if best is None or len(el_name) > len(best.get("name", "")):
                best = el
        if best:
            rect = best.get("rect", {})
            best["x"] = (rect.get("left", 0) + rect.get("right", 0)) // 2
            best["y"] = (rect.get("top", 0) + rect.get("bottom", 0)) // 2
        return best
    except Exception:
        return None


# ── 点击 ──
def click_element(name: str = "", elem_type: str = "") -> str:
    """找到元素并点击（OCR优先）"""
    el = find_text_on_screen(name)
    if not el:
        el = find_element(name, elem_type)

    if not el:
        try:
            import uiautomation as _uia
            ctrl = _find_control_by_name(_uia.GetRootControl(), name)
            if ctrl:
                ctrl.Click()
                return f"[成功] UIA点击: {ctrl.Name}"
        except Exception:
            pass
        return f"[未找到] 屏幕和控件中都没有'{name}'"

    cx = el.get("x") or ((el.get("rect", {}).get("left", 0) + el.get("rect", {}).get("right", 0)) // 2)
    cy = el.get("y") or ((el.get("rect", {}).get("top", 0) + el.get("rect", {}).get("bottom", 0)) // 2)
    try:
        import pyautogui
        pyautogui.click(cx, cy)
        return f"[成功] 点击 '{el['name'][:30]}' @({cx},{cy})"
    except Exception as e:
        return f"[错误] 点击失败: {e}"


def _find_control_by_name(control, name, max_depth=6, _depth=0):
    if _depth > max_depth:
        return None
    try:
        if control.Name and name.lower() in str(control.Name).lower():
            return control
        for child in control.GetChildren():
            result = _find_control_by_name(child, name, max_depth, _depth + 1)
            if result:
                return result
    except Exception:
        pass
    return None
