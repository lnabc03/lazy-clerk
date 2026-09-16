"""可及性热力图聚合逻辑单测：direct/proxy/fail 三态与小时内最新生效。"""
import sqlite3
from datetime import datetime

from app import db, models
from app.config import settings


def _use_tmp_db(tmp_path):
    """把全局连接指向临时库。测试结束时调用方恢复。"""
    object.__setattr__(settings, "data_dir", str(tmp_path))  # frozen dataclass 绕过
    db._conn = None
    db.init()


def _restore_db():
    db._conn = None
    object.__setattr__(settings, "data_dir", "data")


def _insert(hour: int, ok: bool, channel: str, date: str, minute: int = 7):
    db.conn().execute(
        "INSERT INTO probe_logs (ok, latency_ms, detail, channel, created_at)"
        " VALUES (?,?,?,?,?)",
        (int(ok), 10, "", channel, f"{date} {hour:02d}:{minute:02d}:00"),
    )
    db.conn().commit()


def test_heatmap_three_states(tmp_path):
    _use_tmp_db(tmp_path)
    try:
        today = datetime.now(models.TZ).strftime("%Y-%m-%d")
        _insert(6, True, "direct", today)
        _insert(7, True, "proxy", today)
        _insert(8, False, "direct", today)
        # 9 点混合：先失败（自动探测）后成功（手动检测）——最新一条生效，标绿
        _insert(9, False, "direct", today, minute=7)
        _insert(9, True, "direct", today, minute=40)
        # 10 点混合：先成功后失败——最新一条是失败，标红
        _insert(10, True, "direct", today, minute=7)
        _insert(10, False, "direct", today, minute=50)

        probe_row_today = None
        for row in models.probe_heatmap(days=7):
            if row["date"] == today:
                probe_row_today = row
        assert probe_row_today is not None
        cells = probe_row_today["cells"]
        assert cells[6] == "direct"
        assert cells[7] == "proxy"
        assert cells[8] == "fail"
        assert cells[9] == "direct"  # 手动检测成功覆盖整点失败
        assert cells[10] == "fail"   # 最新一条是失败
        assert cells[11] is None     # 无数据
        assert len(cells) == 24
    finally:
        _restore_db()


def test_record_probe_roundtrip(tmp_path):
    _use_tmp_db(tmp_path)
    try:
        models.record_probe(True, 123, "HTTP 200", "proxy")
        row = models.latest_probe()
        assert row["ok"] == 1 and row["channel"] == "proxy" and row["latency_ms"] == 123
    finally:
        _restore_db()


def test_old_db_migrates_channel_column(tmp_path):
    """v0.1.3 及以前的存量 probe_logs 表（无 channel 列）升级后可用。"""
    tmp_path.mkdir(exist_ok=True)
    raw = sqlite3.connect(tmp_path / "lazy-clerk.db")
    raw.execute("CREATE TABLE probe_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ok INTEGER NOT NULL, latency_ms INTEGER, detail TEXT, created_at TEXT NOT NULL)")
    raw.execute("INSERT INTO probe_logs (ok, latency_ms, detail, created_at)"
                " VALUES (1, 10, 'HTTP 200', '2026-09-15 08:07:00')")
    raw.commit()
    raw.close()

    _use_tmp_db(tmp_path)
    try:
        row = models.latest_probe()
        assert row["channel"] == "direct"  # 迁移补默认值
        models.record_probe(True, 20, "HTTP 200", "proxy")  # 新写入正常
    finally:
        _restore_db()


# ---------- probe() 两阶段逻辑：直连失败 → 立即切代理复核，代理也失败才判不可达 ----------

import asyncio  # noqa: E402

from app.core import client  # noqa: E402


def _proxy_on():
    """假装配好了 mihomo 代理。"""
    object.__setattr__(settings, "proxy_url", "http://127.0.0.1:7890")
    object.__setattr__(settings, "mihomo_api", "http://127.0.0.1:9090")


def _proxy_off():
    object.__setattr__(settings, "proxy_url", "")
    object.__setattr__(settings, "mihomo_api", "")


