#!/usr/bin/env bash
# =============================================================================
# rdrun.sh —— 用 docker run 一键启动 / 停止 wedesk 服务端（hbbs / hbbr）
# =============================================================================
# 为什么要有这个脚本（而不是只给 docker-compose.yml）：
#   1. hbbs（ID 注册）与 hbbr（中继）要【分机部署】时，compose 文件是"两台一起"
#      的写法，分机得各自跑一份并屏蔽掉另一个服务，容易改错；
#   2. 生产机器上可能只有 docker，没有 compose 插件；
#   3. 编排到 systemd / k8s 时，最终落地的也是这里的这两条 docker run。
#
# 只依赖 docker + bash，零第三方依赖（与 rdctl.py 一样）。
#
# 用法：
#   ./rdrun.sh both        # 同机启动 hbbs + hbbr（默认）
#   ./rdrun.sh hbbs        # 只跑 ID 注册服务器（另一台机器跑 hbbr）
#   ./rdrun.sh hbbr        # 只跑中继服务器
#   ./rdrun.sh status | key | stop | restart | rm
#
# 数据库：默认 PostgreSQL（内置 rd-postgres 容器，自动创建）。
#   - 想接外部库：填 DB_URL=postgres://user:pwd@host:5432/db 即可，脚本不会拉起本地 PG。
#   - 想退回 SQLite：--db-type sqlite（此时不托管密钥、不落审计，与官方行为一致）。
#
# 典型分机部署：
#   # A 机（ID 注册）：
#   ./rdrun.sh hbbs --relay-host 203.0.113.10
#   # B 机（中继）：
#   ./rdrun.sh hbbr
#
# 完整文档：docs/13-server-deployment-handbook.md 第 3.5 节
# =============================================================================
set -euo pipefail

SELF="$(basename "$0")"

# --- 默认值（会被同目录 .env 覆盖，再被命令行覆盖） -------------------------
IMAGE="wedesk-server:latest"
RELAY_HOST=""
HBBS_PORT="5187"
HBBS_NAT_PORT="5186"
HBBR_PORT="5188"
DATA_DIR="$PWD/data"
NET_MODE="bridge"          # bridge（端口映射）| host（仅 Linux 生效）
NET_NAME="rd-net"
DB_TYPE="postgres"
# 留空且 DB_TYPE=postgres 时，自动用下面的 PG_* 拼出连接串，并由本脚本顺带拉起
# rd-postgres 容器 —— 这样默认部署依然是"一条命令跑起来"，不用先学 compose。
# 想接外部/托管 Postgres（RDS 等），直接填完整连接串即可，脚本检测到 host 不是
# 内置容器名就【不会】再拉起本地 Postgres。
DB_URL=""
PG_IMAGE="postgres:16-alpine"
PG_DB="rustdesk"
PG_USER="rustdesk"
PG_PASSWORD="changeme"     # 生产务必改成强口令：私钥也托管在这个库里
PG_MEM="512m"; PG_CPUS="1.00"
ALWAYS_USE_RELAY="N"
ENFORCED_RELAY="N"
RUST_LOG="warn"
DB_CONNECT_RETRIES=""
MAX_DATABASE_CONNECTIONS=""
MEM_HBBS="256m";  CPUS_HBBS="0.50"
MEM_HBBR="512m";  CPUS_HBBR="1.00"
LOG_MAX_SIZE="10m"; LOG_MAX_FILE="3"

ACTION="both"
DO_PULL=0
FORCE=0
DRY_RUN=0

die() { echo "::error:: $*" >&2; exit 1; }
info() { echo "==> $*"; }
warn() { echo "::warning:: $*"; }

