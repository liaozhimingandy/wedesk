#!/bin/sh
# =============================================================================
# 运行镜像构建期自检 —— hbbs / hbbr / rustdesk-utils 必须【真的能执行】
# =============================================================================
# 被 server/Dockerfile 与 server/Dockerfile.prebuilt 共用。
# 和 build-musl.sh 是同一个道理：两条构建路径（本机从源码编 / CI 装已编译产物）
# 共用同一份检查，避免"本地能跑、CI 推出去的镜像是坏的"这种最难复盘的漂移。
#
# 为什么非要这一步：
#   镜像里少了可用二进制时，docker build 本身【不会报任何错】，
#   要一直拖到 `docker compose up` 才炸出一句
#
#       error during container init: exec: "hbbr": executable file not found in $PATH
#
#   这句说的是容器 init 进程（command 的第一段）在 PATH 里没找到，
#   位置回指的是运行环节，对不上任何一行 Dockerfile，几乎没法按图索骥。
#   放到构建期就炸，排查成本差一个数量级。
#
# 为什么【实际执行】而不是只 test -x：
#   - test -x 抓不到"动态链接器缺失"：可执行位照样在，但 execve 返回 ENOENT；
#   - 光执行又抓不准"文件在、只是没 +x"的定位。
#   所以两种都查，且分开报告。
#
# 退出码：0 = 三个二进制均可执行；非 0 = 已打印具体原因。
# =============================================================================
set -e

status=0

for b in hbbs hbbr rustdesk-utils; do
    p="$(command -v "$b" 2>/dev/null || true)"

    # --- 情况 1：PATH 上找不到 --------------------------------------------
    # 注意 command -v 对"存在但无可执行位"的文件同样会跳过，
    # 所以补一次直查，把这两种原因分开说清楚。
    if [ -z "$p" ]; then
        if [ -e "/usr/bin/$b" ]; then
            echo "✗ /usr/bin/$b 存在，但缺少可执行位：" >&2
            echo "    $(ls -l "/usr/bin/$b")" >&2
            echo "" >&2
            echo "  典型成因：二进制经过 artifact 上传/下载（zip 打包）后丢失了 POSIX mode。" >&2
            echo "  actions/upload-artifact@v4 与 download-artifact@v4 不保留可执行位，" >&2
            echo "  必须在 Dockerfile 里 COPY 之后显式 chmod 0755。" >&2
        else
            echo "✗ 镜像里找不到 $b（不在 PATH 上）" >&2
            echo "    PATH=$PATH" >&2
            echo "  /usr/bin 下的内容：" >&2
            ls -l /usr/bin 2>/dev/null | head -20 >&2 || true
        fi
        status=1
        continue
    fi

    if [ ! -x "$p" ]; then
        echo "✗ $p 缺少可执行位：$(ls -l "$p")" >&2
        status=1
        continue
    fi

    # --- 情况 2：有 +x 但 execve 失败 ------------------------------------
    # 执行解析出的【绝对路径】而不是裸名字：保证"检查的"和"执行的"是同一个文件。
    # 写成 `cmd && rc=0 || rc=$?`，否则 set -e 会让脚本在这里直接退出。
    "$p" --help >/dev/null 2>&1 && rc=0 || rc=$?
    # 127 = command not found：execve 返回 ENOENT。典型是 glibc 二进制被 COPY
    #       进 alpine，它要的 ELF 解释器（/lib64/ld-linux-x86-64.so.2）在
    #       这个镜像里根本不存在。
    # 126 = 找到了但执行不了：不是本平台的可执行格式，或 shebang 指向的解释器
    #       不存在（产物被截断、架构不对、传错文件都会这样）。
    # 两个都要判 —— 只判 127 会漏掉后者。
    if [ "$rc" -eq 127 ] || [ "$rc" -eq 126 ]; then
        echo "✗ $b 无法执行（exit $rc）" >&2
        echo "" >&2
        if [ "$rc" -eq 127 ]; then
            echo "  127 = ENOENT：它不是静态二进制，依赖的动态链接器本镜像里没有，" >&2
            echo "  通常意味着把 glibc 编译的产物 COPY 进了 alpine。" >&2
            echo "  处理：确认 build-musl.sh 的 TARGET 是 *-unknown-linux-musl，" >&2
            echo "        或换成带 glibc 的基础镜像。" >&2
        else
            echo "  126 = 无法执行：文件格式不是本平台的可执行文件，或 shebang 指向的" >&2
            echo "  解释器不存在（产物被截断 / 架构不对 / 传错了文件都会这样）。" >&2
            echo "  处理：用 file <产物> 看它的真实类型与目标架构。" >&2
        fi
        status=1
        continue
    fi

    echo "✓ $b 可执行（$p）"
done

if [ "$status" -ne 0 ]; then
    echo "" >&2
    echo "镜像自检未通过 —— 已中止构建，避免产出一个能推但跑不起来的镜像。" >&2
    exit 1
fi

echo "✓ 三个二进制均通过可执行性自检"
