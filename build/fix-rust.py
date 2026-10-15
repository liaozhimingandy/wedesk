# -*- coding: utf-8 -*-
"""
修复被中断的 Rust 工具链。

背景：此前并发执行 rustup + 中途 kill，导致 C:\\Users\\<user>\\.cargo\\bin 下的
转发壳被截断为 0 字节，且 toolchain 目录里只剩一个残缺的 cargo.exe。

用法：
    python build/fix-rust.py            # 自动清理 + 重装
    python build/fix-rust.py --check    # 只体检，不改动

！！严禁中途 kill 本进程！！rustup 下载期间被杀会再次损坏转发壳。
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# RustDesk v1.5.1 锁定的 Rust 版本，见上游 rust-toolchain.toml / CI
DEFAULT_TOOLCHAIN = "1.75-x86_64-pc-windows-msvc"


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def scan_cargo_bin(cargo_bin: Path):
    """分类转发壳：完好 / 0字节损坏 / rustup 本体"""
    if not cargo_bin.is_dir():
        return [], [], None
    broken, ok, rustup = [], [], None
    for p in sorted(cargo_bin.iterdir()):
        if not p.is_file():
            continue
        size = p.stat().st_size
        if p.name.lower() == "rustup.exe":
            rustup = p
        elif size == 0:
            broken.append(p)
        else:
            ok.append((p, size))
    return broken, ok, rustup


def scan_toolchains(rustup_home: Path):
    root = rustup_home / "toolchains"
    if not root.is_dir():
        return []
    info = []
    for t in sorted(root.iterdir()):
        if not t.is_dir():
            continue
        bins = sorted(p.name for p in (t / "bin").glob("*.exe")) if (t / "bin").is_dir() else []
        info.append((t.name, bins))
    return info


def check(args):
    cargo_bin = Path(os.path.expanduser("~")) / ".cargo" / "bin"
    rustup_home = Path(
        os.environ.get("RUSTUP_HOME") or (Path(os.path.expanduser("~")) / ".rustup")
    )

    print("Rust 工具链体检")
    print("=" * 60)
    print(f"CARGO_HOME/bin : {cargo_bin}")
    print(f"RUSTUP_HOME    : {rustup_home}")
    print()

    broken, ok, rustup = scan_cargo_bin(cargo_bin)
    print(f"[转发壳] 完好 {len(ok)} 个，损坏(0字节) {len(broken)} 个")
    for p, size in ok:
        print(f"  [+] {p.name:<24} {human(size)}")
    for p in broken:
        print(f"  [x] {p.name:<24} 0B (损坏)")
    print()

    ts = scan_toolchains(rustup_home)
    if not ts:
        print("[toolchain] 未安装任何 toolchain")
    for name, bins in ts:
        need = {"rustc.exe", "cargo.exe", "rustfmt.exe", "rust-std-x86_64-pc-windows-msvc"}
        missing = need - set(bins)
        flag = "[+]" if not missing else "[x]"
        print(f"{flag} toolchain {name}: {len(bins)} 个可执行文件")
        for b in bins:
            print(f"      - {b}")
        if missing:
            print(f"      !! 缺少关键组件: {', '.join(sorted(missing))}")
    print()
    return broken, ok, rustup, ts


def repair(args):
    toolchain = args.toolchain
    cargo_bin = Path(os.path.expanduser("~")) / ".cargo" / "bin"
    rustup_home = Path(
        os.environ.get("RUSTUP_HOME") or (Path(os.path.expanduser("~")) / ".rustup")
    )

    broken, ok, rustup, ts = check(args)
    if rustup is None:
        print("!! 找不到 rustup.exe，无法自我修复。")
        print("   请重新安装 rustup: https://rustup.rs/ 或 winget install Rustlang.Rustup")
        return 1

    # 1) 删除 0 字节转发壳
    if broken:
        print(f"[1/4] 删除 {len(broken)} 个损坏转发壳 ...")
        for p in broken:
            # 已是 0 字节，无内容可丢；但仍转到回收站更稳妥的做法是重命名备份
            bak = p.with_suffix(p.suffix + ".broken")
            try:
                p.rename(bak)
                bak.unlink(missing_ok=True)
            except Exception as e:
                print(f"      !! 删除失败 {p.name}: {e}")
        print("      完成")
    else:
        print("[1/4] 无需清理转发壳")

    # 2) 修复残缺 toolchain
    #    不要用 shutil.rmtree 删 toolchain 目录!!
    #    两个原因：
    #      a) 目录上千文件，会被批量删除保护拦截（且可能触发需要人工确认）；
    #      b) rustup 自己带损伤恢复能力，`install --force` 会打印
    #         "recovering from a partially installed toolchain" 并自行清理，比外部删目录安全。
    tc_dir = rustup_home / "toolchains" / toolchain
    if tc_dir.is_dir():
        bins = {b.name for b in (tc_dir / "bin").glob("*.exe")} if (tc_dir / "bin").is_dir() else set()
        complete = {"rustc.exe", "cargo.exe"} <= bins
        if not complete:
            print(f"[2/4] toolchain {toolchain} 残缺（{len(bins)} 个文件），交给 rustup 自行恢复")
        else:
            print(f"[2/4] toolchain {toolchain} 完整，保留")

    # 3) 重装 toolchain（rustup 会自动重建转发壳）
    print(f"[3/4] 安装 {toolchain} ...（下载约 200-400MB，请勿中断）")
    cmd = [str(rustup), "toolchain", "install", toolchain,
           "--profile", args.profile, "--force", "--no-self-update"]
    print(f"      $ {' '.join(cmd)}")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"      !! rustup 返回 {r.returncode}")
        return r.returncode
    print("      完成")

    # 4) 设为默认并验证
    print("[4/4] 设为默认并重建转发壳 ...")
    subprocess.run([str(rustup), "default", toolchain])

    # rustup 正常情况下会把 ~/.cargo/bin 下的转发壳做成 rustup.exe 的硬链接。
    # 但在受限/沙箱环境里建硬链接可能失败，导致安装成功却没有可用命令。
    # 这里兜底：按 toolchain 里真实存在的 exe 补拷贝一份同名代理
    # （rustup.exe 靠 argv[0] 判断自己该扮演谁，改名即生效）。
    made = []
    tc_bin = rustup_home / "toolchains" / toolchain / "bin"
    if tc_bin.is_dir():
        for p in sorted(tc_bin.glob("*.exe")):
            dst = cargo_bin / p.name
            if dst.exists() and dst.stat().st_size > 0:
                continue
            shutil.copy2(rustup, dst)
            made.append(p.name)
    if made:
        print(f"      补建转发壳 {len(made)} 个: {', '.join(made)}")

    ok_count = 0
    for exe in ("rustc", "cargo", "rustfmt"):
        p = cargo_bin / f"{exe}.exe"
        if p.is_file() and p.stat().st_size > 0:
            rr = subprocess.run([str(p), "-V"], capture_output=True, text=True)
            out = (rr.stdout or rr.stderr).strip().splitlines()
            print(f"      [+] {exe}: {out[0] if out else '(无输出)'}")
            ok_count += 1
        else:
            print(f"      [x] {exe}: 缺失")

    print()
    print("=" * 60)
    if ok_count == 3:
        print("修复完成。请重开终端，执行验证：")
        print("    rustc -V    cargo -V    rustfmt -V")
    else:
        print("!! 仍有组件不可用，请手动检查上面的输出。")
    return 0 if ok_count == 3 else 1


def main():
    ap = argparse.ArgumentParser(description="修复被中断的 Rust 工具链")
    ap.add_argument("--check", action="store_true", help="只体检，不改动")
    ap.add_argument("--toolchain", default=DEFAULT_TOOLCHAIN, help=f"目标 toolchain（默认 {DEFAULT_TOOLCHAIN}）")
    ap.add_argument("--profile", default="default", choices=["minimal", "default", "complete"],
                    help="rustup profile（默认 default，含 rustfmt/clippy）")
    args = ap.parse_args()
    if args.check:
        check(args)
        return 0
    return repair(args)


if __name__ == "__main__":
    sys.exit(main())
