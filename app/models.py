"""数据模型与 CRUD：users / invite_codes / sign_logs / settings。"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import settings
from .db import conn

TZ = ZoneInfo(settings.tz)


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class User:
    id: int
    nickname: str
    account: str
    password: str
    sendkey: str | None
    enabled: bool
    created_at: str


def _to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"], nickname=row["nickname"], account=row["account"],
        password=row["password"], sendkey=row["sendkey"],
        enabled=bool(row["enabled"]), created_at=row["created_at"],
    )


# ---------- users ----------

def create_user(nickname: str, account: str, password: str, sendkey: str | None) -> int:
    cur = conn().execute(
        "INSERT INTO users (nickname, account, password, sendkey, enabled, created_at)"
        " VALUES (?,?,?,?,1,?)",
        (nickname, account, password, sendkey or None, now_str()),
    )
    conn().commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_user(user_id: int) -> User | None:
    row = conn().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _to_user(row) if row else None


def get_user_by_account(account: str) -> User | None:
    row = conn().execute("SELECT * FROM users WHERE account=?", (account,)).fetchone()
    return _to_user(row) if row else None


def list_users() -> list[User]:
    return [_to_user(r) for r in conn().execute("SELECT * FROM users ORDER BY id").fetchall()]


def set_user_enabled(user_id: int, enabled: bool) -> None:
    conn().execute("UPDATE users SET enabled=? WHERE id=?", (int(enabled), user_id))
    conn().commit()


def delete_user(user_id: int) -> None:
    conn().execute("DELETE FROM sign_logs WHERE user_id=?", (user_id,))
    conn().execute("DELETE FROM users WHERE id=?", (user_id,))
    conn().commit()


def update_sendkey(user_id: int, sendkey: str | None) -> None:
    conn().execute("UPDATE users SET sendkey=? WHERE id=?", (sendkey or None, user_id))
    conn().commit()


def update_password(user_id: int, password: str) -> None:
    conn().execute("UPDATE users SET password=? WHERE id=?", (password, user_id))
    conn().commit()


# ---------- invite_codes ----------

def add_invite_hashes(hashes: list[str]) -> int:
    """插入邀请码哈希（仅旧脚本兼容路径），返回实际新增数量（重复忽略）。"""
    cur = conn().executemany(
        "INSERT OR IGNORE INTO invite_codes (code_hash) VALUES (?)", [(h,) for h in hashes]
    )
    conn().commit()
    return cur.rowcount


def generate_invite() -> str:
    """生成 1 个邀请码：明文+哈希入库，返回明文。未使用的明文管理页常驻可见。"""
    import secrets
    import string

    alphabet = string.ascii_uppercase + string.digits
    code = "".join(secrets.choice(alphabet) for _ in range(10))
    conn().execute(
        "INSERT INTO invite_codes (code, code_hash) VALUES (?,?)", (code, sha256(code))
    )
    conn().commit()
    return code


def list_unused_invites() -> list[sqlite3.Row]:
    """未使用邀请码（含明文），管理页常驻展示。"""
    return conn().execute(
        "SELECT id, code FROM invite_codes WHERE used_by IS NULL ORDER BY id DESC"
    ).fetchall()


def invite_of_users() -> dict[int, str]:
    """各用户注册时使用的邀请码：{user_id: code 或 None（早期哈希数据）}"""
    rows = conn().execute(
        "SELECT used_by, code FROM invite_codes WHERE used_by IS NOT NULL"
    ).fetchall()
    return {r["used_by"]: r["code"] for r in rows}


def delete_invite(invite_id: int) -> bool:
    cur = conn().execute(
        "DELETE FROM invite_codes WHERE id=? AND used_by IS NULL", (invite_id,)
    )
    conn().commit()
    return cur.rowcount > 0


def cleanup_dead_invites() -> int:
    """删除"已使用但使用者已被删"的死邀请码。"""
    cur = conn().execute(
        "DELETE FROM invite_codes WHERE used_by IS NOT NULL"
        " AND used_by NOT IN (SELECT id FROM users)"
    )
    conn().commit()
    return cur.rowcount


def use_invite(code: str, user_id: int) -> bool:
    """校验并核销邀请码（单次使用）。成功返回 True。"""
    code = code.strip()
    row = conn().execute(
        "SELECT id FROM invite_codes WHERE code_hash=? AND used_by IS NULL",
        (sha256(code),),
    ).fetchone()
    if not row:
        return False
    conn().execute(
        "UPDATE invite_codes SET used_by=?, used_at=?, code=? WHERE id=?",
        (user_id, now_str(), code, row["id"]),
    )
    conn().commit()
    return True


def invite_remaining() -> int:
    row = conn().execute(
        "SELECT COUNT(*) AS c FROM invite_codes WHERE used_by IS NULL"
    ).fetchone()
    return row["c"]


# ---------- sign_logs ----------

def add_log(user_id: int, date: str, period: str, result: str, message: str = "") -> None:
    conn().execute(
        "INSERT INTO sign_logs (user_id, date, period, result, message, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (user_id, date, period, result, message[:500], now_str()),
    )
    conn().commit()


def logs_for_user(user_id: int, days: int = 7) -> list[sqlite3.Row]:
    """近 N 天日志，按操作时间倒序（新→旧）。"""
    since = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    return conn().execute(
        "SELECT * FROM sign_logs WHERE user_id=? AND date>=?"
        " ORDER BY created_at DESC, id DESC",
        (user_id, since),
    ).fetchall()


def today_results() -> dict[int, dict[str, sqlite3.Row]]:
    """今日各用户各时段的最新一条日志：{user_id: {period: row}}"""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    rows = conn().execute(
        "SELECT * FROM sign_logs WHERE date=? ORDER BY id", (today,)
    ).fetchall()
    out: dict[int, dict[str, sqlite3.Row]] = {}
    for r in rows:
        out.setdefault(r["user_id"], {})[r["period"]] = r  # 后写覆盖，保留最新
    return out


def recent_logs(limit: int = 50) -> list[sqlite3.Row]:
    """全账号最近日志（管理页用），按操作时间倒序。"""
    return conn().execute(
        "SELECT l.*, u.nickname FROM sign_logs l JOIN users u ON u.id = l.user_id"
        " ORDER BY l.created_at DESC, l.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def cleanup_logs(days: int = 90) -> int:
    cutoff = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    cur = conn().execute("DELETE FROM sign_logs WHERE date<?", (cutoff,))
    conn().commit()
    return cur.rowcount


# ---------- probe_logs（医院系统可及性探测） ----------

def record_probe(ok: bool, latency_ms: int | None, detail: str) -> None:
    conn().execute(
        "INSERT INTO probe_logs (ok, latency_ms, detail, created_at) VALUES (?,?,?,?)",
        (int(ok), latency_ms, detail[:200], now_str()),
    )
    conn().commit()


def latest_probe() -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM probe_logs ORDER BY id DESC LIMIT 1").fetchone()


def probe_heatmap(days: int = 14) -> list[dict]:
    """近 N 天 × 24 小时可及性网格（登录页热力图用）。

    返回按日期升序的行列表：{date, cells: [True|False|None] × 24}，
    True=该小时探测全部可达，False=有失败，None=无数据。
    """
    cutoff = (datetime.now(TZ) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = conn().execute(
        "SELECT created_at, ok FROM probe_logs WHERE created_at>=? ORDER BY created_at",
        (cutoff,)).fetchall()
    grid: dict[str, dict[int, bool]] = {}
    for r in rows:
        date, hour = r["created_at"][:10], int(r["created_at"][11:13])
        cell = grid.setdefault(date, {})
        cell[hour] = cell.get(hour, True) and bool(r["ok"])
    today = datetime.now(TZ).date()
    return [
        {"date": (d := (today - timedelta(days=i)).strftime("%Y-%m-%d")),
         "cells": [grid.get(d, {}).get(h) for h in range(24)]}
        for i in range(days - 1, -1, -1)
    ]


def cleanup_probes(days: int = 40) -> int:
    cutoff = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    cur = conn().execute("DELETE FROM probe_logs WHERE created_at<?", (cutoff,))
    conn().commit()
    return cur.rowcount


# ---------- settings ----------

def get_setting(key: str) -> str | None:
    row = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    conn().execute(
        "INSERT INTO settings (key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn().commit()