usage() {
  cat <<EOF
$SELF —— 用 docker run 启动 wedesk 服务端（hbbs / hbbr），不依赖 docker compose

用法:
  $SELF <both|hbbs|hbbr>  [选项]     启动对应服务（默认 both）
  $SELF <status|key|stop|restart|rm>  [选项]   运维动作

选项:
  --image <镜像>            默认 wedesk-server:latest（.env 的 RD_IMAGE 优先）
  --relay-host <IP|域名>    客户端访问【中继】的地址。不填则自动探测本机 IP 并提醒确认
  --hbbs-port <P>           hbbs 主端口，默认 5187
  --nat-port  <P-1>         NAT 探测端口，默认 = 主端口-1（源码硬编码，改了会拒绝启动）
  --hbbr-port <R>           hbbr 中继端口，默认 5188
  --data-dir <路径>         密钥与 SQLite 落盘目录，默认 ./data
  --net <bridge|host>       bridge=端口映射（跨平台）；host=宿主网络（仅 Linux，需防火
                            墙 DROP 5189/5190）
  --db-type <sqlite|postgres>   默认 postgres
  --db-url  <路径|连接串>   postgres 留空 = 用内置 Postgres 容器（自动创建 rd-postgres）；
                            填 postgres://user:pwd@host:5432/db 指向外部库时，
                            本脚本检测到主机不是本机就不再拉起本地 Postgres
                            sqlite 用容器内路径 /root/db_v2.sqlite3
  --pg-password <口令>      内置 Postgres 的口令（默认 changeme，生产必须改）
  --always-relay <Y|N>      Y=强制所有连接走中继（跨公网提高连通率，吃服务器带宽）
  --rust-log <warn|info|debug|...>
  --pull                    启动前先 docker pull 镜像
  --force                   同名容器已存在时先删除再启动（改参数后重建用）
  --dry-run                 只打印将要执行的命令，不真的执行
  -h, --help

示例:
  $SELF both --relay-host 10.0.0.5            # 单机：注册 + 中继
  $SELF hbbs --relay-host 203.0.113.10        # 分机：只做 ID 注册
  $SELF hbbr                                  # 分机：只做中继
  $SELF hbbr --hbbr-port 5188 --net host      # 中继机走宿主网络
  $SELF key                                   # 打印服务端公钥
EOF
}

# --- 读 .env（只取本脚本关心的键，作为默认值） ------------------------------
load_env() {
  local f="${1:-$PWD/.env}"
  [ -f "$f" ] || return 0
  while IFS='=' read -r k v; do
    k="${k#"${k%%[![:space:]]*}"}"    # 去前导空白
    k="${k%"${k##*[![:space:]]}"}"    # 去尾部空白
    [ -n "$k" ] || continue
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    # 剥掉引号与【行内注释】。后者必须处理：.env 里大量写成
    #   HBBS_PORT=5187   # hbbs 主端口，同时传给 `hbbs -p`
    # 不剥离的话，值会变成 "5187   # hbbs 主端口..."，后面拿去做算术比较直接报错。
    case "$v" in
      \"*) v="${v#\"}"; v="${v%%\"*}" ;;
      \'*) v="${v#\'}"; v="${v%%\'*}" ;;
      *)   v="${v%%#*}" ;;
    esac
    # 注释剥掉后尾部可能留空白，再清一次
    v="${v%"${v##*[![:space:]]}"}"
    case "$k" in
      RD_IMAGE)                 IMAGE="$v" ;;
      RELAY_HOST)               RELAY_HOST="$v" ;;
      HBBS_PORT)                HBBS_PORT="$v" ;;
      HBBS_NAT_PORT)            HBBS_NAT_PORT="$v" ;;
      HBBR_PORT)                HBBR_PORT="$v" ;;
      DB_TYPE)                  DB_TYPE="$v" ;;
      DB_URL)                   DB_URL="$v" ;;
      ALWAYS_USE_RELAY)         ALWAYS_USE_RELAY="$v" ;;
      ENFORCED_RELAY)           ENFORCED_RELAY="$v" ;;
      RUST_LOG)                 RUST_LOG="$v" ;;
      DB_CONNECT_RETRIES)       DB_CONNECT_RETRIES="$v" ;;
      MAX_DATABASE_CONNECTIONS) MAX_DATABASE_CONNECTIONS="$v" ;;
      PG_IMAGE)                 PG_IMAGE="$v" ;;
      PG_DB)                    PG_DB="$v" ;;
      PG_USER)                  PG_USER="$v" ;;
      PG_PASSWORD)              PG_PASSWORD="$v" ;;
    esac
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=' "$f" || true)
}

