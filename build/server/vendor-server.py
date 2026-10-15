# -*- coding: utf-8 -*-
"""把 rustdesk-server 源码以 vendored 方式实体化进本仓库。

为什么不用 git submodule：
    submodule 意味着 clone 之后还要 `git submodule update --init --recursive`，
    一旦忘记就是一堆空目录；更糟的是服务端 Cargo.toml 用 path 依赖 libs/hbb_common，
    空目录会直接编译失败。本项目的一号目标是「clone 下来就能构建」，所以把上游
    源码连同它锁定的 hbb_common 一起快照进来，永远自包含、永远可复现。

用法（只需在追上游新版本时跑一次）：
    python build/server/vendor-server.py [--server-rev REV] [--common-rev REV]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(ROOT, "server", "src")

SERVER_REPO = "https://github.com/rustdesk/rustdesk-server.git"
COMMON_REPO = "https://github.com/rustdesk/hbb_common.git"

# 上游哪些东西要带走、哪些不带走，理由写在注释里。
# src/libs/Cargo.toml/Cargo.lock/build.rs 是编译的必需项；
# db_v2.sqlite3 是 sqlx 宏在编译期校验 SQL 用的数据库（不是运行时数据！），
# 少了它编译时会报 DATABASE_URL 找不到，必须保留。
KEEP = [
    "src",
    "libs",          # submodule 占位，稍后被真实内容覆盖
    "docs",
    "systemd",
    "tests",
    "Cargo.toml",
    "Cargo.lock",
    "build.rs",
    "LICENSE",
    "db_v2.sqlite3",
    ".env",
    ".cargo",
    ".gitattributes",
]

PIN_STATE = os.path.join(ROOT, "build", "server-vendor.json")

GITHUB_API = "https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"


def run(cmd: list[str], **kw) -> str:
    print("+", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        raise SystemExit(f"命令失败（exit {proc.returncode}）：{' '.join(cmd)}")
    return proc.stdout


def resolve_submodule(repo: str, rev: str, path: str) -> str:
    """查询某个 commit 上 submodule 锁定的 commit sha。"""
    url = GITHUB_API.format(repo=repo, path=path, ref=rev)
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    if data.get("type") != "submodule":
        raise RuntimeError(f"{repo}:{path}@{rev} 不是 submodule：{data}")
    return data["sha"]


def export(src_repo: str, rev: str, dest_dir: str,
           keep: list[str] | None = None, local_src: str = "") -> None:
    """导出指定 commit 的内容。

    用 git archive 而不是 copytree：不会带 .git 目录，也不会受本机 core.autocrlf
    影响把 LF 变成 CRLF —— 上游 shell 脚本、.proto 一旦换行被改写，Docker
    里就会出现经典的 `/bin/sh^M: not found`。

    local_src：本机已存在的克隆目录（网络不通时用它离线导出）。
    """
    if not local_src:
        tmp = tempfile.mkdtemp(prefix="wedesk-export-", dir=ROOT + os.sep + "build")
        try:
            run(["git", "clone", "--quiet", "--no-checkout", src_repo, tmp])
            run(["git", "-C", tmp, "checkout", "--quiet", rev])
        except SystemExit:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    else:
        tmp = local_src

    try:
        os.makedirs(dest_dir, exist_ok=True)
        if keep is None:
            # 整包导出
            subprocess.run(["git", "-C", tmp, "archive", rev, "-o", os.path.join(dest_dir, "_tmp.tar")], check=True)
            subprocess.run(["tar", "-xf", os.path.join(dest_dir, "_tmp.tar"), "-C", dest_dir], check=True)
            os.remove(os.path.join(dest_dir, "_tmp.tar"))
            return
        for item in keep:
            src = os.path.join(tmp, item)
            if not os.path.exists(src):
                print(f"  ! 上游不存在 {item}，跳过")
                continue
            dst = os.path.join(dest_dir, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    finally:
        if not local_src:
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-rev", default="873230e35a8805de7fd057e7eb763c46f7a174ef")
    ap.add_argument("--common-rev", default="")  # 留空则从服务端 submodule 指针解析
    ap.add_argument("--local-server", default="", help="本机已有的 rustdesk-server 克隆目录")
    ap.add_argument("--local-common", default="", help="本机已有的 hbb_common 克隆目录")
    ap.add_argument("--force", action="store_true", help="目标目录已存在时直接覆盖，不询问")
    args = ap.parse_args()

    if os.path.isdir(DEST):
        if args.force:
            print(f"--force：删除已存在的 {DEST}")
            shutil.rmtree(DEST)
        else:
            ans = input(f"{DEST} 已存在，删除并重新导出？[y/N] ").strip().lower()
            if ans != "y":
                print("已取消")
                return 1
            shutil.rmtree(DEST)
    os.makedirs(DEST, exist_ok=True)

    common_rev = args.common_rev or resolve_submodule(
        "rustdesk/rustdesk-server", args.server_rev, "libs/hbb_common"
    )
    print(f"rustdesk-server @ {args.server_rev}")
    print(f"hbb_common      @ {common_rev}")

    export(SERVER_REPO, args.server_rev, DEST, keep=KEEP, local_src=args.local_server)
    # libs/ 是 submodule 占位目录（空），导出真实的 hbb_common 覆盖它
    common_dest = os.path.join(DEST, "libs", "hbb_common")
    shutil.rmtree(common_dest, ignore_errors=True)
    export(COMMON_REPO, common_rev, common_dest, local_src=args.local_common)

    os.makedirs(os.path.dirname(PIN_STATE), exist_ok=True)
    with open(PIN_STATE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "rustdesk-server": args.server_rev,
                "hbb_common": common_rev,
                "note": "升级时改这两个 sha 再跑本脚本；改动 see upstream/server.patch",
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
        f.write("\n")

    print(f"\n已导出到 {DEST}")
    print(f"版本指针写入 {PIN_STATE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