async def _fake(value):
    return value


def test_probe_direct_ok_no_switch(monkeypatch):
    """直连成功：标 direct，不触发任何切换。"""
    _proxy_on()
    try:
        monkeypatch.setattr(client, "_probe_once",
                            lambda t=8.0: _fake(client.ProbeResult(True, "HTTP 200", 10)))
        monkeypatch.setattr(client, "current_channel", lambda: _fake("direct"))
        async def _switch(target):  # pragma: no cover - 不应被调用
            raise AssertionError("直连成功不应切换")
        monkeypatch.setattr(client, "mihomo_switch", _switch)
        r = asyncio.run(client.probe())
        assert r.ok and r.channel == "direct"
    finally:
        _proxy_off()


def test_probe_direct_fail_proxy_rescues(monkeypatch):
    """直连失败 → 主动切代理复核成功：标 proxy（蓝），不再误报红色。"""
    _proxy_on()
    try:
        calls = []
        async def _once(t=8.0):
            calls.append(1)
            ok = len(calls) > 1  # 第一次（直连）失败，第二次（代理）成功
            return client.ProbeResult(ok, "HTTP 200" if ok else "超时", 10)
        monkeypatch.setattr(client, "_probe_once", _once)
        monkeypatch.setattr(client, "current_channel", lambda: _fake("direct"))
        switched = []
        async def _switch(target):
            switched.append(target)
            return None
        monkeypatch.setattr(client, "mihomo_switch", _switch)
        r = asyncio.run(client.probe())
        assert r.ok and r.channel == "proxy"
        assert switched == [f"{settings.proxy_group}-auto"]
        assert len(calls) == 2
    finally:
        _proxy_off()


def test_probe_proxy_also_fail_is_red(monkeypatch):
    """直连失败、切代理复核也失败：判不可达（红）。"""
    _proxy_on()
    try:
        monkeypatch.setattr(client, "_probe_once",
                            lambda t=8.0: _fake(client.ProbeResult(False, "超时", 8000)))
        monkeypatch.setattr(client, "current_channel", lambda: _fake("direct"))
        monkeypatch.setattr(client, "mihomo_switch", lambda target: _fake(None))
        r = asyncio.run(client.probe())
        assert not r.ok and r.channel == "proxy"
    finally:
        _proxy_off()


def test_probe_already_on_proxy_fail_is_red(monkeypatch):
    """当前出口已是代理且失败：不再切换，直接判不可达。"""
    _proxy_on()
    try:
        monkeypatch.setattr(client, "_probe_once",
                            lambda t=8.0: _fake(client.ProbeResult(False, "超时", 8000)))
        monkeypatch.setattr(client, "current_channel", lambda: _fake("proxy"))
        async def _switch(target):  # pragma: no cover - 不应被调用
            raise AssertionError("已在代理出口不应再切换")
        monkeypatch.setattr(client, "mihomo_switch", _switch)
        r = asyncio.run(client.probe())
        assert not r.ok
    finally:
        _proxy_off()


def test_probe_no_proxy_direct_fail_is_red(monkeypatch):
    """未配置代理：直连失败即不可达，不尝试切换。"""
    _proxy_off()
    monkeypatch.setattr(client, "_probe_once",
                        lambda t=8.0: _fake(client.ProbeResult(False, "超时", 8000)))
    r = asyncio.run(client.probe())
    assert not r.ok and r.channel == "direct"


def test_probe_no_switch_on_first_attempt(monkeypatch):
    """switch_on_fail=False（定时探测/赛前守卫的首探）：直连失败不切代理，直接报失败。"""
    _proxy_on()
    try:
        monkeypatch.setattr(client, "_probe_once",
                            lambda t=8.0: _fake(client.ProbeResult(False, "超时", 8000)))
        async def _switch(target):  # pragma: no cover - 不应被调用
            raise AssertionError("首探不应切换出口")
        monkeypatch.setattr(client, "mihomo_switch", _switch)
        r = asyncio.run(client.probe(switch_on_fail=False))
        assert not r.ok and r.channel == "direct"
    finally:
        _proxy_off()
