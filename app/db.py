"""SQLite 初始化与连接管理。低并发场景，单连接 + 线程检查关闭即可。"""
from __future__ import annotations

import os
import sqlite3

from .config import settings

_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname   TEXT NOT NULL,
    account    TEXT NOT NULL UNIQUE,
    password   TEXT NOT NULL,
    sendkey    TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invite_codes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    code      TEXT,                    -- 明文邀请码（仅管理员可见，用于分发）
    code_hash TEXT NOT NULL UNIQUE,
    used_by   INTEGER,
    used_at   TEXT
);
CREATE TABLE IF NOT EXISTS sign_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    date       TEXT NOT NULL,
    period     TEXT NOT NULL,
    result     TEXT NOT NULL,
    message    TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sign_logs_user_date ON sign_logs(user_id, date);
CREATE TABLE IF NOT EXISTS probe_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ok         INTEGER NOT NULL,
    latency_ms INTEGER,
    detail     TEXT,
    channel    TEXT NOT NULL DEFAULT 'direct',  -- direct=直连 / proxy=代理出口
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_probe_logs_time ON probe_logs(created_at);
CREATE TABLE IF NOT EXISTS sign_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,             -- 场次：YYYY-MM-DD-am / YYYY-MM-DD-pm
    user_id    INTEGER NOT NULL REFERENCES users(id),
    round      INTEGER NOT NULL,          -- 该用户本场第几次尝试（1 起）
    result     TEXT NOT NULL,
    message    TEXT,
    node       TEXT,                      -- 本次尝试的出口：直连 / 代理节点名
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sign_attempts_run ON sign_attempts(run_id);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init() -> None:
    global _conn
    os.makedirs(settings.data_dir, exist_ok=True)
    _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    # 宿舍版签到进程与管理 Web 双进程共享库：WAL + 忙等，避免 database is locked
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.executescript(SCHEMA)
    _migrate()
    _conn.commit()


def _migrate() -> None:
    """存量库迁移。"""
    # invite_codes 补 code 明文字段（v0.3 起明文存储）
    cols = {r["name"] for r in _conn.execute("PRAGMA table_info(invite_codes)")}
    if "code" not in cols:
        _conn.execute("ALTER TABLE invite_codes ADD COLUMN code TEXT")
    # probe_logs 补 channel 出口字段（v0.1.4 起区分直连/代理）
    cols = {r["name"] for r in _conn.execute("PRAGMA table_info(probe_logs)")}
    if cols and "channel" not in cols:
        _conn.execute("ALTER TABLE probe_logs ADD COLUMN channel TEXT NOT NULL DEFAULT 'direct'")


def conn() -> sqlite3.Connection:
    if _conn is None:
        init()
    assert _conn is not None
    return _conn
