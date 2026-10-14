#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check-env.py —— Windows 本地构建环境体检

构建 RustDesk 需要的组件很多，缺任何一个都会在编译到一半时才报错，
排查成本很高。这个脚本一次性把所有前置条件检查清楚。

用法:
    python check-env.py
    python check-env.py --fix-hint     # 显示每个缺失项的具体安装方法
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

OK = "+"
BAD = "x"
WARN = "!"

# 绿色/红色/黄色
G = R = Y = D = N = ""
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _c(code, txt):
    return f"\033[{code}m{txt}\033[0m" if os.environ.get("NO_COLOR") is None else txt


GREEN = lambda t: _c("32", t)
RED = lambda t: _c("31", t)
YELL = lambda t: _c("33", t)
DIM = lambda t: _c("2", t)
BOLD = lambda t: _c("1", t)


def run(cmd):
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, shell=True, timeout=30
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


def check_version(name, cmd, pattern, expect=None, extra_path=None):
    """执行检查并返回 (是否OK, 实际版本, 说明)"""
    env = os.environ.copy()
    if extra_path:
        env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
    rc, out = run(cmd)
    if rc != 0:
        return False, None, "未安装或不在 PATH 中"
    m = re.search(pattern, out)
    if not m:
        return False, None, "已安装但无法识别版本"
    ver = m.group(1)
    note = ""
    if expect and ver != expect:
        note = f"期望 {expect}"
        return "warn", ver, note
    return True, ver, note


def find_msvc():
    """定位 Visual Studio Build Tools / MSVC 工具链"""
    roots = [
        Path(r"C:\Program Files\Microsoft Visual Studio\2022"),
        Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2022"),
        Path(r"C:\Program Files\Microsoft Visual Studio\2019"),
        Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2019"),
    ]
    for base in roots:
        if not base.exists():
            continue
        for edition in base.iterdir():
            vcv = edition / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
            if vcv.exists():
                return True, str(vcv), edition.name
    return False, None, None


def find_winsdk():
    base = Path(r"C:\Program Files (x86)\Windows Kits\10\bin")
    if not base.exists():
        return False, None
    vers = sorted(
        [d.name for d in base.iterdir() if re.match(r"^10\.", d.name)], reverse=True
    )
    return True, vers[0] if vers else None


