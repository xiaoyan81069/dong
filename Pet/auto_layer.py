"""自动分层脚本 — 通过 z-image.ai 在线服务将立绘拆分为 RGBA 图层"""
import requests
import time
import os

IMAGE_PATH = r"C:\Users\15927\WorkBuddy\2026-05-12-task-1\winter_character_20260512.png"
OUTPUT_DIR = r"C:\Users\15927\AppData\Roaming\CherryStudio\Data\Agents\r7j6igibk\desktop_pet\res\role\冬\分层"

def try_zimage():
    """试试 z-image.ai"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # z-image.ai 的前端页面
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": "https://z-image.ai",
        "Referer": "https://z-image.ai/zh/qwen-image-layered",
    })

    # 先访问页面获取可能的 CSRF token
    page = session.get("https://z-image.ai/zh/qwen-image-layered", timeout=15)
    print(f"[z-image] 页面状态: {page.status_code}")

    # 检查是否有 Gradio 后端 API
    # 尝试常见的 Gradio API 端点
    api_urls = [
        "https://z-image.ai/api/upload",
        "https://z-image.ai/gradio_api/",
        "https://z-image.ai/api/predict",
    ]

    for url in api_urls:
        try:
            r = session.get(url, timeout=10)
            print(f"[z-image] {url} → {r.status_code}")
        except Exception as e:
            print(f"[z-image] {url} → {e}")

    print("\n[z-image] 需要浏览器交互，无法纯脚本上传。换方案...")


def try_fal():
    """通过 fal.ai API 分层 (需要 FAL_KEY)"""
    fal_key = os.environ.get("FAL_KEY", "")
    if not fal_key:
        print("[fal.ai] 未设置 FAL_KEY 环境变量")
        print("[fal.ai] 获取免费额度: https://fal.ai/dashboard → Settings → API Keys")
        print("[fal.ai] 然后运行: set FAL_KEY=你的key && python auto_layer.py")
        return False

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 先用 fal 的文件上传 API 上传图片
    print("[fal.ai] 上传图片...")

    # 直接使用图片 URL 不太方便，试试 fal 的 storage upload
    # 或者用 data URL
    import base64
    with open(IMAGE_PATH, "rb") as f:
        img_data = base64.b64encode(f.read()).decode()

    # fal.ai 支持通过 REST API 先上传文件
    upload_resp = requests.post(
        "https://rest.fal.ai/storage/upload",
        headers={
            "Authorization": f"Key {fal_key}",
            "Content-Type": "application/json",
        },
        json={"file_name": "winter_character.png", "content_type": "image/png"},
        timeout=15
    )

    if upload_resp.status_code != 200:
        print(f"[fal.ai] 上传失败: {upload_resp.status_code} {upload_resp.text}")
        return False

    upload_info = upload_resp.json()
    upload_url = upload_info.get("upload_url")
    file_url = upload_info.get("file_url")

    # PUT 上传文件到 presigned URL
    with open(IMAGE_PATH, "rb") as f:
        put_resp = requests.put(upload_url, data=f.read(),
                                headers={"Content-Type": "image/png"}, timeout=30)

    if put_resp.status_code not in (200, 201):
        print(f"[fal.ai] PUT上传失败: {put_resp.status_code}")
        return False

    print(f"[fal.ai] 图片上传成功: {file_url}")
    print("[fal.ai] 提交分层任务 (约15-30秒)...")

    # 提交任务
    submit_resp = requests.post(
        "https://fal.run/fal-ai/qwen-image-layered",
        headers={
            "Authorization": f"Key {fal_key}",
            "Content-Type": "application/json",
        },
        json={
            "image_url": file_url,
            "num_layers": 6,  # 冬的立绘比较复杂，多分几层
            "num_inference_steps": 28,
            "output_format": "png",
        },
        timeout=120
    )

    if submit_resp.status_code != 200:
        print(f"[fal.ai] 任务提交失败: {submit_resp.status_code} {submit_resp.text}")
        return False

    result = submit_resp.json()
    images = result.get("images", [])

    print(f"[fal.ai] 获得 {len(images)} 个图层，正在下载...")
    for i, img in enumerate(images):
        img_url = img.get("url", "")
        if not img_url:
            continue
        img_data = requests.get(img_url, timeout=30).content
        out_path = os.path.join(OUTPUT_DIR, f"layer_{i:02d}.png")
        with open(out_path, "wb") as f:
            f.write(img_data)
        print(f"  ✓ layer_{i:02d}.png ({len(img_data)} bytes)")

    print(f"\n[fal.ai] 完成! 图层保存在: {OUTPUT_DIR}")
    return True


def try_anime_seg():
    """本地轻量方案：用 anime-segmentation 分割 (CPU可用)"""
    try:
        from transformers import AutoImageProcessor, AutoModelForSemanticSegmentation
        from PIL import Image
        import torch
        import numpy as np

        print("[anime-seg] 加载模型...")
        model_name = "skytnt/anime-seg"
        processor = AutoImageProcessor.from_pretrained(model_name)
        model = AutoModelForSemanticSegmentation.from_pretrained(model_name)

        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[anime-seg] 使用设备: {device}")
        model = model.to(device)

        img = Image.open(IMAGE_PATH).convert("RGB")
        inputs = processor(images=img, return_tensors="pt")
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = model(**inputs)

        # 获取分割mask
        logits = outputs.logits
        mask = torch.argmax(logits, dim=1).squeeze().cpu().numpy()

        img_np = np.array(img)
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # 对每个类别生成带透明通道的图层
        unique_classes = np.unique(mask)
        for cls_id in unique_classes:
            alpha = (mask == cls_id).astype(np.uint8) * 255
            rgba = np.zeros((img_np.shape[0], img_np.shape[1], 4), dtype=np.uint8)
            rgba[:, :, :3] = img_np
            rgba[:, :, 3] = alpha

            out_path = os.path.join(OUTPUT_DIR, f"seg_layer_{int(cls_id):02d}.png")
            Image.fromarray(rgba).save(out_path)
            print(f"  ✓ seg_layer_{int(cls_id):02d}.png")

        print(f"[anime-seg] 完成! 图层保存在: {OUTPUT_DIR}")
        return True

    except ImportError:
        print("[anime-seg] 需要安装: pip install transformers torch torchvision pillow numpy")
        return False
    except Exception as e:
        print(f"[anime-seg] 出错: {e}")
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("冬 · 立绘自动分层")
    print("=" * 60)
    print()

    # 方案1: z-image.ai (免费但需要浏览器)
    # try_zimage()

    # 方案2: fal.ai (付费 $0.05/次)
    if try_fal():
        exit(0)

    # 方案3: 本地 anime-seg (免费, CPU可用, 但质量一般)
    print("\n--- 尝试本地方案 ---")
    if try_anime_seg():
        exit(0)

    print("\n=== 所有方案均失败 ===")
    print("推荐: 手动访问 https://z-image.ai/zh/qwen-image-layered 上传立绘")
