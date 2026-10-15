# syntax=docker/dockerfile:1
# =============================================================================
# wedesk 自建服务端镜像 —— 完全自编译，不基于 rustdesk 官方镜像
# =============================================================================
# 为什么不用 rustdesk/rustdesk-server 官方镜像：
#   1. 那是别人编译好的二进制，版本更新要看上游节奏，版本对你不可控；
#   2. 官方 hbbs/hbbr 每天会向 api.rustdesk.com 发一次版本检查请求，
#      而我们已用 upstream/server.patch 把它关掉；
#   3. 出问题没法对着源码定位。
#   本 Dockerfile 在容器内从 rustdesk-server 源码编译出 musl 静态二进制，
#   再装进干净的 alpine。构建机不需要装 Rust —— 只要能跑 docker build。
#
# 构建方式（context 必须是 server/ 目录）：
#     cd server && docker build -t wedesk-server:latest .
#   Docker Desktop 的默认平台跟本机走，给 Linux 服务器用时显式指定：
#     docker build --platform linux/amd64 -t wedesk-server:latest .
#
# 产出的二进制是 musl 静态链接，任意 Linux 发行版都能跑，不依赖 glibc 版本。
# =============================================================================

# ---------------------------------------------------------------------------
# 阶段 1：编译
# ---------------------------------------------------------------------------
# rust:alpine 的 host triple 就是 musl，产物天然静态。
# 固定大版本便于复现；要换编译器版本：
#   docker build --build-arg RUST_IMAGE=rust:1.87-alpine .
ARG RUST_IMAGE=rust:1.90-alpine
FROM ${RUST_IMAGE} AS builder

# build-base : gcc/make —— libsodium(sqlx 之外的加密) 与 sqlite3(sqlx bundled) 要现场编 C
# musl-dev   : musl 头文件，静态链接 libc
# perl       : ring(TLS) 的构建脚本用 perl 生成汇编，缺了会报 "perl: not found"
# pkgconfig  : 部分 *-sys 包用它探测系统库
RUN apk add --no-cache \
        build-base \
        musl-dev \
        perl \
        pkgconfig \
        cmake

WORKDIR /src

# sqlx 的查询宏在【编译期】就要连一个真实 SQLite 库来校验 SQL，上游为此在仓库里
# 塞了 db_v2.sqlite3。不设这个变量会报 `DATABASE_URL` 未设置。
# 它只参与编译期校验，不会进运行镜像。
ENV DATABASE_URL=sqlite:///src/db_v2.sqlite3

COPY rustdesk-server/ ./

# 两条 cache mount：
#   registry —— 依赖包缓存，重构建时不再重复下载几百 MB
#   target   —— 增量编译缓存，只改一个 .rs 时第二次构建从分钟级降到秒级
# 上游 release profile 开了 lto + codegen-units=1 + strip，首次全量编译 10~20 分钟
# 属于正常范围，之后都是增量。
# --locked：Cargo.lock 不允许被悄悄改写，依赖有变动应当显式失败。
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo build --locked --release \
        --bin hbbs --bin hbbr --bin rustdesk-utils

# ---------------------------------------------------------------------------
# 阶段 2：运行镜像
# ---------------------------------------------------------------------------
# 选 alpine 而不是 scratch：静态二进制在 scratch 里也能跑，但排障时连 sh 都没有，
# 没法 docker exec 进去查端口和文件。多约 5 MB 换可运维性，值得。
FROM alpine:3.21

RUN apk add --no-cache ca-certificates tzdata

COPY --from=builder /src/target/release/hbbs           /usr/bin/hbbs
COPY --from=builder /src/target/release/hbbr           /usr/bin/hbbr
COPY --from=builder /src/target/release/rustdesk-utils /usr/bin/rustdesk-utils

# 密钥（id_ed25519 / id_ed25519.pub）与 SQLite 数据库的工作目录。
# hbbs 默认把文件写在 $HOME 下，容器内 HOME 是 /root —— 与 docker-compose.yml
# 里的 `./data:/root` 挂载点对得上。
VOLUME /root
WORKDIR /root

EXPOSE 21115 21116 21116/udp 21117

# 不设 ENTRYPOINT：让 docker-compose 的 command（hbbs -r xxx / hbbr）直接生效。
# CMD 给个默认值，单独 docker run 时也能起来。
CMD ["hbbs"]
