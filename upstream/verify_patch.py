#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证 upstream/custom.patch 的可重放性 —— 上游升级前的安全网。

做什么
------
全程只读，绝不改动 D:\\Git\\rustdesk 工作区：
  1. 用 `git checkout-index` 从上游对象库导出一份"干净"的 v1.5.1 代码到临时目录
     （主仓一次 + libs/hbb_common 子模块一次，后者落到 libs/hbb_common/）
  2. 在干净副本上 `git apply` 定制补丁
  3. 把结果和 client/ 下的同名文件逐文件比对（归一化 CRLF 后比对字节）
     全部一致 => 补丁能完整重建当前定制状态，上游升级后可安全重放
     （客户端已迁到 client/，CUSTOMIZED_FILES 仍是上游相对路径，
       仓库侧查找统一拼 CLIENT 前缀，见下面的 CLIENT 常量说明）

为什么这样写
------------
两个踩过的坑，脚本里都做了防御：

坑 1 · git apply 静默跳过（最危险）
    临时目录若位于另一个 git 仓库内（例如 D:\\Git\\wedesk\\.verify\\clean），
    git 会向上找到 wedesk 的 .git，把补丁路径按 wedesk 仓库根解析，
    恰好匹配到 rustdesk/ 下已定制的文件 => 判定"改动已应用" =>
    输出 "Skipped patch ..." 后**仍然返回 0**。看起来成功，其实一个字节都没改。
    防御：在导出目录执行 `git init` 使其成为独立仓库根，
    并显式扫描输出里的 "Skipped patch"，出现即判失败。

坑 2 · CRLF/LF 差异
    git 内部存 LF，工作区文件可能是 CRLF。直接比字节会全量误报。
    防御：比对前把 \\r\\n 归一化为 \\n。

用法
----
    python upstream/verify_patch.py [--upstream D:/Git/rustdesk]
