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


def _vs_roots():
    """候选 Visual Studio 安装根目录。

    不能只写死 C:\\Program Files\\Microsoft Visual Studio\\2022：
      1) 用户完全可能把 VS 装到 D 盘等其他盘符；
      2) 版本号也不止 2022/2019，新版本（如 VS 18 = "2026"）目录名完全不同；
      3) 还得考虑 Isolated/Insiders 这类发行通道目录（我这儿就是 18\\Insiders）。
    所以这里做「盘符 × 程序目录 × 任意 VS 版本 × 任意发行版」的笛卡尔积扫描。
    """
    roots = []
    for drive_env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        d = os.environ.get(drive_env)
        if d:
            roots.append(Path(d) / "Microsoft Visual Studio")
    for letter in "CDEFGHIJ":
        for middle in ("Program Files", "Program Files (x86)"):
            roots.append(Path(f"{letter}:\\{middle}\\Microsoft Visual Studio"))
    # 去重且保序
    seen, out = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def find_msvc():
    """定位 Visual Studio Build Tools / MSVC 工具链"""
    for root in _vs_roots():
        if not root.exists():
            continue
        # 版本层（2022 / 18 / ...）→ 发行层（BuildTools / Community / Insiders / ...）
        for version in sorted(root.iterdir(), reverse=True):
            if not version.is_dir():
                continue
            for edition in sorted(version.iterdir()):
                if not edition.is_dir():
                    continue
                tools = edition / "VC" / "Tools" / "MSVC"
                vcv = edition / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
                if not vcv.exists():
                    continue
                toolset = None
                if tools.is_dir():
                    vers = sorted(tools.iterdir(), reverse=True)
                    toolset = vers[0].name if vers else None
                return True, str(vcv), f"{version.name}/{edition.name} (MSVC {toolset})"
    return False, None, None


def find_winsdk():
    """定位 Windows SDK，同样要覆盖非 C 盘安装。"""
    bases = []
    for letter in "CDEFGHIJ":
        bases.append(Path(f"{letter}:\\Program Files (x86)\\Windows Kits\\10\\bin"))
        bases.append(Path(f"{letter}:\\Windows Kits\\10\\bin"))
    for drive_env in ("ProgramFiles(x86)", "ProgramW6432"):
        d = os.environ.get(drive_env)
        if d:
            bases.append(Path(d) / "Windows Kits" / "10" / "bin")
    for base in bases:
        if not base.exists():
            continue
        vers = sorted(
            [d.name for d in base.iterdir() if re.match(r"^10\.", d.name)], reverse=True
        )
        if vers:
            return True, vers[0] if vers else None
    return False, None


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
    # flutter 常常装好了却没进 PATH（典型：直接解压到 C:\flutter），
    # 只查 which 会误判成未安装。先探测常见安装位置，再回退 PATH。
    flutter_bat = None
    for cand in ["C:\\flutter", "C:\\src\\flutter", "D:\\flutter", "D:\\src\\flutter"]:
        p = Path(cand) / "bin" / "flutter.bat"
        if p.exists():
            flutter_bat = str(p)
            break
    if flutter_bat is None:
        found = shutil.which("flutter")
        flutter_bat = found if found else None

    ver = "?"
    if flutter_bat:
        vfile = Path(flutter_bat).parent.parent / "version"
        if vfile.exists():
            ver = vfile.read_text(encoding="utf-8").strip()
        else:
            rc, out = run(f'"{flutter_bat}" --version')
            m = re.search(r"Flutter (\S+)", out)
            ver = m.group(1) if m else "?"

    if flutter_bat:
        ok = ver == "3.24.5"
        on_path = shutil.which("flutter") is not None
        note = ver + ("" if ok else "  (官方 CI 要求 3.24.5)")
        if not on_path:
            note += f"  [未进 PATH: {flutter_bat}]"
        results.append(
            (
                "Flutter",
                ok,
                note,
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
    llvm = None
    for cand in [
        r"C:\Program Files\LLVM\bin\clang.exe",
        r"D:\Program Files\LLVM\bin\clang.exe",
        r"C:\LLVM\bin\clang.exe",
    ]:
        if Path(cand).exists():
            llvm = Path(cand)
            break
    if llvm is None:
        found = shutil.which("clang")
        if found:
            llvm = Path(found)
    if llvm and llvm.exists():
        rc, out = run(f'"{llvm}" --version')
        m = re.search(r"version (\S+)", out)
        ver = m.group(1) if m else "?"
        ok = ver.startswith("15.")
        results.append(
            ("LLVM/Clang", ok, ver + ("" if ok else "  (需 15.0.6)"), "ok" if ok else "warn")
        )
    else:
        results.append(("LLVM/Clang", False, "未安装（需 15.0.6）", "critical"))

    # ---------- 构建辅助 ----------
    for exe, name in [("cmake", "CMake"), ("7z", "7-Zip"), ("curl", "curl")]:
        p = shutil.which(exe)
        results.append((name, bool(p), p or "未找到", "warn" if not p else "ok"))

    # ---------- 源码 ----------
    # 上游源码已扁平化到仓库根（与 build/ 同级），路径从脚本位置推导，
    # 不再硬编码盘符，仓库放哪都能体检。
    src = Path(__file__).resolve().parent.parent
    if src.exists():
        has_common = (src / "libs" / "hbb_common" / "src" / "config.rs").exists()
        results.append(
            (
                "源码 libs/hbb_common",
                has_common,
                (
                    "已就绪"
                    if has_common
                    else "未就绪：libs/hbb_common/src/config.rs 不存在"
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
        print("    cd " + str(src))
        print("    python .\\build.py --portable --flutter --skip-portable-pack --hwcodec")
        print()
        print("  （或直接双击 build\\build-local.bat）")
        print()


if __name__ == "__main__":
    main()