# --- 自动探测本机对外 IP（用户没给 --relay-host 时） ------------------------
detect_ip() {
  local ip=""
  ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [ -n "$ip" ] || ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$ip" ] || ip="$(ifconfig 2>/dev/null | awk '/inet / && $2 !~ /^127\./ {print $2; exit}' | sed 's/addr://')"
  printf '%s' "$ip"
}

# --- 端口关系校验：HBBS_NAT_PORT 必须 == HBBS_PORT-1 ------------------------
# hbbs 源码里 nat_port = port - 1 / ws_port = port + 2 是硬编码的（没有开关），
# 所以给错组合只能拒绝启动 —— 宁可起不来，也不要一个"端口开着但连不上"的服务。
check_ports() {
  local expect=$((HBBS_PORT - 1))
  if [ "$HBBS_NAT_PORT" != "$expect" ]; then
    die "HBBS_NAT_PORT($HBBS_NAT_PORT) 必须 = HBBS_PORT-1($expect)。源码硬编码 nat_port=port-1，改了不生效只会得到半通的配置。要平移就把 --hbbs-port 与 --nat-port 一起改。"
  fi
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "没找到 docker，请先安装 Docker。"
  docker info >/dev/null 2>&1 || die "docker 守护进程不可用（Docker 没启动，或当前用户无权访问 /var/run/docker.sock）。"
}

ensure_net() {
  if ! docker network inspect "$NET_NAME" >/dev/null 2>&1; then
    info "创建网络 $NET_NAME"
    [ "$DRY_RUN" = 1 ] || docker network create "$NET_NAME" >/dev/null
  fi
}

