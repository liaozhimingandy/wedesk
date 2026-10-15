#!/usr/bin/env python
"""从客户端 Cargo.toml 提取主包版本号，输出到 stdout。

单独成一个文件而不是在 .bat 里内联 python -c：
cmd 的 `for /f` 无法解析命令行里带括号的表达式（`.read()` 会被当成语法），
拆成脚本后 bat 侧只是一条普通命令，可移植性更好。

用法:
    python build/common/get-version.py [Cargo.toml 路径]

省略参数时取仓库的 client/Cargo.toml（由本脚本位置向上三级推导，不依赖 CWD）。
"""
import pathlib
import re
import sys

# 本脚本在 build/common/ 下，向上三级是仓库根；客户端 Cargo.toml 在其下的 client/ 里。
# 原先默认值写 "Cargo.toml"（相对 CWD），客户端整棵树迁进 client/ 后就失效了。
DEFAULT_CARGO = pathlib.Path(__file__).resolve().parent.parent.parent / "client" / "Cargo.toml"


def main() -> int:
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CARGO
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
