-- =============================================================================
-- hbbs PostgreSQL 后端 / peer 表结构
-- =============================================================================
-- 说明：
--   正常情况下**不需要手动执行本文件**——hbbs 启动时会自动调用 create_tables()
--   建表与建索引（见 server/src/src/database.rs 的 PG_DDL_STATEMENTS）。
--
--   本文件的用途有两个：
--   1. 交给 DBA 审阅，或由运维在受管 PostgreSQL 实例上预建 schema；
--   2. 作为类型映射的对照表，便于写报表 / 审计查询。
--
-- 与 SQLite 版的差异（务必留意）：
--   blob        -> bytea
--   tinyint     -> smallint
--   datetime    -> timestamptz
--   WITHOUT ROWID 是 SQLite 专有语法，PostgreSQL 没有，直接去掉
--   user 是 PostgreSQL 保留字，必须写成 "user"
-- =============================================================================

create table if not exists peer (
    guid       bytea        primary key not null,   -- 内部 UUID v4
    id         varchar(100) not null,               -- 客户端可见的数字 ID
    uuid       bytea        not null,               -- 硬件绑定标识
    pk         bytea        not null,               -- 客户端 ed25519 公钥
    created_at timestamptz  not null default now(),
    "user"     bytea,                               -- 预留的用户关联字段
    status     smallint,                            -- 0 = 禁用
    note       varchar(300),
    info       text         not null                -- PeerInfo 的 JSON 序列化
);

create unique index if not exists index_peer_id         on peer (id);
create        index if not exists index_peer_user       on peer ("user");
create        index if not exists index_peer_created_at on peer (created_at);
create        index if not exists index_peer_status     on peer (status);

-- ---------------------------------------------------------------------------
-- server_config：服务端自身的键值状态
-- ---------------------------------------------------------------------------
-- 目前托管的是 id_ed25519 密钥对。多实例部署时所有 hbbs 读同一行，
-- 从而共享同一把钥匙（否则每台各生成一把，客户端 ID 会来回变）。
--
-- ⚠️ 私钥以 base64 明文存于此处，因此这个库的访问权限等同于
--    服务端私钥的访问权限。
-- ---------------------------------------------------------------------------
create table if not exists server_config (
    key        varchar(100) primary key not null,
    value      text         not null,
    updated_at timestamptz  not null default now()
);

-- ---------------------------------------------------------------------------
-- connection_log：连接审计
-- ---------------------------------------------------------------------------
-- event 取值：
--   register_pk          客户端注册 / 更新公钥（ID 与设备绑定）
--   punch_hole           A 请求连 B，已转发给 B（via_relay 表示是否强制中继）
--   punch_hole_failed    请求被拒绝（detail 说明原因）
--
-- detail 取值（punch_hole_failed）：
--   license_mismatch / offline / id_not_exist
--
-- 说明：from_id 在打洞事件里通常是空的 —— 协议里 PunchHoleRequest 只带
-- 被连接方的 id，发起方此刻只有一个 socket 地址。要还原"哪个 ID 连了哪个 ID"，
-- 用下面的关联查询把 IP 反查成 ID。
-- ---------------------------------------------------------------------------
create table if not exists connection_log (
    id        bigserial    primary key,
    ts        timestamptz  not null default now(),
    event     varchar(32)  not null,
    from_id   varchar(253),
    from_ip   varchar(64),
    to_id     varchar(253),
    to_ip     varchar(64),
    via_relay boolean,
    detail    text
);

create index if not exists index_connection_log_ts    on connection_log (ts);
create index if not exists index_connection_log_event on connection_log (event);

-- ---------------------------------------------------------------------------
-- 常用查询样例
-- ---------------------------------------------------------------------------
-- 设备总数：
--   select count(*) from peer;
--
-- 最近注册的 20 台设备（guid 以 bytea 存储，看原始值可再 encode）：
--   select encode(guid, 'hex'), id, created_at from peer order by created_at desc limit 20;
--
-- 被禁用的设备：
--   select id, created_at from peer where status = 0;
--
-- 最近活跃 IP（从 info 的 JSON 里取）：
--   select id, info::json->>'ip' as ip from peer limit 50;
--
-- 服务端密钥当前托管在哪（有行即已托管）：
--   select key, updated_at from server_config where key like 'server.%';
--
-- 最近 100 条连接记录（把发起方 IP 反查成 ID）：
--   select c.ts, c.event, c.from_ip,
--          coalesce(c.from_id, p.id) as from_id_resolved,
--          c.to_id, c.via_relay, c.detail
--     from connection_log c
--     left join peer p on p.info::json->>'ip' = c.from_ip
--    order by c.ts desc limit 100;
--
-- 各事件计数（近 24 小时）：
--   select event, count(*) from connection_log
--    where ts > now() - interval '24 hours' group by event;
--
-- 走中继的比例（近 24 小时，只看成功转发）：
--   select via_relay, count(*) from connection_log
--    where event = 'punch_hole' and ts > now() - interval '24 hours'
--    group by via_relay;
