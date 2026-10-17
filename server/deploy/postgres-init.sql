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
