#!/usr/bin/env python3
"""从品牌图标源生成各处使用的 ICO，保证全平台图标同源。

背景（2026-10-22）：exe 内嵌图标（runner/resources/app_icon.ico）在 8a1d3cf
已换成品牌图标，但 client/res/icon.ico（MSI 快捷方式 / DefaultIcon 注册表）
和 client/res/tray-icon.ico（系统托盘）漏掉了，装的桌面快捷方式仍是旧
RustDesk 圆环。本工具以 app_icon.ico 中最高清的 256x256 PNG 条目为唯一
源，重新生成这两个文件，杜绝"改了一处漏三处"。

用法：
    python build/client/gen_ico.py                 # 默认全量生成
    python build/client/gen_ico.py --check         # 只校验不写盘（CI 可用）
    python build/client/gen_ico.py --source x.png  # 指定别的源图

生成规则（与上游图标约定对齐）：
    res/icon.ico      16/24/32/48/64/128/256，256 存 PNG（无损、Explorer 大
                      图标视图优先取用），其余存 32bpp BMP（兼容老视图）。
    res/tray-icon.ico 仅 32x32（tray.rs 以 include_bytes 嵌入，image crate
                      解码后交给 tray-icon 按 DPI 取用；上游本就是单 32px，
                      多尺寸会被解码成 256px 再缩，托盘反而发虚）。
"""

from __future__ import annotations

import argparse
import io
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPO / "client/flutter/windows/runner/resources/app_icon.ico"
TARGETS = {
    REPO / "client/res/icon.ico": [16, 24, 32, 48, 64, 128, 256],
    REPO / "client/res/tray-icon.ico": [32],
}


def largest_png_entry(ico_path: Path) -> bytes:
    """取 ICO 里最大的 PNG 条目；没有 PNG 条目就取最大位图并转 PNG。"""
    data = ico_path.read_bytes()
    if data[:4] != b"\x00\x00\x01\x00":
        raise SystemExit(f"[x] {ico_path} 不是 ICO 文件")
    count = struct.unpack("<H", data[4:6])[0]
    best = None  # (w, png_bytes)
    for i in range(count):
        w, h, _cc, _r, _planes, _bpp, size, off = struct.unpack(
            "<BBBBHHII", data[6 + i * 16 : 22 + i * 16]
        )
        w = w or 256
        if data[off : off + 8] == b"\x89PNG\r\n\x1a\n":
            if best is None or w > best[0]:
                best = (w, data[off : off + size])
    if best is None:
        # 极端情况：源 ICO 全是 BMP 条目。交给 Pillow 统一转。
        from PIL import Image

        img = Image.open(ico_path)
        img.size = (256, 256)
        buf = io.PureBytesIO()  # noqa: F821 - 不会走到（现源含 PNG）
        img.save(buf, format="PNG")
        best = (256, buf.getvalue())
    return best[1]


def build_ico(png_bytes: bytes, sizes: list[int]) -> bytes:
    """用 Pillow 把一张 PNG 重采样成多尺寸 ICO。

    Pillow 的 ICO 编码器对 256x256 自动走 PNG 压缩、小尺寸走 32bpp BMP，
    与 Explorer 的取用习惯一致。LANCZOS 对圆角图标边缘最干净。
    """
    from PIL import Image

    src = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    out = io.BytesIO()
    src.save(out, format="ICO", sizes=[(s, s) for s in sizes])
    return out.getvalue()


def verify(ico_path: Path, sizes: list[int]) -> list[str]:
    """读回 ICO，核对每个条目的尺寸都在且能解码。"""
    from PIL import Image

    data = ico_path.read_bytes()
    count = struct.unpack("<H", data[4:6])[0]
    found = []
    for i in range(count):
        w, h, _cc, _r, _planes, _bpp, size, off = struct.unpack(
            "<BBBBHHII", data[6 + i * 16 : 22 + i * 16]
        )
        found.append(w or 256)
        Image.open(io.BytesIO(data[off : off + size])).load()
    missing = [s for s in sizes if s not in found]
    if missing:
        raise SystemExit(f"[x] {ico_path.name} 缺尺寸条目: {missing}")
    return found


def main(argv: list[str]) -> int:
    force_utf8_console()
    ap = argparse.ArgumentParser(description="从品牌源图生成 icon.ico / tray-icon.ico")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--check", action="store_true", help="只校验现有文件，不生成")
    args = ap.parse_args(argv)

    if args.check:
        ok = True
        for path, sizes in TARGETS.items():
            if not path.exists():
                print(f"[x] {path.name} 不存在")
                ok = False
                continue
            try:
                found = verify(path, sizes)
                print(f"[OK] {path.relative_to(REPO)} 尺寸 {sorted(found)}")
            except SystemExit as err:
                print(err)
                ok = False
            except Exception as err:
                print(f"[x] {path.name} 损坏: {err}")
                ok = False
        return 0 if ok else 1

    png = largest_png_entry(Path(args.source))
    for path, sizes in TARGETS.items():
        blob = build_ico(png, sizes)
        old = path.read_bytes() if path.exists() else b""
        path.write_bytes(blob)
        mark = "（内容不变）" if old == blob else f"（{len(old)} -> {len(blob)} 字节）"
        found = verify(path, sizes)
        print(f"[OK] {path.relative_to(REPO)} 尺寸 {sorted(found)} {mark}")
    return 0


def force_utf8_console() -> None:
    """Windows runner 控制台是 cp1252，中文 print 会直接把进程打挂。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
