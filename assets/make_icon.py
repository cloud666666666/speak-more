# coding: utf-8
"""
生成 Speak More 的应用图标

设计思路：**圆角方块 + 波形柱**。
- 语音输入工具的通用视觉语言就是声波，含义不需要解释
- 缩到 16×16 的托盘尺寸还得认得出 —— 所以**小尺寸用简化版**：
  16/24/32 画 3 根粗柱 + 接近纯色的底；48 以上才用 5 根柱 + 完整对角渐变。
  5 根细柱在 16 像素里会挤成一团糊掉（试过）。
- 每个尺寸都在 4 倍画布上画完再缩下来，这样圆角和柱子边缘才有正确的抗锯齿，
  而不是靠 Pillow 从大图硬缩。

跑法：
    .venv/Scripts/python.exe assets/make_icon.py

产出：
    assets/icon.ico            多尺寸，托盘和 exe 用
    assets/icon_512.png        大图，安装向导 / 关于页用
    assets/icon_preview_16.png 16×16 放大预览，用来肉眼检查小尺寸可读性
"""

from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import Dict, List, Sequence

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent

SUPERSAMPLE = 4                     # 先放大 4 倍画再缩，边缘更干净
CORNER_RATIO = 0.22                 # 圆角半径占总宽比例

# 大尺寸：5 根柱 + 完整对角渐变
BARS_LARGE: Sequence[float] = [0.34, 0.60, 1.00, 0.56, 0.82]
BAR_W_LARGE = 0.088
BAR_GAP_LARGE = 0.058
BAR_MAX_H = 0.52

# 小尺寸：3 根粗柱（16px 下才分得开）
BARS_SMALL: Sequence[float] = [0.52, 1.00, 0.70]
BAR_W_SMALL = 0.150
BAR_GAP_SMALL = 0.090
SMALL_THRESHOLD = 48                # 小于这个尺寸走简化版

COLOR_TOP = (59, 130, 246)          # #3b82f6 蓝
COLOR_BOTTOM = (6, 182, 212)        # #06b6d4 青
COLOR_SMALL = (29, 111, 208)        # #1d6fd0，小尺寸用（对比度更好）

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
PREVIEW_SIZE = 16


# ============================================================
#  绘制
# ============================================================

def _gradient(size: int) -> Image.Image:
    """
    按 (x+y) 走的对角渐变。

    先在小画布上逐像素算再放大 —— 线性渐变放大后是精确的，
    而在大画布上跑百万次 Python 循环要好秒，没必要。
    """
    steps = 256
    small = Image.new('RGB', (steps, steps))
    px = small.load()
    denom = 2 * (steps - 1)
    for y in range(steps):
        for x in range(steps):
            t = (x + y) / denom
            px[x, y] = (
                int(COLOR_TOP[0] + (COLOR_BOTTOM[0] - COLOR_TOP[0]) * t),
                int(COLOR_TOP[1] + (COLOR_BOTTOM[1] - COLOR_TOP[1]) * t),
                int(COLOR_TOP[2] + (COLOR_BOTTOM[2] - COLOR_TOP[2]) * t),
            )
    return small.resize((size, size), Image.Resampling.BILINEAR)


def render(size: int) -> Image.Image:
    """渲染指定尺寸的图标。"""
    simple = size < SMALL_THRESHOLD
    canvas_px = size * SUPERSAMPLE

    # ---- 底 ----
    img = Image.new('RGBA', (canvas_px, canvas_px), (0, 0, 0, 0))
    mask = Image.new('L', (canvas_px, canvas_px), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, canvas_px - 1, canvas_px - 1],
        radius=int(canvas_px * CORNER_RATIO), fill=255)

    if simple:
        img.paste(Image.new('RGB', (canvas_px, canvas_px), COLOR_SMALL),
                  (0, 0), mask)
    else:
        img.paste(_gradient(canvas_px), (0, 0), mask)

    # ---- 波形柱 ----
    draw = ImageDraw.Draw(img)
    bars = BARS_SMALL if simple else BARS_LARGE
    bar_w = canvas_px * (BAR_W_SMALL if simple else BAR_W_LARGE)
    gap = canvas_px * (BAR_GAP_SMALL if simple else BAR_GAP_LARGE)
    total = len(bars) * bar_w + (len(bars) - 1) * gap
    x0 = (canvas_px - total) / 2
    max_h = canvas_px * BAR_MAX_H

    for i, ratio in enumerate(bars):
        h = max_h * ratio
        x = x0 + i * (bar_w + gap)
        y0 = (canvas_px - h) / 2
        draw.rounded_rectangle([x, y0, x + bar_w, y0 + h],
                               radius=bar_w / 2, fill=(255, 255, 255, 255))

    return img.resize((size, size), Image.Resampling.LANCZOS)


# ============================================================
#  ICO 组装
# ============================================================
#
#  手写不用 Pillow 的 ICO 保存，是因为它只接受"一张图 + 一串尺寸"，
#  没法给不同尺寸喂不同设计。而小尺寸恰恰需要简化版。
#  ICO 格式很简单：6 字节目录头 + 每张 16 字节目录项 + 图像数据，
#  Vista 以后可以直接塞 PNG。

def save_ico(images: Dict[int, Image.Image], path: Path) -> None:
    sizes = sorted(images, reverse=True)
    entries = []
    blobs: List[bytes] = []
    offset = 6 + 16 * len(sizes)

    for s in sizes:
        buf = io.BytesIO()
        images[s].save(buf, format='PNG')
        blob = buf.getvalue()
        blobs.append(blob)
        entries.append(struct.pack(
            '<BBBBHHII',
            0 if s >= 256 else s,      # 宽（0 表示 256）
            0 if s >= 256 else s,      # 高
            0, 0,                      # 调色板数 / 保留
            1, 32,                     # 色彩平面 / 位深
            len(blob), offset,
        ))
        offset += len(blob)

    with open(path, 'wb') as f:
        f.write(struct.pack('<HHH', 0, 1, len(sizes)))   # 保留 / 类型=图标 / 数量
        for e in entries:
            f.write(e)
        for b in blobs:
            f.write(b)


def main() -> None:
    images = {s: render(s) for s in ICO_SIZES}

    ico_path = OUT_DIR / 'icon.ico'
    save_ico(images, ico_path)
    print(f'✅ {ico_path.name}   {ico_path.stat().st_size / 1024:.1f} KB  '
          f'尺寸 {ICO_SIZES}')

    png_path = OUT_DIR / 'icon_512.png'
    render(512).save(png_path, format='PNG')
    print(f'✅ {png_path.name}   {png_path.stat().st_size / 1024:.1f} KB')

    preview_path = OUT_DIR / 'icon_preview_16.png'
    images[PREVIEW_SIZE].resize((256, 256), Image.Resampling.NEAREST).save(
        preview_path, format='PNG')
    print(f'✅ {preview_path.name}  （{PREVIEW_SIZE}×{PREVIEW_SIZE} 放大 16 倍，'
          f'检查小尺寸可读性）')


if __name__ == '__main__':
    main()
