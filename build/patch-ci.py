#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch-ci.py —— 让 fork 的 RustDesk 在 CI 里自动产出定制客户端

为什么要这样做
--------------
RustDesk 官方的 flutter-build.yml 内部会执行 actions/checkout 拉取源码。
如果我们在外部 job 里先跑 customize.py、再调用官方 workflow，
checkout 会把我们的修改冲掉 —— 定制不会生效。

所以唯一可靠的做法是把定制环节塞进官方 workflow 的内部，放在
  「checkout 完成」之后、「Build rustdesk」之前。
这样既保留了官方验证过的整套复杂流程（自研 Flutter engine、vcpkg、
bridge 生成、硬件编解码 ...），又能在编译前完成品牌与服务器注入。

本脚本做三件事（幂等，可重复执行）
---------------------------------
  1. 把 customize.py 与 custom.toml 复制进仓库的 .github/ 下
  2. 在 flutter-build.yml 的 Windows 构建 job 中插入定制步骤
  3. 创建 custom-build.yml —— 一个可手动触发的便捷入口

用法
----
    python patch-ci.py --source <仓库根目录> [--config build/custom.toml]

  上游源码已扁平化到仓库根，--source 直接指向仓库根即可
  （在本仓库里就是 build\\patch-ci.py --source ..）。

之后：git add . && git commit && git push，去 GitHub Actions 页面手动触发。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

GREEN, RED, YELLOW, BLUE, RESET = (
    ("\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[0m")
    if (sys.platform != "win32" or __import__("os").environ.get("WT_SESSION"))
    else ("", "", "", "", "")
)

MARKER_BEGIN = "      # >>> rd-suite custom: BEGIN <<<"
MARKER_END = "      # >>> rd-suite custom: END <<<"

# 插入点的锚点：Windows job 的构建步骤。定制必须在编译发生前完成。
BUILD_ANCHOR = "      - name: Build rustdesk"

CUSTOM_STEP = """      # >>> rd-suite custom: BEGIN <<<
      # 由 patch-ci.py 自动插入，勿手动编辑此区块之间的内容。
      # 把品牌信息与自建服务器注入源码。此时源码已完成 checkout，
      # 后续 Build rustdesk 步骤会编译到的就是定制后的代码。
      - name: Apply custom branding and self-hosted server
        shell: bash
        run: |
          echo "== 应用定制配置 =="
          python3 .github/scripts/customize.py --source . --config .github/custom.toml
          echo "== 校验关键定制是否生效 =="
          grep -n 'RENDEZVOUS_SERVERS\\|RS_PUB_KEY' libs/hbb_common/src/config.rs || true
      # >>> rd-suite custom: END <<<
"""

CUSTOM_BUILD_TRIGGER = """# =============================================================================
# 定制客户端构建触发器（由 patch-ci.py 生成）
# =============================================================================
# 用法: GitHub 仓库页面 -> Actions -> "Custom Client Build" -> Run workflow
#
# 说明: 这会调用官方 flutter-build.yml 的全部 job（多平台）。
#       若只想产出 Windows 版本，可编辑 .github/workflows/flutter-build.yml，
#       把 matrix 里不需要的 job 注释掉，能显著缩短构建时间。
# =============================================================================

name: Custom Client Build

on:
  workflow_dispatch:
    inputs:
      tag:
        description: "产物发布到的 Tag"
        required: false
        default: "custom"
        type: string

jobs:
  call-flutter-build:
    uses: ./.github/workflows/flutter-build.yml
    secrets: inherit
    with:
      upload-artifact: true
      upload-tag: ${{ inputs.tag }}
"""


def die(msg: str):
    print(f"{RED}[x]{RESET} {msg}")
    sys.exit(1)


def ensure_workflow_dispatch(text: str) -> "tuple[str, bool]":
    """给 flutter-build.yml 补上 workflow_dispatch，否则没法在页面手动触发"""
    if "workflow_dispatch" in text:
        return text, False
    # 原文件开头是
    #   on:
    #     workflow_call:
    old = "on:\n  workflow_call:\n"
    new = "on:\n  workflow_dispatch:   # 由 patch-ci.py 添加：允许手动触发定制构建\n  workflow_call:\n"
    if old not in text:
        # 布局有变，退化为替换首个 'on:' 下的内容
        text = re.sub(r"^on:\n", new, text, count=1, flags=re.M)
        return text, True
    return text.replace(old, new, 1), True


