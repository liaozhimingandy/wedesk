#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验文档里引用的仓库路径是否真实存在。

为什么需要它
------------
重构目录结构时，文档里的路径是最容易跟代码脱节的地方——而路径写错不会让
任何构建失败，只会在新人照着文档敲命令时才发现"没有这个文件"。
本脚本把文档中所有形如 `client/...` / `server/...` / `build/...` 的引用
逐个 stat 一遍，写错立即报出。

用法:
    python build/common/check-doc-paths.py              # 检查全部文档
    python build/common/check-doc-paths.py docs/09.md   # 只检查指定文件

退出码: 0 = 全部存在；1 = 有不存在的路径；2 = 用法/环境错误
"""
from __future__ import annotations

import os
import re
import sys
import glob
from pathlib import Path

# 形如 <顶层目录>/... 的相对路径。
# 注意结尾不强制要求"以字母数字收尾"——那会把 `128x128.png` 截成 `128x128`
# （正则回溯时优先满足前面的量词），正是本脚本第一版的误报来源。
PATH_RE = re.compile(
    r"(?<![\w/.\-])((?:client|server|build|docs|upstream|\.github)/[A-Za-z0-9_][A-Za-z0-9_./\-@]*)"
)

# 以下路径"不存在"是正常状态，不算文档错误：
ALLOW_MISSING = {
    # flutter_rust_bridge 的代码生成产物，被 .gitignore 排除，仓库里本来就没有。
    # 文档提及它们是为了说明"缺了会报什么错"。
    "client/src/bridge_generated.rs",
    "client/src/bridge_generated.io.rs",
    # 常作为 grep 排除模式的前缀出现，如 grep -v "^client/src/bridge_generated"
    "client/src/bridge_generated",
    "client/flutter/lib/generated_bridge.dart",
    "client/flutter/lib/generated_bridge.freezed.dart",
    # rdctl.py 运行时才创建的目录
    "server/bin",
    "server/backup",
    # docs/11-server-ops.md 特意说明"不要在这里放 .env"，不存在才是正确状态
    "server/data/.env",
    # iOS 构建输出目录：CI 里 --no-codesign 只产出 .xcarchive，IPA 是手动 zip
    # 出来的（见 docs/03b-ci.md 设计点 17），仓库里本来就没有。
    "build/ios/ipa",
    # PostgreSQL 数据目录：只有 docker compose --profile pg 首次启动后才创建，
    # 默认 SQLite 部署下不存在才是正常状态（docs/13 场景 D 会提到它）。
    "server/deploy/pgdata",
}

# vendored 上游（client/libs、client/flutter）内部的路径另行核对，
# 它们的正确性以上游仓库为准，不由本脚本判定。
SKIP_PREFIX = ("client/libs/", "client/flutter/")


def check(doc: Path) -> list[str]:
    problems: list[str] = []
    text = doc.read_text(encoding="utf-8")
    for m in PATH_RE.finditer(text):
        raw = m.group(1).rstrip(".,;:)\"'`*/")
        if not raw or raw in ALLOW_MISSING or raw.startswith(SKIP_PREFIX):
            continue
        # 带通配符的是模式不是路径
        if any(c in raw for c in "*?["):
            continue
        if os.path.exists(raw):
            continue
        # 只剩一段目录名（如 `server/`）时不算引用
        if raw.count("/") == 1 and os.path.isdir(raw.rstrip("/")):
            continue
        problems.append(raw)
    return problems


def main(argv: list[str]) -> int:
    docs = [Path(a) for a in argv[1:]]
    if not docs:
        docs = (
            [Path("README.md"), Path("build/README.md")]
            + sorted(Path("docs").glob("*.md"))
            + [Path("upstream/UPSTREAM.md")]
        )
    docs = [d for d in docs if d.exists()]
    if not docs:
        print("没有找到可检查的文档")
        return 2

    bad: list[tuple[str, str]] = []
    total = 0
    for d in docs:
        text = d.read_text(encoding="utf-8")
        total += len(PATH_RE.findall(text))
        for p in check(d):
            bad.append((str(d), p))

    print("检查了 %d 个路径引用（%d 个文档）" % (total, len(docs)))
    if bad:
        print("\n!! 以下路径在仓库中不存在（%d 个）：" % len(bad))
        for doc, p in bad:
            print("   %-28s %s" % (doc, p))
        print("\n如果是代码生成产物或运行时目录，把它加进 ALLOW_MISSING。")
        return 1
    print("✓ 全部存在")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
