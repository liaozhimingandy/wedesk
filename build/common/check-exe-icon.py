#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# 校验 Windows exe 内嵌的图标资源（RT_GROUP_ICON / RT_ICON）
#
# 【为什么需要它】
#   wedesk.exe 的图标来自 flutter/windows/runner/Runner.rc 的 IDI_APP_ICON，
#   指向 resources/app_icon.ico，由 RC 编译器在 flutter build windows 时
#   编进产物。这条链上任一环断掉（Runner.rc 被改坏、ico 文件丢失、CMake
#   没编译 rc），构建日志里一个字都不会提 —— 用户拿到的是一个"看起来正常
#   但图标空白"的 exe。本脚本对产物直接做资源级校验，把这种问题变成 CI 红灯。
#
# 【校验什么】
#   1. exe 必须含 RT_GROUP_ICON（组图标目录），且至少引用一个 RT_ICON；
#   2. 组图标引用的每个 RT_ICON 都必须存在，且字节数与目录声明一致；
#   3. 最大的那张位图不能是"纯色/全同字节"——空白图标（例如整张全透明的
#      32bpp DIB 就是一片 0x00）在这一步现形；
#   4.（软校验）若同时给出 .ico 源文件，比对 exe 内嵌位图与 .ico 中的位图
#      是否逐字节一致，不一致只发警告不失败 —— RC 的存储细节不做强假设。
#
# 用法：
#   python check-exe-icon.py <exe> [icon.ico]
# 退出码：0 = 通过（含软校验警告）；1 = 硬校验失败
# =============================================================================

from __future__ import annotations

import struct
import sys
from pathlib import Path

RT_ICON = 3
RT_GROUP_ICON = 14

USAGE = "用法: python check-exe-icon.py <exe> [icon.ico]"


class PeResourceError(Exception):
    pass


def _rva_to_offset(rva: int, sections) -> int | None:
    """把 RVA 换算成文件偏移。sections: [(vsize, vaddr, rawsize, rawptr), ...]"""
    for vsize, vaddr, rawsize, rawptr in sections:
        span = max(vsize, rawsize)
        if vaddr <= rva < vaddr + span:
            return rawptr + (rva - vaddr)
    return None


def _parse_pe(data: bytes):
    """返回 (rsrc_rva, sections)。解析失败抛 PeResourceError。"""
    if data[:2] != b"MZ":
        raise PeResourceError("不是 PE 文件（缺少 MZ 头）")
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_off : pe_off + 4] != b"PE\0\0":
        raise PeResourceError("不是 PE 文件（缺少 PE 签名）")
    coff = pe_off + 4
    (nsec,) = struct.unpack_from("<H", data, coff + 2)
    (optsize,) = struct.unpack_from("<H", data, coff + 16)
    opt = coff + 20
    (magic,) = struct.unpack_from("<H", data, opt)
    if magic not in (0x10B, 0x20B):
        raise PeResourceError(f"无法识别的可选头魔数 0x{magic:x}")
    # 数据目录表：PE32 在可选头 +96，PE32+ 在 +112；资源是第 3 个目录（索引 2）
    dd_off = opt + (112 if magic == 0x20B else 96)
    rsrc_rva, _rsrc_size = struct.unpack_from("<II", data, dd_off + 2 * 8)
    sec_table = opt + optsize
    sections = []
    for i in range(nsec):
        off = sec_table + i * 40
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, off + 8)
        sections.append((vsize, vaddr, rawsize, rawptr))
    return rsrc_rva, sections


def collect_icon_resources(data: bytes) -> tuple[dict[int, bytes], list[bytes]]:
    """走资源树，返回 ({RT_ICON 的资源名 id: 位图字节}, [RT_GROUP_ICON 原始字节, ...])。

    组图标可能有多个（多语言、或多套图标），所以用列表全收，免得校验漏掉
    除最后一个之外的组。
    """
    rsrc_rva, sections = _parse_pe(data)
    if rsrc_rva == 0:
        raise PeResourceError("PE 没有资源目录（.rsrc 节缺失）")
    base_off = _rva_to_offset(rsrc_rva, sections)
    if base_off is None:
        raise PeResourceError(f"资源目录 RVA 0x{rsrc_rva:x} 不在任何节里")

    icons: dict[int, bytes] = {}
    groups: list[bytes] = []

    def read_dir(off: int):
        # 资源目录头 16 字节，后接条目；高位置位的 Name/Id 是名字，否则是 ID
        (n_named, n_id) = struct.unpack_from("<HH", data, off + 12)
        entries = []
        for i in range(n_named + n_id):
            name_or_id, offset = struct.unpack_from("<II", data, off + 16 + i * 8)
            entries.append((name_or_id, offset))
        return entries

    def walk(off: int, level: int, type_id: int | None, name_id: int | None):
        for name_or_id, offset in read_dir(off):
            if offset & 0x80000000:
                sub = base_off + (offset & 0x7FFFFFFF)
                if level == 1:
                    walk(sub, 2, name_or_id, None)
                elif level == 2:
                    walk(sub, 3, type_id, name_or_id)
            else:
                if level != 3:
                    continue
                # 叶子：指向 DATA_ENTRY（RVA + Size + CodePage + Reserved）
                entry_off = base_off + offset
                data_rva, size = struct.unpack_from("<II", data, entry_off)
                file_off = _rva_to_offset(data_rva, sections)
                if file_off is None:
                    raise PeResourceError(f"资源数据 RVA 0x{data_rva:x} 不在任何节里")
                blob = data[file_off : file_off + size]
                if type_id == RT_ICON and name_id is not None:
                    icons[name_id] = blob
                elif type_id == RT_GROUP_ICON:
                    groups.append(blob)

    walk(base_off, 1, None, None)
    return icons, groups


