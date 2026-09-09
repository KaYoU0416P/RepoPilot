-- RepoPilot 业务表结构
--
-- 三张表对应三个业务实体：
--   runs                一次修复任务（同时兼任「任务队列」）
--   webhook_deliveries  外部触发的幂等去重台账
--   approvals           人工审批的审计流水
--
-- 注：标了 [PG-only] 的写法 MySQL 没有，是这次要重点学的。

-- ---------------------------------------------------------------- 枚举
-- [PG-only] MySQL 的 ENUM 是列级别的；PG 的 ENUM 是独立的类型，
-- 可以被多个表复用，改值要用 ALTER TYPE。
CREATE TYPE run_status AS ENUM (
    'queued',            -- 已入队，等 worker 领取
    'running',           -- worker 持有租约，Agent 执行中
    'pending_approval',  -- Agent 跑完且测试通过，等人批准
    'rejected',          -- 人工驳回
    'publishing',        -- 正在开 PR
    'published',         -- PR 已创建，终态
    'failed',            -- Agent 失败且重试用尽，终态
    'cancelled'          -- 人工取消，终态
);

CREATE TYPE trigger_source AS ENUM ('manual', 'github_issue', 'ci_failure');

-- ---------------------------------------------------------------- runs
CREATE TABLE runs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    status           run_status     NOT NULL DEFAULT 'queued',
    task             text           NOT NULL,
    repo_path        text           NOT NULL,
    source           trigger_source NOT NULL DEFAULT 'manual',
    -- 外部来源的引用，比如 "owner/repo#42"。用于回写评论和排查。
    external_ref     text,

    -- ---- Agent 产物 ----
    verdict          text,
    diff             text,
    final_report     text,
    -- [PG-only] jsonb 是二进制 JSON，可建索引、可用 -> ->> @> 查询。
    -- MySQL 的 JSON 类型能力弱很多。
    evaluation       jsonb,
    step_log         jsonb          NOT NULL DEFAULT '[]'::jsonb,
    -- [PG-only] 原生数组类型。MySQL 只能存 JSON 或另开一张表。
    files_changed    text[]         NOT NULL DEFAULT '{}',
    retry_count      int            NOT NULL DEFAULT 0,
    error            text,

    -- ---- 队列 / 租约（可靠性的核心）----
    attempts         int            NOT NULL DEFAULT 0,
    max_attempts     int            NOT NULL DEFAULT 3,
    locked_by        text,          -- 哪个 worker 拿着它
    lease_expires_at timestamptz,   -- 租约到期时间：worker 崩了就靠它回收

    created_at       timestamptz    NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz
);

-- [PG-only] 部分索引（partial index）：只给待领取的行建索引。
-- 队列表里 99% 的行是已完成的历史数据，全量索引又大又没用。
-- MySQL 完全没有这个功能。这是本次最值得记的 PG 特性之一。
CREATE INDEX idx_runs_claimable
    ON runs (created_at)
    WHERE status IN ('queued', 'running');

CREATE INDEX idx_runs_status_created ON runs (status, created_at DESC);
CREATE INDEX idx_runs_external_ref   ON runs (external_ref) WHERE external_ref IS NOT NULL;

-- --------------------------------------------------- webhook_deliveries
-- 幂等台账。GitHub 每次投递带一个唯一的 X-GitHub-Delivery header；
-- 网络抖动/重投时 delivery_id 不变，靠主键冲突挡掉重复。
CREATE TABLE webhook_deliveries (
    delivery_id  text        PRIMARY KEY,
    source       text        NOT NULL,
    event_type   text,
    run_id       uuid        REFERENCES runs(id) ON DELETE SET NULL,
    payload      jsonb,
    received_at  timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------ approvals
-- 审批流水。不覆盖写，每次决策追加一行 —— 审计要的是历史，不是当前值。
CREATE TABLE approvals (
    id         bigserial   PRIMARY KEY,
    run_id     uuid        NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    decision   text        NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decided_by text        NOT NULL,
    reason     text,
    decided_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_approvals_run ON approvals (run_id, decided_at DESC);
