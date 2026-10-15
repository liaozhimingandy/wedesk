#!/usr/bin/env python
"""从 Cargo.toml 提取主包版本号，输出到 stdout。

单独成一个文件而不是在 .bat 里内联 python -c：
cmd 的 `for /f` 无法解析命令行里带括号的表达式（`.read()` 会被当成语法），
拆成脚本后 bat 侧只是一条普通命令，可移植性更好。

用法:
    python build/common/get-version.py [Cargo.toml 路径]
"""
import pathlib
import re
import sys


def main() -> int:
    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "Cargo.toml")
    if not target.is_file():
        sys.stderr.write(f"[x] 找不到 {target}\n")
        return 1
    text = target.read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        sys.stderr.write(f"[x] {target} 里没有找到 version 字段\n")
        return 1
    sys.stdout.write(m.group(1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
