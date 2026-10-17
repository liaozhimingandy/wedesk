#!/usr/bin/env bash
# 把日志文件里的关键错误行挑出来，以 ::error:: 形式抛到 Actions 的 Annotations 区。
#
# 为什么需要它 —— 两个具体的失败模式，都是踩过的：
#
#   1. build.py 用 os.system() 包着每一条子命令，任何一步失败都只是
#      sys.exit(-1)。而 sys.exit(-1) 在 Git Bash 里映射到退出码 **127**，
#      看上去跟"命令不存在"一模一样。于是整个步骤在日志里只剩 runner 那句
#      "Process completed with exit code 127"，既看不出是 cargo 挂的还是
#      flutter 挂的，也看不出挂在哪一行。
#   2. cargo / flutter / CMake 失败时输出几千行，真正的报错往往被埋在中间。
#      Actions 的日志页面要展开十几段才看得到。
#
# 挑出来单独报出去之后，扫一眼 Annotations 就能定位，不必翻日志。
#
# 用法（必须 source，不能当子进程执行 —— 函数要留在调用者的 shell 里）：
#   source build/common/emit-error-lines.sh
#   if ! (cd client && cargo build ...) 2>&1 | tee "$LOG"; then
#     emit_error_lines "$LOG" cargo
#     exit 1
#   fi

emit_error_lines() {
  local log="${1:?用法: emit_error_lines <日志文件> <标签> [最多条数]}"
  local tag="${2:-build}"
  local max="${3:-12}"
  local n=0
  local line

  if [ ! -f "$log" ]; then
    echo "::error::[$tag] 日志文件不存在：$log"
    return 0
  fi

  # -i：cargo 是 "error[E0583]"，CMake 是 "CMake Error"，MSVC 是 "error LNK1181"
  # 大小写混用，统一忽略大小写更稳。
  while IFS= read -r line; do
    n=$((n + 1))
    [ "$n" -gt "$max" ] && break
    # workflow command 的三个保留字符必须转义，否则 GitHub 会把这条消息截断，
    # 剩下的内容静默丢弃 —— 那比不报还糟，因为看起来"报过了"。
    line="${line//%/%25}"
    line="${line//$'\r'/%0D}"
    line="${line//$'\n'/%0A}"
    echo "::error::[$tag] $line"
  done < <(grep -inE 'error(\[|:| )|failed|fatal|LNK[0-9]+|CMake Error|panicked' "$log" | head -n "$max")
}
