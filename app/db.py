"""SQLite 持久层：连接管理、表结构。

所有连接均使用 autocommit(isolation_level=None),写操作通过显式
BEGIN IMMEDIATE / COMMIT 控制事务边界,保证:
  - 增量确认 = increments 日志 + results 应用,同一事务提交,崩溃不丢;
  - 全量装载的分块写入与进度记录在同一事务,断点可续;
  - 基线切换(合并+替换)在单个事务内完成,读接口只见切换前或切换后。
"""
import os
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,              -- 'full' 全量 | 'inc' 增量
    filename        TEXT NOT NULL,              -- 服务器上保存的 CSV 路径
    orig_name       TEXT,                       -- 原始文件名
    status          TEXT NOT NULL,              -- full: loading|staging_ready|done|failed
                                                -- inc : processing|done|failed
    total_bytes     INTEGER NOT NULL DEFAULT 0,
    processed_bytes INTEGER NOT NULL DEFAULT 0, -- 已处理的文件偏移(断点续传游标)
    total_rows      INTEGER NOT NULL DEFAULT 0,
    applied_rows    INTEGER NOT NULL DEFAULT 0,
    error_rows      INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    start_seq       INTEGER NOT NULL DEFAULT 0, -- 全量装载开始时的增量序号水位
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 同一时刻只允许一个未完成的全量装载(数据库级强制)
CREATE UNIQUE INDEX IF NOT EXISTS one_active_full_load
    ON batches(kind) WHERE kind = 'full' AND status IN ('loading', 'staging_ready');

-- 正式结果表:撤回的行保留为墓碑(active=0),记录撤回修订号,
-- 使“旧修订不得恢复结果”的规则在基线切换后仍然成立。
CREATE TABLE IF NOT EXISTS results (
    report_no     TEXT NOT NULL,
    item          TEXT NOT NULL,
    result        TEXT,
    revision      INTEGER NOT NULL DEFAULT 0,
    withdrawn_rev INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (report_no, item)
);

-- 全量装载暂存区(单装载独占,与正式表同构)
CREATE TABLE IF NOT EXISTS results_staging (
    report_no     TEXT NOT NULL,
    item          TEXT NOT NULL,
    result        TEXT,
    revision      INTEGER NOT NULL DEFAULT 0,
    withdrawn_rev INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (report_no, item)
);

-- 已确认增量日志:seq 为全局单调序号,切换时按 seq 回放合入
CREATE TABLE IF NOT EXISTS increments (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    report_no   TEXT NOT NULL,
    item        TEXT NOT NULL,
    action      TEXT NOT NULL,                  -- 'correct' | 'withdraw'
    result      TEXT,
    revision    INTEGER NOT NULL,
    effect      TEXT,                           -- applied|stale|withdrawn|recorded
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 交付包:冻结某报告号下一组必需项目的当前正式结果与修订号。
-- 包内任一项目的结果值/修订号/有效性被增量确认或基线切换改变时,
-- 整包在同一事务内永久停用(status 为状态位,不随结果回改而复活)。
CREATE TABLE IF NOT EXISTS packages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_no       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active', -- active | disabled
    item_count      INTEGER NOT NULL,
    disabled_reason TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    disabled_at     TEXT
);

-- 包内冻结的项目快照(建包时的正式结果值与修订号)
CREATE TABLE IF NOT EXISTS package_items (
    package_id INTEGER NOT NULL REFERENCES packages(id),
    line_no    INTEGER NOT NULL,                -- 建包时项目的录入顺序
    item       TEXT NOT NULL,
    result     TEXT NOT NULL,                   -- 冻结的结果值
    revision   INTEGER NOT NULL,                -- 冻结的修订号
    PRIMARY KEY (package_id, item)
);
"""


def db_path(data_dir):
    return os.path.join(data_dir, "lims.db")


def connect(data_dir):
    """新建一个连接(每线程各自使用)。autocommit + 显式事务。"""
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(db_path(data_dir), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # 读写并发:装载/切换期间读接口不阻塞
    conn.execute("PRAGMA busy_timeout=30000")    # 写者排队而不是立即报锁
    conn.execute("PRAGMA synchronous=FULL")      # 已确认事务断电不丢
    return conn


def init_schema(data_dir):
    conn = connect(data_dir)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_db():
    """请求作用域连接(Flask g)。"""
    if "db" not in g:
        g.db = connect(current_app.config["DATA_DIR"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()
