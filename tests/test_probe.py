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


# ---------- probe() 双通道逻辑：直连/代理各测一次，按结果调整出口 ----------

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


def _patch_channels(monkeypatch, direct, proxy, retest=(None, ""), current="direct"):
    """mock 四条外部依赖：直连结果、代理当前节点结果、全量重测结果、当前出口。"""
    monkeypatch.setattr(client, "_probe_direct", lambda t=8.0: _fake(direct))
    monkeypatch.setattr(client, "_probe_proxy", lambda t=8.0: _fake(proxy))
    monkeypatch.setattr(client, "_proxy_group_retest", lambda t=8.0: _fake(retest))
    monkeypatch.setattr(client, "current_channel", lambda: _fake(current))


def _patch_switch(monkeypatch, calls: list):
    async def _switch(target):
        calls.append(target)
        return None
    monkeypatch.setattr(client, "mihomo_switch", _switch)


def test_probe_direct_ok_no_switch(monkeypatch):
    """直连通且已在直连出口：标 direct，不触发任何切换。"""
    _proxy_on()
    try:
        _patch_channels(monkeypatch, direct=(True, 10, "正常"), proxy=(561, "节点A"))
        calls = []
        _patch_switch(monkeypatch, calls)
        r = asyncio.run(client.probe())
        assert r.ok and r.channel == "direct" and r.latency_ms == 10
        assert "561ms" in r.detail  # 代理通道状态也呈现在摘要里
        assert calls == []
    finally:
        _proxy_off()


def test_probe_direct_recovers_switches_back(monkeypatch):
    """直连恢复（当前挂在代理上）：自动切回 DIRECT。"""
    _proxy_on()
    try:
        _patch_channels(monkeypatch, direct=(True, 10, "正常"), proxy=(561, "节点A"),
                        current="proxy")
        calls = []
        _patch_switch(monkeypatch, calls)
        r = asyncio.run(client.probe())
        assert r.ok and r.channel == "direct"
        assert calls == ["DIRECT"]
    finally:
        _proxy_off()


def test_probe_direct_fail_proxy_rescues(monkeypatch):
    """直连被封、代理当前节点可用：切代理，标 proxy。"""
    _proxy_on()
    try:
        _patch_channels(monkeypatch, direct=(False, 8000, "超时（疑似被防火墙拦截）"),
                        proxy=(561, "节点A"))
        calls = []
        _patch_switch(monkeypatch, calls)
        r = asyncio.run(client.probe())
        assert r.ok and r.channel == "proxy" and r.latency_ms == 561
        assert calls == [f"{settings.proxy_group}-auto"]
        assert "节点A" in r.detail
    finally:
        _proxy_off()


def test_probe_node_dead_retest_rescues(monkeypatch):
    """直连被封且代理当前节点也死：全量重测找到可用节点 → 切代理（即时自愈）。"""
    _proxy_on()
    try:
        _patch_channels(monkeypatch, direct=(False, 8000, "超时（疑似被防火墙拦截）"),
                        proxy=(None, "死节点"), retest=(489, "节点B"))
        calls = []
        _patch_switch(monkeypatch, calls)
        r = asyncio.run(client.probe())
        assert r.ok and r.channel == "proxy" and r.latency_ms == 489
        assert calls == [f"{settings.proxy_group}-auto"]
        assert "节点B" in r.detail
    finally:
        _proxy_off()


def test_probe_both_fail_is_down_no_switch(monkeypatch):
    """双通道全灭（全量重测也无可用节点）：判不可达，维持现状不切换。"""
    _proxy_on()
    try:
        _patch_channels(monkeypatch, direct=(False, 8000, "超时（疑似被防火墙拦截）"),
                        proxy=(None, "死节点"), retest=(None, ""), current="proxy")
        calls = []
        _patch_switch(monkeypatch, calls)
        r = asyncio.run(client.probe())
        assert not r.ok and r.channel == "proxy"  # channel 记录当前出口
        assert calls == []
    finally:
        _proxy_off()


def test_probe_no_proxy_direct_only(monkeypatch):
    """未配置代理：只测直连，代理通道函数不应被调用。"""
    _proxy_off()
    monkeypatch.setattr(client, "_probe_direct",
                        lambda t=8.0: _fake((False, 8000, "超时（疑似被防火墙拦截）")))
    async def _boom(t=8.0):  # pragma: no cover - 不应被调用
        raise AssertionError("未配置代理不应测代理通道")
    monkeypatch.setattr(client, "_probe_proxy", _boom)
    r = asyncio.run(client.probe())
    assert not r.ok and r.channel == "direct"