def parse_group_icon(group: bytes) -> list[tuple[int, int, int]]:
    """解析 RT_GROUP_ICON 数据，返回 [(width, height, bytes_in_res, icon_id), ...]。"""
    if len(group) < 6:
        raise PeResourceError("RT_GROUP_ICON 数据不足 6 字节")
    reserved, typ, count = struct.unpack_from("<HHH", group, 0)
    if reserved != 0 or typ != 1:
        raise PeResourceError(f"RT_GROUP_ICON 头异常（reserved={reserved}, type={typ}）")
    if count == 0:
        raise PeResourceError("RT_GROUP_ICON 声明 0 张图标")
    need = 6 + count * 14
    if len(group) < need:
        raise PeResourceError(f"RT_GROUP_ICON 数据不足：声明 {count} 张，实际只有 {len(group)} 字节")
    out = []
    for i in range(count):
        off = 6 + i * 14
        w, h, _cc, _rsv, _planes, _bits, size, icon_id = struct.unpack_from(
            "<BBBBHHIH", group, off
        )
        out.append((w or 256, h or 256, size, icon_id))
    return out


def parse_ico(path: Path) -> dict[int, bytes]:
    """解析 .ico 容器，返回 {尺寸: 位图字节}（尺寸 = 宽，图标通常为正方形）。"""
    data = path.read_bytes()
    if data[:4] != b"\x00\x00\x01\x00":
        raise PeResourceError(f"{path} 不是合法 ICO 文件")
    (count,) = struct.unpack_from("<H", data, 4)
    out: dict[int, bytes] = {}
    for i in range(count):
        w, h, _cc, _rsv, _planes, _bits, size, off = struct.unpack_from(
            "<BBBBHHII", data, 6 + i * 16
        )
        out[w or 256] = data[off : off + size]
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(USAGE)
        return 2
    exe = Path(argv[1])
    ico = Path(argv[2]) if len(argv) > 2 else None

    try:
        data = exe.read_bytes()
    except OSError as err:
        print(f"[x] 读不了 {exe}: {err}")
        return 1

    try:
        icons, groups = collect_icon_resources(data)
    except (PeResourceError, struct.error) as err:
        print(f"[x] {exe.name} 图标校验失败: {err}")
        print("    检查 client/flutter/windows/runner/Runner.rc 的 IDI_APP_ICON")
        print("    与 resources/app_icon.ico 是否在位、CMake 是否编译了 Runner.rc")
        return 1

    if not groups:
        print(f"[x] {exe.name} 没有 RT_GROUP_ICON 资源 —— 图标根本没编进 exe")
        print("    检查 client/flutter/windows/runner/Runner.rc 的 IDI_APP_ICON")
        print("    与 resources/app_icon.ico 是否在位、CMake 是否编译了 Runner.rc")
        return 1

    entries: list[tuple[int, int, int]] = []
    for group in groups:
        try:
            entries.extend(parse_group_icon(group))
        except (PeResourceError, struct.error) as err:
            print(f"[x] {exe.name} 组图标目录损坏: {err}")
            return 1
    if not entries:
        print(f"[x] {exe.name} 的组图标目录一张图标都没声明")
        return 1

    problems: list[str] = []
    for w, h, size, icon_id in entries:
        blob = icons.get(icon_id)
        if blob is None:
            problems.append(f"{w}x{h} 引用的 RT_ICON id={icon_id} 不存在")
        elif len(blob) != size:
            problems.append(
                f"{w}x{h} 的 RT_ICON id={icon_id} 大小不符：目录声明 {size}，实际 {len(blob)}"
            )
    if problems:
        print(f"[x] {exe.name} 图标资源不完整：")
        for p in problems:
            print(f"    - {p}")
        return 1

    sizes = ", ".join(f"{w}x{h}" for w, h, _s, _i in entries)
    biggest = max(entries, key=lambda e: e[2])
    biggest_blob = icons[biggest[3]]
    distinct = len(set(biggest_blob))
    if distinct < 2:
        print(
            f"[x] {exe.name} 最大的图标（{biggest[0]}x{biggest[1]}）是空白图："
            f"{len(biggest_blob)} 字节只有 {distinct} 种取值"
        )
        print("    resources/app_icon.ico 内容异常，换一张真正的图标")
        return 1

    print(f"[OK] {exe.name} 内嵌 {len(entries)} 张图标（{sizes}），"
          f"最大 {biggest[0]}x{biggest[1]} / {len(biggest_blob)} 字节 / {distinct} 种取值")

    if ico is not None:
        try:
            want = parse_ico(ico)
        except (PeResourceError, OSError, struct.error) as err:
            print(f"[warn] 无法解析 {ico}: {err}（跳过比对）")
            return 0
        got = {w: icons[i] for w, _h, _s, i in entries if i in icons}
        missing = [s for s, blob in want.items() if got.get(s) != blob]
        if missing:
            print(
                f"[warn] exe 内嵌图标与 {ico.name} 不一致（尺寸 {', '.join(str(s) for s in missing)}）"
                "——图标可能是旧的或被换过，仅警告不失败"
            )
        else:
            print(f"[OK] exe 内嵌图标与 {ico.name} 逐字节一致")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
