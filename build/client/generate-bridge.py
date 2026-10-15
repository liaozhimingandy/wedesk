# -*- coding: utf-8 -*-
"""
生成 flutter-rust-bridge 胶水代码（本地构建第 0 步）。

为什么必须有这一步：
    src/bridge_generated.rs 与 flutter/lib/generated_bridge.dart 是代码生成器
    的产物，被 .gitignore 排除，仓库里没有。少了它们：
        - cargo build   -> error[E0583]: file not found for module `bridge_generated`
        - flutter build -> Target of URI hasn't been generated: 'generated_bridge.dart'
    所以本地必须自己生成一遍，和 CI 里的 generate-bridge job 完全一致。

用法：
    python build/client/generate-bridge.py              # 缺了才生成
    python build/client/generate-bridge.py --force   # 改了 src/flutter_ffi.rs 后强制重生成
    python build/client/generate-bridge.py --check      # 只看是否已就绪

注意：flutter_rust_bridge_codegen 是从源码 cargo install 的，首次约 5-15 分钟，
中途别 kill —— 半途中断会留下损坏的二进制，得删掉 build/.tools/frb 重来。
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

FRB_VERSION = "1.80.1"
CARGO_EXPAND_VERSION = "1.0.95"
TOOLS_DIR = HERE / ".tools" / "frb"
FRB_BIN = TOOLS_DIR / "bin" / (
    "flutter_rust_bridge_codegen.exe" if os.name == "nt" else "flutter_rust_bridge_codegen"
)

OUTPUTS = [
    "src/bridge_generated.rs",
    "src/bridge_generated.io.rs",
    "flutter/lib/generated_bridge.dart",
    "flutter/lib/generated_bridge.freezed.dart",
    "flutter/macos/Runner/bridge_generated.h",
    "flutter/ios/Runner/bridge_generated.h",
]

# 给 cargo install 指定固定的 target 目录。
# 默认行为是每次建一个临时目录、跑完就删——意味着中途失败一次，
# 前面 20 分钟编译出来的依赖全部作废，下次从零再来。
# codegen 这套依赖里有 syntect/clap 这类重活，重编代价很高。
BUILD_TARGET = HERE / ".tools" / "target"


def run(cmd, cwd=None, check=True):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None)
    if check and r.returncode != 0:
        sys.exit(f"\n[x] 命令失败（退出码 {r.returncode}）")
    return r.returncode


def flutter_version(root: Path):
    v = root / "flutter" / "version"
    if v.is_file():
        return v.read_text(encoding="utf-8").strip().split("/")[0]
    return None


def missing_outputs(root: Path):
    return [o for o in OUTPUTS if not (root / o).is_file()]


def install_codegen(force_reinstall: bool):
    if FRB_BIN.is_file() and FRB_BIN.stat().st_size > 0 and not force_reinstall:
        print(f"[=] codegen 已存在，跳过安装: {FRB_BIN}")
        return

    if not shutil.which("cargo"):
        sys.exit("[x] 找不到 cargo。请先安装 Rust 1.75（winget install Rustlang.Rustup）")

    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(BUILD_TARGET)
    print(f"[=] CARGO_TARGET_DIR = {BUILD_TARGET}（中断后可续编）")

    print(f"[1/3] 安装 cargo-expand {CARGO_EXPAND_VERSION}")
    rc = subprocess.run(
        ["cargo", "install", "cargo-expand",
         "--version", CARGO_EXPAND_VERSION, "--locked", "--root", str(TOOLS_DIR)],
        env=env,
    ).returncode
    if rc != 0:
        sys.exit(f"\n[x] cargo install cargo-expand 失败（退出码 {rc}）")

    print(f"[2/3] 安装 flutter_rust_bridge_codegen {FRB_VERSION}")
    rc = subprocess.run(
        ["cargo", "install", "flutter_rust_bridge_codegen",
         "--version", FRB_VERSION, "--features", "uuid", "--locked",
         "--root", str(TOOLS_DIR)],
        env=env,
    ).returncode
    if rc != 0:
        sys.exit(f"\n[x] cargo install flutter_rust_bridge_codegen 失败（退出码 {rc}）")

    if not FRB_BIN.is_file():
        sys.exit(f"[x] 安装完成但找不到 {FRB_BIN}")
    print("[3/3] codegen 就绪")


def main():
    ap = argparse.ArgumentParser(description="生成本地构建所需的 flutter-rust-bridge 代码")
    ap.add_argument("--root", default=None, help="仓库根目录（默认由本脚本位置向上两级推断）")
    ap.add_argument("--force", action="store_true", help="忽略已有产物，强制重新生成")
    ap.add_argument("--reinstall", action="store_true", help="强制重装 codegen 二进制")
    ap.add_argument("--check", action="store_true", help="只检查是否已就绪")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else ROOT
    print(f"仓库根目录: {root}\n")

    miss = missing_outputs(root)
    if args.check:
        if miss:
            print("[x] bridge 产物缺失：")
            for m in miss:
                print(f"      - {m}")
            print("\n  执行 python build/client/generate-bridge.py 生成")
            return 1
        print("[OK] bridge 产物齐全")
        return 0

    if not miss and not args.force:
        print("[OK] bridge 产物已存在，无需生成。")
        print("     改了 src/flutter_ffi.rs 后请加 --force 重新生成。")
        return 0

    if miss:
        print("缺失产物：")
        for m in miss:
            print(f"  - {m}")
        print()

    install_codegen(args.reinstall)

    flutter = shutil.which("flutter")
    if not flutter:
        sys.exit("[x] 找不到 flutter。请把它加入 PATH，或先运行 apply-rustdesk-engine.bat")

    # extended_text 14 需要较新的 Dart；Flutter 3.22.x 的 Dart 解析不了，
    # 临时降到 13 让 pub get 能过。只在 3.22 下需要，3.24 保持原样。
    ver = flutter_version(root)
    pubspec = root / "flutter" / "pubspec.yaml"
    need_downgrade = bool(ver and ver.startswith("3.22"))
    backup = pubspec.with_suffix(".yaml.rdbridgebak")

    try:
        if need_downgrade:
            print(f"[pub] Flutter {ver} 检测到，临时把 extended_text 降到 13.0.0")
            shutil.copy2(pubspec, backup)
            text = pubspec.read_text(encoding="utf-8")
            pubspec.write_text(
                text.replace("extended_text: 14.0.0", "extended_text: 13.0.0"),
                encoding="utf-8",
            )

        print("[pub] flutter pub get")
        run(["flutter", "pub", "get"], cwd=root / "flutter")

        print("[gen] 运行 flutter_rust_bridge_codegen")
        mac_out = root / "flutter" / "macos" / "Runner" / "bridge_generated.h"
        mac_out.parent.mkdir(parents=True, exist_ok=True)
        run([
            FRB_BIN,
            "--rust-input", "./src/flutter_ffi.rs",
            "--dart-output", "./flutter/lib/generated_bridge.dart",
            "--c-output", str(mac_out),
        ], cwd=root)

        ios_out = root / "flutter" / "ios" / "Runner" / "bridge_generated.h"
        ios_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mac_out, ios_out)
    finally:
        # pubspec 是源码的一部分，无论成功失败都必须还原，
        # 否则会在 git status 里留下莫名其妙的 diff。
        if backup.exists():
            shutil.copy2(backup, pubspec)
            backup.unlink()
            print("[pub] 已还原 pubspec.yaml")

    still = missing_outputs(root)
    if still:
        print("\n[x] 生成结束但以下产物仍缺失：")
        for m in still:
            print(f"      - {m}")
        return 1

    print("\n[OK] bridge 生成完成：")
    for o in OUTPUTS:
        size = (root / o).stat().st_size
        print(f"      {o}  ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