ensure_image() {
  # --pull：远端镜像（名字里带 /）总是拉最新的；本地自编镜像名没有 /，跳过
  if [ "$DO_PULL" = 1 ]; then
    case "$IMAGE" in
      */*)
        info "拉取远端镜像 $IMAGE"
        [ "$DRY_RUN" = 1 ] || docker pull "$IMAGE"
        return 0
        ;;
    esac
  fi
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    return 0
  fi
  if [ "$DO_PULL" = 1 ]; then
    info "拉取镜像 $IMAGE"
    [ "$DRY_RUN" = 1 ] || docker pull "$IMAGE"
  else
    die "本地没有镜像 $IMAGE。二选一：
  1) 从源码构建（在 server/ 目录下）：
       docker build --platform linux/amd64 -t wedesk-server:latest -f deploy/Dockerfile .
  2) 拉 CI 推的远端镜像：
       docker pull liaozhiming/wedesk-server:latest
     （或给本脚本加 --pull 自动拉取）"
  fi
}

# 已存在同名容器时的处理：--force 才删，否则提示并退出（避免误覆盖生产容器）
precheck_container() {
  local name="$1"
  if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    if [ "$FORCE" = 1 ]; then
      info "删除已存在的容器 $name（--force）"
      [ "$DRY_RUN" = 1 ] || docker rm -f "$name" >/dev/null
    else
      die "容器 $name 已存在。改参数重建请加 --force；只是想重启用 '$SELF restart'；查看状态用 '$SELF status'。"
    fi
  fi
}

run_or_print() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '  '; printf '%q ' "$@"; echo
  else
    "$@"
  fi
}

# --- 数据库：默认走 PostgreSQL ----------------------------------------------
# 判断 DB_URL 指向的主机是不是【本脚本自己拉起】的内置 Postgres 容器。
# 只有这种情况下才需要自动创建容器 —— 用户填了外部数据库（RDS / 另一台机器）
# 时不能多此一举地拉一个本地 Postgres 起来。
pg_url_points_local() {
  case "$DB_TYPE" in
    postgres) ;;
    *) return 1 ;;
  esac
  local host
  # 从 postgres://user:pwd@host:port/db 里抠出 host 段
  host="$(printf '%s' "$DB_URL" | sed -E 's|^postgres(ql)?://[^@]*@([^:/?]+).*|\2|')"
  case "$host" in
    postgres|rd-postgres|localhost|127.0.0.1) return 0 ;;
    *) return 1 ;;
  esac
}

# 若 DB_URL 为空而 DB_TYPE=postgres，则用 PG_* 拼一个内置容器的连接串。
#
# 主机名为什么是 rd-postgres 而不是 postgres：
#   `postgres` 是 compose 的【服务名】，只在 docker compose 创建的网络里有 DNS 记录；
#   这里是 docker run，容器之间靠【容器名】互访，所以必须写成 rd-postgres。
#   写错的表现非常隐蔽 —— hbbs 起得来、日志只有一行解析失败，然后直接退出。
# host 网络模式下没有这个 DNS，直接用 127.0.0.1。
derive_db() {
  [ "$DB_TYPE" = "postgres" ] || return 0
  if [ -z "$DB_URL" ]; then
    local host="rd-postgres"
    [ "$NET_MODE" = "host" ] && host="127.0.0.1"
    DB_URL="postgres://${PG_USER}:${PG_PASSWORD}@${host}:5432/${PG_DB}"
    info "DB_URL 未设置，按内置 Postgres 自动拼出：$(
      printf '%s' "$DB_URL" | sed -E 's|(://[^:]*:)[^@]*(@)|\1******\2|'
    )"
  fi
  case "$PG_PASSWORD" in
    ""|changeme|postgres|rustdesk|password|123456)
      warn "PG_PASSWORD 还是弱口令（'$PG_PASSWORD'）。密钥也托管在这个库里，等于把服务端私钥的访问权限交了出去 —— 生产环境必须改掉。"
      ;;
  esac
}

# 需要时拉起内置 Postgres 容器（幂等：已存在就复用，停了就 start）。
ensure_postgres() {
  pg_url_points_local || return 0
  local pgdata="$PWD/pgdata"

  if docker ps -a --format '{{.Names}}' | grep -qx rd-postgres; then
    if ! docker ps --format '{{.Names}}' | grep -qx rd-postgres; then
      info "启动已存在但已停止的 Postgres 容器 rd-postgres"
      [ "$DRY_RUN" = 1 ] || docker start rd-postgres >/dev/null
    else
      info "Postgres 容器 rd-postgres 已在运行，复用"
    fi
    return 0
  fi

  [ "$DRY_RUN" = 1 ] || mkdir -p "$pgdata"
  if ! docker image inspect "$PG_IMAGE" >/dev/null 2>&1; then
    info "拉取镜像 $PG_IMAGE"
    [ "$DRY_RUN" = 1 ] || docker pull "$PG_IMAGE"
  fi

  info "创建 Postgres 容器 rd-postgres（数据目录 $pgdata）"
  if [ "$DRY_RUN" = 1 ]; then
    echo "  docker run -d --name rd-postgres ... $PG_IMAGE"
    return 0
  fi

  local cmd=(docker run -d --name rd-postgres --restart unless-stopped)
  if [ "$NET_MODE" = "host" ]; then
    # host 模式下 Postgres 直接占用宿主的 5432，必须确认它没被别的实例占用
    cmd+=(--network host)
  else
    ensure_net
    cmd+=(--network "$NET_NAME")
  fi
  cmd+=(-v "$pgdata:/var/lib/postgresql/data")
  cmd+=(-e "POSTGRES_DB=$PG_DB")
  cmd+=(-e "POSTGRES_USER=$PG_USER")
  cmd+=(-e "POSTGRES_PASSWORD=$PG_PASSWORD")
  cmd+=(-e "PGDATA=/var/lib/postgresql/data/pgdata")
  cmd+=(--memory "$PG_MEM" --cpus "$PG_CPUS")
  cmd+=("$PG_IMAGE")
  "${cmd[@]}" >/dev/null

  # 用 pg_isready 轮询而不是看容器的 healthcheck 状态：
  # 官方 postgres 镜像不保证自带 HEALTHCHECK，读 State.Health 会拿到空串，
  # 这里就会白等 30 秒。直接问数据库本身更可靠。
  local i
  for i in $(seq 1 30); do
    if docker exec rd-postgres pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
      info "Postgres 已就绪"
      return 0
    fi
    sleep 1
  done
  warn "Postgres 30 秒内未进入 healthy 状态（可能是首次初始化）。hbbs 会自行重试连接，可用 '$SELF status' 观察。"
}

# --- 组装并启动 hbbs ---------------------------------------------------------
start_hbbs() {
  check_ports
  precheck_container rd-hbbs

  local cmd=(docker run -d --name rd-hbbs --restart unless-stopped)
  if [ "$NET_MODE" = "host" ]; then
    cmd+=(--network host)
    warn "已选 host 网络：容器内 5189（hbbs WS）会暴露到宿主网络栈。请确认防火墙已 DROP TCP 5189，且只放行 5186/5187/5188。"
  else
    ensure_net
    cmd+=(--network "$NET_NAME")
    # 5187 同时要 TCP 与 UDP：Docker 里 "5187:5187" 只 publish TCP，
    # UDP 必须单独写一条 /udp —— 少了它，客户端注册永远超时（ID 出不来）。
    cmd+=(-p "$HBBS_NAT_PORT:$HBBS_NAT_PORT/tcp")
    cmd+=(-p "$HBBS_PORT:$HBBS_PORT/tcp")
    cmd+=(-p "$HBBS_PORT:$HBBS_PORT/udp")
  fi
  cmd+=(-v "$DATA_DIR:/root")
  cmd+=(--memory "$MEM_HBBS" --cpus "$CPUS_HBBS" --pids-limit 128)
  cmd+=(--log-opt max-size="$LOG_MAX_SIZE" --log-opt max-file="$LOG_MAX_FILE")
  cmd+=(-e "ALWAYS_USE_RELAY=$ALWAYS_USE_RELAY")
  cmd+=(-e "ENFORCED_RELAY=$ENFORCED_RELAY")
  cmd+=(-e "DB_TYPE=$DB_TYPE")
  cmd+=(-e "DB_URL=$DB_URL")
  cmd+=(-e "RUST_LOG=$RUST_LOG")
  # 这两个变量刻意留空时用后端各自的默认值（sqlite→1 / postgres→8），
  # 所以只有非空才传 —— 写成 `test -n && cmd+=(...)` 在 set -e 下会让函数直接退出。
  if [ -n "$DB_CONNECT_RETRIES" ]; then
    cmd+=(-e "DB_CONNECT_RETRIES=$DB_CONNECT_RETRIES")
  fi
  if [ -n "$MAX_DATABASE_CONNECTIONS" ]; then
    cmd+=(-e "MAX_DATABASE_CONNECTIONS=$MAX_DATABASE_CONNECTIONS")
  fi
  # -r 必须带端口：不带的话 hbbs 会按默认的 21117 拼给客户端，
  # 表现为"能注册、连不上桌面"。多个中继用逗号分隔：1.2.3.4:5188,5.6.7.8:5188
  cmd+=("$IMAGE" hbbs -p "$HBBS_PORT" -r "$RELAY_HOST:$HBBR_PORT")

  info "启动 hbbs（ID 注册） 端口 TCP $HBBS_NAT_PORT/$HBBS_PORT + UDP $HBBS_PORT，中继指向 $RELAY_HOST:$HBBR_PORT"
  run_or_print "${cmd[@]}"
}

# --- 组装并启动 hbbr ---------------------------------------------------------
start_hbbr() {
  precheck_container rd-hbbr

  local cmd=(docker run -d --name rd-hbbr --restart unless-stopped)
  if [ "$NET_MODE" = "host" ]; then
    cmd+=(--network host)
    warn "已选 host 网络：容器内 5190（hbbr WS）会暴露到宿主网络栈。请确认防火墙已 DROP TCP 5190。"
  else
    ensure_net
    cmd+=(--network "$NET_NAME")
    cmd+=(-p "$HBBR_PORT:$HBBR_PORT/tcp")
  fi
  # 挂载 data 只为与 compose 行为一致（hbbr 自己不写库，但读同一份密钥目录）
  cmd+=(-v "$DATA_DIR:/root")
  cmd+=(--memory "$MEM_HBBR" --cpus "$CPUS_HBBR" --pids-limit 256)
  cmd+=(--log-opt max-size="$LOG_MAX_SIZE" --log-opt max-file="$LOG_MAX_FILE")
  cmd+=(-e "RUST_LOG=$RUST_LOG")
  # 注意这里是命令行 -p，不是环境变量 PORT：hbbr 源码里环境变量 PORT 会被 +1 再用，
  # 写成 PORT=5188 它会去监听 5189，正好撞上 hbbs 的 WebSocket 端口。
  cmd+=("$IMAGE" hbbr -p "$HBBR_PORT")

  info "启动 hbbr（中继） 端口 TCP $HBBR_PORT"
  run_or_print "${cmd[@]}"
}

do_start() {
  require_docker
  ensure_image
  mkdir -p "$DATA_DIR"
  # 默认后端是 PostgreSQL：DB_URL 没填就自动拼内置容器，并顺带把它拉起来
  derive_db
  ensure_postgres
  case "$ACTION" in
    both) start_hbbs; start_hbbr ;;
    hbbs) start_hbbs ;;
    hbbr) start_hbbr ;;
  esac
  if [ "$DRY_RUN" = 1 ]; then
    info "--dry-run：以上命令未实际执行"
    return 0
  fi
  echo
  info "容器状态："
  docker ps --filter 'name=rd-hbbs' --filter 'name=rd-hbbr' \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
  if [ "$ACTION" = "hbbs" ] || [ "$ACTION" = "both" ]; then
    echo
    info "服务端公钥（写入客户端 RS_PUB_KEY）："
    print_key || warn "还没生成，稍后再跑 '$SELF key'"
  fi
}

# 打印公钥：优先从运行中的容器取；没起容器就从 data 目录直接读
print_key() {
  local pub="$DATA_DIR/id_ed25519.pub"
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx rd-hbbs; then
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      if docker exec rd-hbbs cat /root/id_ed25519.pub 2>/dev/null; then
        return 0
      fi
      sleep 1
    done
    return 1
  fi
  if [ -f "$pub" ]; then
    cat "$pub"
    return 0
  fi
  return 1
}

do_stop()    { require_docker; docker rm -f rd-hbbs rd-hbbr >/dev/null 2>&1 || true; info "已停止并删除容器（数据保留在 $DATA_DIR）"; }
do_restart() { FORCE=1; do_stop; do_start; }
do_status() {
  require_docker
  docker ps -a --filter 'name=rd-hbbs' --filter 'name=rd-hbbr' \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
  echo
  echo "数据目录: $DATA_DIR"
  echo "镜像:     $IMAGE"
  echo "数据库:   $DB_TYPE"
  if [ "$DB_TYPE" = "postgres" ]; then
    # 脱敏后再打印：连接串里含明文口令，而排障真正需要的（主机 / 库名）依然可见
    echo "          连接: $(printf '%s' "$DB_URL" | sed -E 's|(://[^:]*:)[^@]*(@)|\1******\2|')"
    echo "          库内共 3 张表：peer（ID 台账）、server_config（密钥托管）、connection_log（连接审计）"
  else
    echo "          连接: $DB_URL"
    echo "          ⚠️ SQLite 模式下不托管密钥、不落审计，仅 peer 表生效"
  fi
  echo "端口规划: hbbs TCP $HBBS_NAT_PORT / TCP+UDP $HBBS_PORT   hbbr TCP $HBBR_PORT"
  echo "          容器内还监听 5189(hbbs WS) / 5190(hbbr WS)，刻意不 publish"
}

# --- 参数解析 ---------------------------------------------------------------
# .env 只提供【默认】值：命令行显式给的参数优先，所以必须在解析参数之前读
load_env "$PWD/.env"

[ $# -eq 0 ] || case "$1" in
  both|hbbs|hbbr|status|key|stop|restart|rm) ACTION="$1"; shift ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --image)                   IMAGE="${2:?--image 需要一个值}"; shift 2 ;;
    --relay-host)              RELAY_HOST="${2:?--relay-host 需要一个值}"; shift 2 ;;
    --hbbs-port)               HBBS_PORT="${2:?--hbbs-port 需要一个值}"; shift 2 ;;
    --nat-port)                HBBS_NAT_PORT="${2:?--nat-port 需要一个值}"; shift 2 ;;
    --hbbr-port)               HBBR_PORT="${2:?--hbbr-port 需要一个值}"; shift 2 ;;
    --data-dir)                DATA_DIR="${2:?--data-dir 需要一个值}"; shift 2 ;;
    --net)                     NET_MODE="${2:?--net 需要一个值}"; shift 2 ;;
    --db-type)                 DB_TYPE="${2:?--db-type 需要一个值}"; shift 2 ;;
    --db-url)                  DB_URL="${2:?--db-url 需要一个值}"; shift 2 ;;
    --pg-password)             PG_PASSWORD="${2:?--pg-password 需要一个值}"; shift 2 ;;
    --always-relay)            ALWAYS_USE_RELAY="${2:?--always-relay 需要一个值}"; shift 2 ;;
    --rust-log)                RUST_LOG="${2:?--rust-log 需要一个值}"; shift 2 ;;
    --pull)                    DO_PULL=1; shift ;;
    --force)                   FORCE=1; shift ;;
    --dry-run)                 DRY_RUN=1; shift ;;
    -h|--help)                 usage; exit 0 ;;
    *)                         echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

case "$NET_MODE" in
  bridge|host) ;;
  *) die "--net 只能是 bridge 或 host" ;;
esac
case "$DB_TYPE" in
  sqlite|postgres) ;;
  *) die "--db-type 只能是 sqlite 或 postgres" ;;
esac

# Git Bash / MSYS 会把看起来像 Unix 路径的参数偷偷改写成 Windows 路径
# （实测把 /root/db_v2.sqlite3 变成 C:/Program Files/.../PortableGit/root/db_v2.sqlite3），
# 直接后果就是 hbbs 起来就崩：unable to open database file。
# MSYS_NO_PATHCONV=1 关掉这个转换；此时数据目录要写成 Windows 风格。
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*)
    export MSYS_NO_PATHCONV=1
    warn "检测到 Git Bash/MSYS：已设 MSYS_NO_PATHCONV=1 防止 /root 被改写成本机路径。数据目录请用 Windows 风格（如 C:/wedesk/data）；生产请直接在 Linux 服务器或 WSL 里运行。"
    ;;
esac

# RELAY_HOST 决定客户端去哪找中继，是最容易填错的一个值：
# 填 127.0.0.1 的话，客户端会拿"自己的 127.0.0.1"当中继地址 —— 本机自测能连，
# 别的机器一律连不上。没填就探测本机 IP，并明确要求确认。
if [ -z "$RELAY_HOST" ]; then
  RELAY_HOST="$(detect_ip)"
  if [ -n "$RELAY_HOST" ]; then
    warn "未指定 --relay-host，已探测到本机 IP：${RELAY_HOST}。请确认这是【客户端能访问到】的地址，否则改用 --relay-host <IP|域名>。"
  else
    die "无法自动探测本机 IP，请用 --relay-host <IP|域名> 指定客户端访问【中继】的地址。"
  fi
fi
case "$RELAY_HOST" in
  127.0.0.1|localhost|0.0.0.0)
    warn "RELAY_HOST=$RELAY_HOST：客户端会把中继地址解析成它自己，除本机外全都连不上。多机部署请填这台机器的实际 IP/域名。"
    ;;
esac

case "$ACTION" in
  key)     print_key || die "读不到公钥。hbbs 起来了吗？数据目录是 $DATA_DIR 吗？" ;;
  stop)    do_stop ;;
  rm)      do_stop ;;
  restart) do_restart ;;
  status)  do_status ;;
  *)       do_start ;;
esac
