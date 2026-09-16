"""可及性热力图聚合逻辑单测：direct/proxy/fail 三态与小时内优先级。"""
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


def _insert(hour: int, ok: bool, channel: str, date: str):
    db.conn().execute(
        "INSERT INTO probe_logs (ok, latency_ms, detail, channel, created_at)"
        " VALUES (?,?,?,?,?)",
        (int(ok), 10, "", channel, f"{date} {hour:02d}:07:00"),
    )
    db.conn().commit()


def test_heatmap_three_states(tmp_path):
    _use_tmp_db(tmp_path)
    try:
        today = datetime.now(models.TZ).strftime("%Y-%m-%d")
        _insert(6, True, "direct", today)
        _insert(7, True, "proxy", today)
        _insert(8, False, "direct", today)
        # 9 点混合：先失败后成功——fail 优先级最高，整格标红
        _insert(9, False, "direct", today)
        _insert(9, True, "direct", today)

        row = probe_row_today = None
        for row in models.probe_heatmap(days=7):
            if row["date"] == today:
                probe_row_today = row
        assert probe_row_today is not None
        cells = probe_row_today["cells"]
        assert cells[6] == "direct"
        assert cells[7] == "proxy"
        assert cells[8] == "fail"
        assert cells[9] == "fail"   # fail > direct
        assert cells[10] is None    # 无数据
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
