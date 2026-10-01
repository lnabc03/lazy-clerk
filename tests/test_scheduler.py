"""定时探测任务 _probe_job 的状态机单测：up/down、复核、告警去重、恢复通知。"""
import asyncio

import pytest

from app import db, models
from app.config import settings
from app.core.client import ProbeResult

pytest.importorskip("apscheduler")  # 仅容器内安装；宿主机跑测试时跳过本文件
from server import scheduler  # noqa: E402


def _use_tmp_db(tmp_path):
    object.__setattr__(settings, "data_dir", str(tmp_path))  # frozen dataclass 绕过
    db._conn = None
    db.init()


def _restore_db():
    db._conn = None
    object.__setattr__(settings, "data_dir", "data")


def _patch(monkeypatch, results, pushed):
    """fake probe 按队列依次返回结果；fake notify 记录标题。"""
    calls = []

    async def fake_probe(timeout=8.0):
        calls.append(1)
        return results[min(len(calls) - 1, len(results) - 1)]

    async def fake_notify(title, msg, sendkey=None):
        pushed.append(title)

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr("app.core.client.probe", fake_probe)
    monkeypatch.setattr("app.core.notify.notify", fake_notify)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)
    return calls


OK = ProbeResult(True, "认证 100ms；考勤 200ms（节点A）", 200, "proxy")
RED = ProbeResult(False, "认证 100ms；考勤不通；全量重测无可用节点", 0, "proxy")


def test_down_confirmed_alerts_once(monkeypatch, tmp_path):
    """首次判红：20 秒复核仍红才告警并置 down；下一小时仍红不重复告警。"""
    _use_tmp_db(tmp_path)
    try:
        pushed = []
        calls = _patch(monkeypatch, [RED, RED], pushed)
        asyncio.run(scheduler._probe_job())
        assert len(calls) == 2  # 初测 + 复核
        assert any("无法访问" in t for t in pushed)
        assert models.get_setting("probe_last_state") == "down"

        pushed.clear()
        calls.clear()
        asyncio.run(scheduler._probe_job())
        assert len(calls) == 1  # 已知 down，不复核
        assert not pushed     # 不重复告警
    finally:
        _restore_db()


def test_down_flaky_no_alert(monkeypatch, tmp_path):
    """初测红、复核绿：抖动不误报，状态为 up。"""
    _use_tmp_db(tmp_path)
    try:
        pushed = []
        calls = _patch(monkeypatch, [RED, OK], pushed)
        asyncio.run(scheduler._probe_job())
        assert len(calls) == 2
        assert not pushed
        assert models.get_setting("probe_last_state") == "up"
    finally:
        _restore_db()


def test_recovery_notifies(monkeypatch, tmp_path):
    """down 后恢复：推恢复通知并置 up。"""
    _use_tmp_db(tmp_path)
    try:
        models.set_setting("probe_last_state", "down")
        pushed = []
        _patch(monkeypatch, [OK], pushed)
        asyncio.run(scheduler._probe_job())
        assert any("恢复可达" in t for t in pushed)
        assert models.get_setting("probe_last_state") == "up"
    finally:
        _restore_db()


def test_legacy_state_value_treated_as_up(monkeypatch, tmp_path):
    """存量 probe_last_state=direct/proxy（v0.1.x 口径）：按 up 处理，不误报恢复。"""
    _use_tmp_db(tmp_path)
    try:
        models.set_setting("probe_last_state", "proxy")
        pushed = []
        _patch(monkeypatch, [OK], pushed)
        asyncio.run(scheduler._probe_job())
        assert not pushed
        assert models.get_setting("probe_last_state") == "up"
    finally:
        _restore_db()