退出码 0 = 通过，1 = 补丁无法完整重建。
"""

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile

GIT = r"C:\Program Files\Git\cmd\git.exe"
WEDESK = pathlib.Path(__file__).resolve().parent.parent
# 客户端 vendored 源码所在：重构后 client/ 下才是 crate 根。
# CUSTOMIZED_FILES 里的路径【保持上游相对】（与 custom.patch 里的路径一致），
# 只有"往本仓库取文件"时才需要拼上这个前缀。两边混用会同时踩两个坑：
#   仓库侧不加前缀 -> 所有文件都"不存在"，脚本静默跳过全部比对
#   上游侧加了前缀 -> git apply 找不到路径，输出 Skipped 后仍返回 0
CLIENT = WEDESK / "client"
PATCH = WEDESK / "upstream" / "custom.patch"
# 这份清单 = customize.py 的规则覆盖文件 ∩ 需要第三方 Git 初检的文件。
#
# 判定标准：只要 customize.py 能完整重建，就不必进补丁；但为了让 custom.patch
# 成为"从干净上游一键回到当前状态"的完整凭据，凡是被注入过的文件都应登记。
# 漏登的后果不是构建失败 —— 而是某天升级上游时，这个文件被整包覆盖回原始值，
# 且因为 verify_patch 也不看它，没人会发现。
#
# 新增/删除定制规则时同步本清单，并跑 `python upstream/verify_patch.py --regen`。
CUSTOMIZED_FILES = [
    "Cargo.toml",
    "src/common.rs",
    "src/client.rs",
    "libs/hbb_common/src/config.rs",
    "flutter/lib/common.dart",
    "flutter/lib/desktop/pages/desktop_setting_page.dart",
    "flutter/lib/desktop/pages/install_page.dart",
    "flutter/windows/runner/Runner.rc",
    "flutter/android/app/src/main/res/values/strings.xml",
    "res/msi/Package/Language/Package.en-us.wxl",
    "res/msi/preprocess.py",
    "res/rustdesk.desktop",
    "res/rustdesk-link.desktop",
]


def run(args, cwd, check=True):
    r = subprocess.run(args, cwd=str(cwd), capture_output=True)
    if check and r.returncode != 0:
        sys.exit(
            f"命令失败: {' '.join(args)}\n"
            + r.stderr.decode("utf-8", "replace")[:600]
        )
    return r


def export_clean(git_dir: pathlib.Path, dest: pathlib.Path) -> None:
    """把某个 git 仓库 HEAD 的工作树导出到 dest，不触碰原工作区。"""
    dest.mkdir(parents=True, exist_ok=True)
    run([GIT, "checkout-index", "-a", "-f", f"--prefix={dest.as_posix()}/"], git_dir)


def norm(data: bytes) -> bytes:
    """归一化换行，避免 CRLF/LF 造成的假差异。"""
    return data.replace(b"\r\n", b"\n")


def regen(upstream: pathlib.Path) -> int:
    """手工改了 CUSTOMIZED_FILES 里的源码后，重新生成 custom.patch。

    直接 `git diff` 会有两个坑，这里都处理掉了：
      1. 工作区是 CRLF（受 .gitattributes 的 `* text=auto` 影响），
         而 checkout-index 导出的是 LF。不归一化的话每一行都算改动，
         生成出来的补丁是整文件替换，等于没有参考价值。
      2. 需要一个临时 git 仓库来做 diff，路径才是相对的；
         同样要先 `git init` 防止 git 向上找 wedesk 的 .git。
    """
    clean = pathlib.Path(tempfile.gettempdir()) / "wedesk-regen-clean"
    shutil.rmtree(clean, ignore_errors=True)

    print(f"[1/4] 导出干净上游代码 -> {clean}")
    export_clean(upstream, clean)
    export_clean(upstream / "libs" / "hbb_common", clean / "libs" / "hbb_common")

    run([GIT, "init", "-q", "."], clean)
    run([GIT, "config", "core.autocrlf", "false"], clean)

    print("[2/4] 建立基线 index")
    run([GIT, "add", "-A", "."], clean)

    print(f"[3/4] 用当前仓库的 {len(CUSTOMIZED_FILES)} 个文件覆盖（换行统一 LF）")
    staged = []
    for rel in CUSTOMIZED_FILES:
        # 仓库侧：rel 是上游相对路径，要拼 client/ 才是本仓库里的真实位置。
        # 干净侧：clean 是上游树的副本，用 rel 原样（补丁路径也是这个形式）。
        src_file = CLIENT / rel
        if not src_file.exists():
            print(f"      ! 跳过（{CLIENT / rel} 不存在）: {rel}")
            continue
        dst = clean / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(norm(src_file.read_bytes()))
        staged.append(rel)

    print("[4/4] 生成 diff")
    r = subprocess.run(
        [GIT, "diff", "--no-ext-diff", "--no-color", "--"] + staged,
        cwd=str(clean),
        capture_output=True,
    )
    patch = r.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    if not patch.strip():
        print("      ! 没有任何差异，补丁未改动")
        shutil.rmtree(clean, ignore_errors=True)
        return 1

    PATCH.write_text(patch, encoding="utf-8", newline="\n")
    shutil.rmtree(clean, ignore_errors=True)
    hunks = patch.count("\n@@")
    print(f"      已写入 {PATCH}（{len(staged)} 个文件，{hunks} 个 hunk）")
    print()
    print("下一步务必验证：python upstream/verify_patch.py")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", default="D:/Git/rustdesk",
                    help="仍保留 .git 的上游源码目录（用于导出干净基线）")
    ap.add_argument("--regen", action="store_true",
                    help="重新生成 custom.patch（手工改了源码后必跑）")
    args = ap.parse_args()

    upstream = pathlib.Path(args.upstream)
    if not (upstream / ".git").exists():
        sys.exit(f"{upstream} 不是 git 仓库，无法导出干净基线")

    if args.regen:
        return regen(upstream)

    if not PATCH.exists():
        sys.exit(f"补丁不存在: {PATCH}")

    clean = pathlib.Path(tempfile.gettempdir()) / "wedesk-verify-clean"
    if clean.exists():
        shutil.rmtree(clean, ignore_errors=True)

    print(f"[1/4] 导出干净上游代码 -> {clean}")
    export_clean(upstream, clean)
    export_clean(upstream / "libs" / "hbb_common", clean / "libs" / "hbb_common")

    # 坑 1 防御：让它成为独立仓库根，否则 git apply 会向上找到 wedesk 的 .git
    run([GIT, "init", "-q", "."], clean)
    run([GIT, "config", "core.autocrlf", "false"], clean)

    print("[2/4] 预检补丁")
    r = run([GIT, "apply", "--check", "-v", str(PATCH)], clean, check=False)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    if r.returncode != 0 or "Skipped patch" in out:
        print("      预检失败 ✗"); print(out[:800]); return 1
    print("      预检通过 ✓")

    print("[3/4] 应用补丁")
    r = run([GIT, "apply", "-v", str(PATCH)], clean, check=False)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    if r.returncode != 0 or "Skipped patch" in out:
        print("      应用失败 ✗")
        print("      提示：git apply 在路径误判时会输出 Skipped 后静默返回 0，")
        print("            不要只看退出码。")
        print(out[:800]); return 1
    applied = out.count("Applied patch")
    print(f"      应用成功 ✓（{applied} 个文件）")

    print(f"[4/4] 与 {CLIENT} 比对 {len(CUSTOMIZED_FILES)} 个文件")
    ok = True
    for rel in CUSTOMIZED_FILES:
        a, b = clean / rel, CLIENT / rel
        if not (a.exists() and b.exists()):
            print(f"      ✗ {rel} (文件缺失: {b})"); ok = False; continue
        same = norm(a.read_bytes()) == norm(b.read_bytes())
        ok &= same
        print(f"      {'✓' if same else '✗'} {rel}")

    shutil.rmtree(clean, ignore_errors=True)
    print()
    print("结论:", "补丁可完整重建当前定制状态 ✓" if ok else "存在不可重建的差异 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
