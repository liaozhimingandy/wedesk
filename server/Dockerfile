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
# 真正的编译命令不在这里，在 build-musl.sh —— 那份脚本同时被 CI 使用，
# 保证"本机 docker build"与"CI 编出来的二进制"是同一套配方。
# =============================================================================
ARG RUST_IMAGE=rust:1.90-alpine

# ---------------------------------------------------------------------------
# 阶段 1：编译
# ---------------------------------------------------------------------------
# rust:alpine 的 host triple 就是 musl，产物天然静态。
# 固定大版本便于复现；要换编译器版本：
#   docker build --build-arg RUST_IMAGE=rust:alpine .
FROM ${RUST_IMAGE} AS builder

# 编译目标。传给 build-musl.sh，决定产物落在 target/$TARGET/release 下。
# 这里显式写出来（而不是依赖 host 默认值）是为了让路径可预期：
# cargo 只有在传了 --target 时才会用 target/<triple>/ 子目录。
ARG TARGET=x86_64-unknown-linux-musl
ENV TARGET=$TARGET

WORKDIR /src

# sqlx 的查询宏在【编译期】就要连一个真实 SQLite 库来校验 SQL，上游为此在仓库里
# 塞了 db_v2.sqlite3。所需的 DATABASE_URL 由 build-musl.sh 内部设置。
# 依赖包安装同样在脚本里 —— 与 CI 共用一份，避免两边配方漂移。
COPY build-musl.sh /build-musl.sh
COPY rustdesk-server/ ./

# 两条 cache mount：
#   registry —— 依赖包缓存，重构建时不再重复下载几百 MB
#   target   —— 增量编译缓存，只改一个 .rs 时第二次构建从分钟级降到秒级
# 上游 release profile 开了 lto + codegen-units=1 + strip，首次全量编译 10~20 分钟
# 属于正常范围，之后都是增量。
# --locked：Cargo.lock 不允许被悄悄改写，依赖有变动应当显式失败。
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    sh /build-musl.sh

# ---------------------------------------------------------------------------
# 阶段 2：运行镜像
# ---------------------------------------------------------------------------
# 选 alpine 而不是 scratch：静态二进制在 scratch 里也能跑，但排障时连 sh 都没有，
# 没法 docker exec 进去查端口和文件。多约 5 MB 换可运维性，值得。
FROM alpine:3.21

RUN apk add --no-cache ca-certificates tzdata

ARG TARGET=x86_64-unknown-linux-musl
COPY --from=builder /src/target/${TARGET}/release/hbbs           /usr/bin/hbbs
COPY --from=builder /src/target/${TARGET}/release/hbbr           /usr/bin/hbbr
COPY --from=builder /src/target/${TARGET}/release/rustdesk-utils /usr/bin/rustdesk-utils

# 密钥（id_ed25519 / id_ed25519.pub）与 SQLite 数据库的工作目录。
# hbbs 默认把文件写在 $HOME 下，容器内 HOME 是 /root —— 与 docker-compose.yml
# 里的 `./data:/root` 挂载点对得上。
VOLUME /root
WORKDIR /root

# 声明仅为文档用途：真正能访问到的是 docker-compose.yml 里 publish 出来的端口。
# 5186 = hbbs NAT 探测，5187 = hbbs 主端口（TCP 打洞 + UDP 注册心跳），
# 5188 = hbbr 中继。容器内还监听 5189(hbbs WS) 与 5190(hbbr WS)，刻意不列出
# 也不 publish —— 它们无条件信任 X-Real-IP 头，见 docker-compose.yml 的说明。
EXPOSE 5186 5187 5187/udp 5188

# 不设 ENTRYPOINT：让 docker-compose 的 command（hbbs -r xxx / hbbr）直接生效。
# CMD 给个默认值，单独 docker run 时也能起来。
CMD ["hbbs"]