def insert_custom_step(text: str) -> "tuple[str, bool]":
    """在 Windows job 的 Build rustdesk 之前插入定制步骤"""
    if MARKER_BEGIN in text:
        return text, False  # 已注入，保持幂等
    if BUILD_ANCHOR not in text:
        return text, None  # 找不到锚点，交由调用方报错
    return text.replace(BUILD_ANCHOR, CUSTOM_STEP + BUILD_ANCHOR, 1), True


def main():
    ap = argparse.ArgumentParser(description="为 fork 的 RustDesk 注入 CI 定制步骤")
    ap.add_argument("--source", required=True, help="仓库根目录（源码已扁平化在此）")
    ap.add_argument(
        "--config", default=None, help="custom.toml 路径，默认与本脚本同级目录"
    )
    ap.add_argument("--undo", action="store_true", help="移除注入的定制步骤")
    args = ap.parse_args()

    root = Path(args.source).resolve()
    if not root.is_dir():
        die(f"源码目录不存在: {root}")

    wf = root / ".github" / "workflows" / "flutter-build.yml"
    if not wf.is_file():
        die(f"未找到 {wf}\n    请确认传入的是 rustdesk 源码根目录（需已 --recursive 拉取 submodules）")

    here = Path(__file__).resolve().parent
    cfg_src = Path(args.config).resolve() if args.config else (here / "custom.toml")
    py_src = here / "customize.py"

    if not py_src.is_file():
        die(f"未找到 {py_src}")
    if not cfg_src.is_file():
        die(f"未找到配置文件 {cfg_src}")

    scripts_dir = root / ".github" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(py_src, scripts_dir / "customize.py")
    print(f"{GREEN}✓{RESET} 已部署定制脚本 -> .github/scripts/customize.py")

    shutil.copy2(cfg_src, root / ".github" / "custom.toml")
    print(f"{GREEN}✓{RESET} 已部署定制配置 -> .github/custom.toml")

    text = wf.read_text(encoding="utf-8")

    if args.undo:
        text = re.sub(
            re.escape(MARKER_BEGIN) + r".*?" + re.escape(MARKER_END) + r"\n",
            "",
            text,
            flags=re.S,
        )
        wf.write_text(text, encoding="utf-8")
        trig = root / ".github" / "workflows" / "custom-build.yml"
        if trig.exists():
            trig.unlink()
        print(f"{GREEN}✓{RESET} 已移除注入内容")
        return

    text, changed_dispatch = ensure_workflow_dispatch(text)
    text, ok = insert_custom_step(text)

    if ok is None:
        die(
            "在 flutter-build.yml 中找不到锚点 '      - name: Build rustdesk'。\n"
            "    上游可能调整了步骤名称，请手动在 Windows job 的编译步骤前插入：\n"
            f"{CUSTOM_STEP}"
        )

    if ok:
        print(f"{GREEN}✓{RESET} 已在 Windows 构建流程中插入定制步骤")
    else:
        print(f"{YELLOW}·{RESET} 定制步骤已存在，跳过（幂等）")
    if changed_dispatch:
        print(f"{GREEN}✓{RESET} 已添加 workflow_dispatch 手动触发入口")
    else:
        print(f"{YELLOW}·{RESET} workflow_dispatch 已存在，跳过")

    wf.write_text(text, encoding="utf-8")

    trig = root / ".github" / "workflows" / "custom-build.yml"
    trig.write_text(CUSTOM_BUILD_TRIGGER, encoding="utf-8")
    print(f"{GREEN}✓{RESET} 已创建便捷触发器 -> .github/workflows/custom-build.yml")

    print()
    print(f"{BLUE}接下来的操作{RESET}")
    print("    1. 检查 .github/custom.toml 里的服务器地址与公钥是否正确")
    print("    2. (可选) 编辑 flutter-build.yml 注释掉不需要的平台，缩短构建时间")
    print("    3. git add . && git commit -m 'customize client' && git push")
    print("    4. GitHub 仓库页面 -> Actions -> 'Custom Client Build' -> Run workflow")
    print()
    print(
        f"{YELLOW}注意{RESET} 公钥会随 .github/custom.toml 一并提交到仓库。"
        "ed25519 公钥本就无需保密；若仓库是私有仓库则更安全。"
    )


if __name__ == "__main__":
    main()
