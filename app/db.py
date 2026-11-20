"""SQLite 连接与建表。所有状态持久化在 SQLite 中，服务重启后保持一致。"""
import os
import sqlite3

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "app.db")


def db_path() -> str:
    # 每次连接时读取环境变量，便于测试与 Docker 挂载覆盖
    return os.environ.get("EXHIBITION_DB", DEFAULT_DB)


def connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # isolation_level=None: 自动提交，由业务代码显式 BEGIN / BEGIN IMMEDIATE 控制事务
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")  # WAL 下安全，写延迟更低
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS exhibitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exhibition_no TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS originals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_no TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS exhibition_originals (
    exhibition_id INTEGER NOT NULL REFERENCES exhibitions(id),
    original_id INTEGER NOT NULL REFERENCES originals(id),
    PRIMARY KEY (exhibition_id, original_id)
);

CREATE TABLE IF NOT EXISTS licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_no TEXT NOT NULL UNIQUE,          -- 授权号唯一：重传不会覆盖，更不会恢复已撤回
    original_id INTEGER NOT NULL REFERENCES originals(id),
    commercial INTEGER NOT NULL DEFAULT 0,    -- 是否允许商用
    attribution TEXT NOT NULL DEFAULT '',     -- 署名
    revoked INTEGER NOT NULL DEFAULT 0,       -- 撤回为终态
    batch_label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS manifest_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exhibition_id INTEGER NOT NULL REFERENCES exhibitions(id),
    original_id INTEGER NOT NULL REFERENCES originals(id),
    license_id INTEGER REFERENCES licenses(id),   -- 旧版字段，保留兼容；当前选用改为读取时解析
    attribution TEXT NOT NULL DEFAULT '',          -- 编辑已确认的署名
    confirmed INTEGER NOT NULL DEFAULT 0,          -- 该署名是否已被编辑确认
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (exhibition_id, original_id)
);
CREATE INDEX IF NOT EXISTS idx_manifest_lookup
    ON manifest_items (exhibition_id, original_id, confirmed);
CREATE INDEX IF NOT EXISTS idx_licenses_pick
    ON licenses (original_id, commercial, revoked, attribution, id);
CREATE INDEX IF NOT EXISTS idx_links_original
    ON exhibition_originals (original_id, exhibition_id);

CREATE TABLE IF NOT EXISTS confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exhibition_id INTEGER NOT NULL REFERENCES exhibitions(id),
    editor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
