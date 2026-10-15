#!/bin/sh
# =============================================================================
# 在 musl 环境里编译 hbbs / hbbr / rustdesk-utils
# =============================================================================
# 【为什么要在 musl 容器里本机编译，而不是交叉编译】
#   服务端常用 *-sys 依赖有三个：openssl-sys、libsqlite3-sys、libsodium-sys。
#   在 glibc 机器上 `cargo build --target x86_64-unknown-linux-musl` 时，
#   openssl-sys 的构建脚本会去找【目标平台】的 OpenSSL，找不到就报：
#       Could not find directory of OpenSSL installation, and this `-sys`
#       crate cannot proceed without this knowledge. ... set the `OPENSSL_DIR`
#   手工给 musl 再编一份 OpenSSL 并设置 OPENSSL_DIR/OPENSSL_LIB_DIR 固然能过，
#   但那套路径完全依赖宿主发行版与 runner 镜像版本，换个环境就失效。
#
#   在 musl 容器里编译就没有这个问题：容器的 host triple 本身就是 musl，
#   alpine 的 openssl-dev / sqlite-dev 本来就是给 musl 编的，pkg-config 一查即中，
#   产物还是货真价实的 musl 静态二进制。
#
# 【为什么要单独抽成脚本】
#   server/Dockerfile（本机/手动构建）和 GitHub Actions（CI）共用这一份。
#   两份构建配方一旦各自漂移，就会出现"CI 绿但本地 docker build 红（或反之）"
#   这种最难复盘的故障。构建命令只有这一处。
# =============================================================================
# 约定：
#   输入  /src                          rustdesk-server 源码目录（vendored）
#   输出  /src/target/$TARGET/release/  hbbs / hbbr / rustdesk-utils
#         $OUT（默认 /out）              上面三个二进制的副本
#   环境变量 TARGET 可覆盖目标三元组（默认 x86_64-unknown-linux-musl）
#         OUT   可覆盖副本目录（默认 /out）
#
# 【为什么还要往 /out 复制一份 —— 本机 docker build 直接踩过的坑】
#   server/Dockerfile 里 cargo 的 target 目录是挂成 BuildKit cache mount 的：
#       RUN --mount=type=cache,target=/src/target  sh /build-musl.sh
#   cache mount 的设计就是"内容不进镜像层"：RUN 一结束挂载即卸载，
#   写进去的东西只留在构建缓存里供下次构建复用，绝不会落进镜像层。
#   而 cargo 用 --target 时产物正好写在 /src/target/$TARGET/release/ —— 挂载点上。
#   于是下一阶段 COPY --from=builder /src/target/$TARGET/release/hbbr 拿不到文件：
#   轻则构建直接失败，重则本地残着更早打出来的同名镜像、
#   docker compose up（不带 --build）静默复用旧镜像，
#   启动时才报 exec: "hbbr": executable file not found in $PATH，
#   而报错行列回指的是容器 init，完全看不出是镜像里少了东西。
#
#   所以产物必须落在【普通目录】里，且复制要和编译处在【同一个 RUN 内】
#   （CI 里没有 cache mount，target/ 本就在卷里，多复制一份也无妨）。
# =============================================================================

set -eu

TARGET="${TARGET:-x86_64-unknown-linux-musl}"
SRC="${SRC:-/src}"

echo "==> 编译目标：$TARGET"
rustc -vV || true

# ---------------------------------------------------------------------------
# 编译期依赖
# ---------------------------------------------------------------------------
# build-base/musl-dev : gcc + musl 头文件，musl 静态链接 libc 的前提
# perl                : ring(TLS) 的构建脚本用 perl 生成汇编，缺了报 "perl: not found"
# pkgconfig           : openssl-sys / libsqlite3-sys 靠它探测系统库
# cmake               : 部分 *-sys 包的构建系统
# git                 : Cargo.lock 里钉了 7 个 git 依赖（reqwest/confy/sysinfo 等），
#                       cargo 要现场 clone —— 忘装会报 `git: command not found`
# openssl-dev         : 【本次报错的关键】openssl-sys 需要 libssl.pc + 头文件
# sqlite-dev          : sqlx 开的是 sqlite 而非 bundled，libsqlite3-sys 走 pkg-config
# libsodium-dev       : libsodium-sys 若选了系统库而非自带源码，需要它
# zstd-dev            : zstd-sys 同理
# file                : 末尾静态性自检要用
apk add --no-cache \
    build-base musl-dev perl pkgconfig cmake git file \
    openssl-dev sqlite-dev libsodium-dev zstd-dev

# *-static 子包提供 .a，装上了才能真正静态链接；某些 alpine 版本可能缺其一。
# 逐包安装而不是一条 apk add，是为了让"某个 -static 包不可用"只降级
# （该库退回动态链接，运行时镜像同为 alpine 依旧能跑），而不是整次构建失败。
for p in openssl-libs-static sqlite-static libsodium-static zstd-static; do
    if apk add --no-cache "$p" >/dev/null 2>&1; then
        echo "    ✓ $p：静态链接"
    else
        echo "    ⚠ $p 不可用，对应库将动态链接（alpine 运行镜像内仍可用）"
    fi
done

# OPENSSL_STATIC=1：musl target 虽然默认 +crt-static，OpenSSL 仍会去找动态
# libssl.so。要让打包出来的裸二进制能在任意 Linux 发行版上直接运行，必须静态。
export OPENSSL_STATIC=1

# DATABASE_URL 是 sqlx 的编译期要求：hbbs 里的查询宏会在编译阶段连一个真实
# SQLite 库做 SQL 校验，上游为此把一个 db_v2.sqlite3 模板库放进了仓库根目录。
# 少了它报 `DATABASE_URL must be set`。它只参与编译期校验，不进运行镜像。
export DATABASE_URL="sqlite://$SRC/db_v2.sqlite3"

cd "$SRC"

cargo build --locked --release --target "$TARGET" \
    --bin hbbs --bin hbbr --bin rustdesk-utils

BIN="$SRC/target/$TARGET/release"
ls -lh "$BIN/hbbs" "$BIN/hbbr" "$BIN/rustdesk-utils"

# ---------------------------------------------------------------------------
# 把三个二进制复制到 $OUT（默认 /out）—— 与编译同处一个 RUN，见文件头说明
# ---------------------------------------------------------------------------
OUT="${OUT:-/out}"
mkdir -p "$OUT"
cp -f "$BIN/hbbs" "$BIN/hbbr" "$BIN/rustdesk-utils" "$OUT/"
chmod 755 "$OUT/hbbs" "$OUT/hbbr" "$OUT/rustdesk-utils"
echo "==> 产物副本目录 $OUT："
ls -lh "$OUT"

# ---------------------------------------------------------------------------
# 静态性自检 —— 只报告，不失败
# ---------------------------------------------------------------------------
# 上面缺了某个 -static 包时，产物会带上动态依赖。放进 alpine 镜像没问题，
# 但 offline 分发 zip 到其他发行版就会 `not found`。这里把结论打出来，
# 真出现"只有 Docker 能跑、裸二进制跑不起来"时可以直接对上。
echo "==> 产物链接方式："
for b in hbbs hbbr rustdesk-utils; do
    printf '    %-16s %s\n' "$b" "$(file "$BIN/$b" | cut -d: -f2- | sed 's/^ *//')"
done