def main():
    print()
    print(BOLD("RustDesk Windows 本地构建 —— 环境体检"))
    print(DIM("=" * 60))

    results = []

    # ---------- 必需：MSVC ----------
    has_msvc, vcv_path, edition = find_msvc()
    results.append(
        (
            "MSVC 工具链" if has_msvc else "MSVC 工具链（阻塞项）",
            has_msvc,
            edition if has_msvc else "未找到 VS 2022/2019 + C++ 桌面开发组件",
            "critical",
        )
    )
    has_sdk, sdk_ver = find_winsdk()
    results.append(
        (
            "Windows SDK",
            has_sdk,
            sdk_ver or "未安装",
            "critical" if not has_sdk else "ok",
        )
    )

    # ---------- Rust ----------
    cargo_home = Path.home() / ".cargo" / "bin"
    extra = str(cargo_home) if cargo_home.exists() else None
    rc, out = run(
        f'"{cargo_home / "cargo.exe"}" --version'
        if cargo_home.exists()
        else "cargo --version"
    )
    if rc == 0:
        st, ver, note = check_version("rust", None, r"cargo (\S+)", None, extra)
        results.append(("Rust", True, out.strip().splitlines()[0], "ok"))
    else:
        results.append(("Rust", False, "未安装（需要 1.75）", "critical"))

    # ---------- Flutter ----------
    fcmd = shutil.which("flutter")
    if fcmd:
        rc, out = run("flutter --version")
        m = re.search(r"Flutter (\S+)", out)
        ver = m.group(1) if m else "?"
        ok = ver == "3.24.5"
        results.append(
            (
                "Flutter",
                ok,
                ver + ("" if ok else "  (官方 CI 要求 3.24.5)"),
                "ok" if ok else "warn",
            )
        )
    else:
        results.append(("Flutter", False, "未安装（需要 3.24.5）", "critical"))

    # ---------- vcpkg ----------
    vroot = os.environ.get("VCPKG_ROOT")
    if vroot and Path(vroot).exists():
        results.append(("vcpkg", True, vroot, "ok"))
        inst = Path(vroot) / "installed" / "x64-windows-static"
        if inst.exists():
            libs = [d.name for d in inst.iterdir() if d.is_dir()]
            need = {"libvpx", "libyuv", "opus", "aom"}
            missing = need - set(libs)
            results.append(
                (
                    "vcpkg 依赖库",
                    not missing,
                    (
                        "已编译: " + ", ".join(sorted(need & set(libs)))
                        if not missing
                        else "缺少: " + ", ".join(sorted(missing))
                    ),
                    "ok" if not missing else "warn",
                )
            )
        else:
            results.append(("vcpkg 依赖库", False, "尚未安装 C++ 依赖", "warn"))
    else:
        results.append(("vcpkg", False, "VCPKG_ROOT 未设置", "critical"))

    # ---------- LLVM ----------
    llvm = Path(r"C:\Program Files\LLVM\bin\clang.exe")
    if llvm.exists():
        rc, out = run(f'"{llvm}" --version')
        m = re.search(r"version (\S+)", out)
        ver = m.group(1) if m else "?"
        results.append(("LLVM/Clang", True, ver, "ok"))
    else:
        results.append(("LLVM/Clang", False, "未安装（需 15.0.6）", "critical"))

    # ---------- 构建辅助 ----------
    for exe, name in [("cmake", "CMake"), ("7z", "7-Zip"), ("curl", "curl")]:
        p = shutil.which(exe)
        results.append((name, bool(p), p or "未找到", "warn" if not p else "ok"))

    # ---------- 源码 ----------
    src = Path(r"D:\Git\rustdesk")
    if src.exists():
        has_common = (src / "libs" / "hbb_common" / "src" / "config.rs").exists()
        results.append(
            (
                "源码 libs/hbb_common",
                has_common,
                (
                    "已就绪"
                    if has_common
                    else "子模块缺失！需 git submodule update --init --recursive"
                ),
                "ok" if has_common else "critical",
            )
        )
        cfg = src / "libs" / "hbb_common" / "src" / "config.rs"
        if cfg.exists():
            txt = cfg.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'RS_PUB_KEY: &str = "([^"]+)"', txt)
            key = m.group(1) if m else "?"
            official = key.startswith("OeVuKk5nlHiX")
            results.append(
                (
                    "定制已注入",
                    not official,
                    ("官方默认 key" if official else key[:24] + "..."),
                    "warn" if official else "ok",
                )
            )

    # ---------- 输出 ----------
    print()
    critical_missing = []
    for name, ok, detail, level in results:
        mark = GREEN(f"[{OK}]") if ok else (RED(f"[{BAD}]") if level == "critical" else YELL(f"[{WARN}]"))
        print(f"  {mark} {name:<24} {detail}")
        if not ok and level == "critical":
            critical_missing.append(name)

    print()
    print(DIM("=" * 60))

    if critical_missing:
        print(RED(f"缺少 {len(critical_missing)} 个必需组件，现在构建会失败："))
        for n in critical_missing:
            print(f"    - {n}")
        print()
        print(BOLD("如何补齐："))
        print(
            """
  1) MSVC / Windows SDK  —— 必须由你自己安装（需要管理员权限）
     下载 Visual Studio Build Tools 2022：
       https://visualstudio.microsoft.com/zh-hans/downloads/#build-tools-for-visual-studio-2022
     安装时务必勾选工作负荷：
       ☑ 使用 C++ 的桌面开发
     它会同时带上 Windows 10/11 SDK。安装约需 20-40 分钟，装完重启终端。

  2) Rust 1.75      运行本目录的 install-rust.bat，或：
       winget install Rustlang.Rustup   rustup default 1.75

  3) Flutter 3.24.5  运行 apply-rustdesk-engine.bat（含替换官方自研 engine）

  4) vcpkg          运行 install-vcpkg-deps.bat（编译 C++ 依赖，耗时最长）

  5) LLVM 15.0.6    winget install LLVM.LLVM --version 15.0.6
"""
        )
        sys.exit(1)
    else:
        print(GREEN("环境就绪，可以尝试构建："))
        print("    cd D:\\Git\\rustdesk")
        print("    python .\\build.py --portable --flutter --skip-portable-pack --hwcodec")
        print()


if __name__ == "__main__":
    main()
