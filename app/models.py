"""数据模型与 CRUD：users / invite_codes / sign_logs / settings。"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import settings
from .core.client import strip_node_flag
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
    conn().execute("DELETE FROM sign_attempts WHERE user_id=?", (user_id,))
    conn().execute("DELETE FROM users WHERE id=?", (user_id,))
    conn().commit()


def update_sendkey(user_id: int, sendkey: str | None) -> None:
    conn().execute("UPDATE users SET sendkey=? WHERE id=?", (sendkey or None, user_id))
    conn().commit()


def update_password(user_id: int, password: str) -> None:
    conn().execute("UPDATE users SET password=? WHERE id=?", (password, user_id))
    conn().commit()


# ---------- invite_codes ----------

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


# ---------- sign_attempts（定时签到逐轮尝试：管理页"日志分析"数据源） ----------

# 非定时任务产生的日志前缀（手动签到/状态检测），场次统计与稳定性判定一律剔除
_NON_AUTO_PREFIX = ("[检测]", "[手动]", "[管理员手动]")
_OK_RESULTS = ("success", "skipped", "no_schedule")


def add_attempt(user_id: int, date: str, period: str, round_no: int,
                result: str, message: str = "", node: str = "") -> None:
    """记录一轮签到尝试。run_id = 日期-时段，唯一标识一场定时任务。"""
    conn().execute(
        "INSERT INTO sign_attempts (run_id, user_id, round, result, message, node, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (f"{date}-{period}", user_id, round_no, result, message[:500], node[:100], now_str()),
    )
    conn().commit()


def cleanup_attempts(days: int = 90) -> int:
    cutoff = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    cur = conn().execute("DELETE FROM sign_attempts WHERE run_id<?", (cutoff,))
    conn().commit()
    return cur.rowcount


def service_summary() -> dict:
    """一句话摘要（热力图卡内，登录页/管理页共用）：每次渲染实时计算。

    稳定日 = 当天两个时段都有定时任务日志且无最终失败。当天尚未跑完
    （下午场未出日志）不计入也不中断，从昨天往前数；当天已有失败则归零。
    """
    row = conn().execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT user_id) u FROM sign_logs WHERE result='success'"
    ).fetchone()
    days: dict[str, dict] = {}
    for r in conn().execute("SELECT date, period, result, message FROM sign_logs"):
        if (r["message"] or "").startswith(_NON_AUTO_PREFIX):
            continue
        d = days.setdefault(r["date"], {"periods": set(), "failed": False})
        d["periods"].add(r["period"])
        if r["result"] == "failed":
            d["failed"] = True

    def green(date: str) -> bool:
        d = days.get(date)
        return bool(d) and not d["failed"] and d["periods"] == {"am", "pm"}

    today = datetime.now(TZ).date()
    t = days.get(today.strftime("%Y-%m-%d"))
    if t and t["failed"]:
        streak = 0
        start = -1  # 已归零，起点无所谓
    else:
        streak = 1 if green(today.strftime("%Y-%m-%d")) else 0
        start = 1  # 从昨天往前数
    i = start
    while i >= 0 and green((today - timedelta(days=i)).strftime("%Y-%m-%d")):
        streak += 1
        i += 1
    return {"users": row["u"], "signs": row["n"], "stable_days": streak}


def session_analysis(days: int = 7) -> list[dict]:
    """近 N 天签到场次分析（管理页"日志分析"卡），按场次新→旧。

    主数据源 sign_attempts（逐轮落库）：每场给出轮次、每轮完成人数、
    成功出口节点、每轮成败名单、最终失败名单。落库前的历史场次回落
    sign_logs 终态汇总（轮次/节点无数据，显示 —）。
    """
    cutoff = (datetime.now(TZ) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    names = {u.id: u.nickname for u in list_users()}

    def name(uid: int) -> str:
        return names.get(uid, f"已删用户#{uid}")

    sessions: dict[str, dict] = {}
    for r in conn().execute(
            "SELECT * FROM sign_attempts WHERE run_id>=? ORDER BY run_id DESC, id",
            (cutoff,)).fetchall():
        sessions.setdefault(r["run_id"], []).append(r)

    out: list[dict] = []
    for rid, attempts in sessions.items():
        by_user: dict[int, list] = {}
        for a in attempts:
            by_user.setdefault(a["user_id"], []).append(a)
        rounds = max(a["round"] for a in attempts)
        round_detail = {k: {"round": k, "ok": [], "fail": []} for k in range(1, rounds + 1)}
        pace_map: dict[int, int] = {}
        nodes: list[str] = []
        failed: list[dict] = []
        manual: list[str] = []
        ok_total = 0
        for uid, atts in by_user.items():
            for a in atts:
                rd = round_detail[a["round"]]
                (rd["ok"] if a["result"] in _OK_RESULTS else rd["fail"]).append(name(uid))
            final = atts[-1]
            if final["result"] in _OK_RESULTS:
                ok_total += 1
                pace_map[final["round"]] = pace_map.get(final["round"], 0) + 1
                # 成功出口：优先 success 尝试的节点，全 skipped/no_schedule 的场次
                # 回落到终态 OK 尝试的节点。展示时剥掉订阅名的国旗前缀
                if final["node"] and (final["result"] == "success" or not nodes):
                    nd = strip_node_flag(final["node"])
                    if nd not in nodes:
                        nodes.append(nd)
            elif final["result"] == "manual":
                manual.append(name(uid))
            else:
                failed.append({"name": name(uid), "msg": final["message"] or ""})
        out.append({
            "run_id": rid, "date": rid[:10], "period": rid[11:],
            "rounds": rounds, "users": len(by_user), "ok": ok_total,
            "pace": [(k, pace_map[k]) for k in sorted(pace_map)],
            "nodes": nodes,
            "detail": [round_detail[k] for k in range(1, rounds + 1)],
            "failed": failed, "manual": manual,
            "failed_title": "；".join(f"{f['name']}: {f['msg']}" for f in failed),
        })

    # 历史回落：sign_logs 有终态但无逐轮数据的场次（逐轮落库上线前的日子）
    fallback: dict[str, dict] = {}
    for r in conn().execute(
            "SELECT * FROM sign_logs WHERE date>=? ORDER BY id", (cutoff,)).fetchall():
        if (r["message"] or "").startswith(_NON_AUTO_PREFIX):
            continue
        rid = f'{r["date"]}-{r["period"]}'
        if rid not in sessions:
            fallback.setdefault(rid, {})[r["user_id"]] = r  # 后写覆盖 = 终态
    for rid, finals in fallback.items():
        failed = [{"name": name(uid), "msg": r["message"] or ""}
                  for uid, r in finals.items() if r["result"] == "failed"]
        manual = [name(uid) for uid, r in finals.items() if r["result"] == "manual"]
        ok = sum(1 for r in finals.values() if r["result"] in _OK_RESULTS)
        out.append({
            "run_id": rid, "date": rid[:10], "period": rid[11:],
            "rounds": None, "users": len(finals), "ok": ok,
            "pace": [], "nodes": [], "detail": [],
            "failed": failed, "manual": manual,
            "failed_title": "；".join(f"{f['name']}: {f['msg']}" for f in failed),
        })

    period_name = {"am": "上午", "pm": "下午"}
    for s in out:
        s["label"] = f"{s['date'][5:]} {period_name.get(s['period'], s['period'])}"
    out.sort(key=lambda s: s["run_id"], reverse=True)
    return out


# ---------- probe_logs（医院系统可及性探测） ----------

def record_probe(ok: bool, latency_ms: int | None, detail: str, channel: str = "direct") -> None:
    conn().execute(
        "INSERT INTO probe_logs (ok, latency_ms, detail, channel, created_at) VALUES (?,?,?,?,?)",
        (int(ok), latency_ms, detail[:200], channel, now_str()),
    )
    conn().commit()


def latest_probe() -> sqlite3.Row | None:
    return conn().execute(
        "SELECT * FROM probe_logs ORDER BY id DESC LIMIT 1").fetchone()


def probe_heatmap(days: int = 7) -> list[dict]:
    """近 N 天 × 24 小时可及性网格（登录页热力图用）。

    返回按日期升序的行列表：{date, cells: [str|None] × 24}，
    格子为 "direct"（直连正常）/ "proxy"（代理正常）/ "fail" / None（无数据）。
    一小时内多次探测取最新一条——手动检测后热力图立即反映当前真实状态。
    """
    cutoff = (datetime.now(TZ) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = conn().execute(
        "SELECT created_at, ok, channel FROM probe_logs WHERE created_at>=? ORDER BY created_at",
        (cutoff,)).fetchall()

    def state(row) -> str:
        if not row["ok"]:
            return "fail"
        return row["channel"] if row["channel"] in ("direct", "proxy") else "direct"

    grid: dict[str, dict[int, str]] = {}
    for r in rows:  # 按时间升序，后到者覆盖，小时内最新一条生效
        date, hour = r["created_at"][:10], int(r["created_at"][11:13])
        grid.setdefault(date, {})[hour] = state(r)
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
