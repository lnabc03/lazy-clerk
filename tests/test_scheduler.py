"""定时探测任务 _probe_job 的状态机单测：up/down、判红迟滞、告警去重、恢复通知。

判红迟滞（2026-10-07 起）：初测红后 5 分钟内 3 次复核全红才告警——
分钟级风控抖动（实测 3-16 分钟）不再产生告警对；确认期间其他入口
实测转绿则以新鲜事实为准中止判红。"""
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
WARMUP_RED = ProbeResult(False, "认证不通；缓冲期内暂不切换", 0, "proxy", warmup=True)


def test_down_confirmed_alerts_once(monkeypatch, tmp_path):
    """首次判红：5 分钟内 3 次复核全红才告警并置 down；下一小时仍红不重复告警。"""
    _use_tmp_db(tmp_path)
    try:
        pushed = []
        calls = _patch(monkeypatch, [RED, RED, RED, RED], pushed)
        asyncio.run(scheduler._probe_job())
        assert len(calls) == 4  # 初测 + 3 次复核
        assert any("持续不通" in t for t in pushed)
        assert models.get_setting("probe_last_state") == "down"

        pushed.clear()
        calls.clear()
        asyncio.run(scheduler._probe_job())
        assert len(calls) == 1  # 已知 down，不复核
        assert not pushed     # 不重复告警
    finally:
        _restore_db()


def test_down_flaky_no_alert(monkeypatch, tmp_path):
    """初测红、复核绿：分钟级抖动不误报，状态为 up。"""
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


def test_late_flap_no_alert(monkeypatch, tmp_path):
    """复核两次仍红、第三次转绿（抖动跨越约 3 分钟）：不告警。"""
    _use_tmp_db(tmp_path)
    try:
        pushed = []
        calls = _patch(monkeypatch, [RED, RED, RED, OK], pushed)
        asyncio.run(scheduler._probe_job())
        assert len(calls) == 4
        assert not pushed
        assert models.get_setting("probe_last_state") == "up"
    finally:
        _restore_db()


def test_fresh_green_from_other_source_aborts_alert(monkeypatch, tmp_path):
    """判红确认期间其他入口（手动探测/门控）实测转绿：以新鲜事实为准，不发过期告警。"""
    _use_tmp_db(tmp_path)
    try:
        pushed = []
        calls = []

        async def fake_probe(timeout=8.0):
            calls.append(1)
            if len(calls) == 4:
                # 最后一次复核前，另一入口实测转绿并已落库（模拟手动探测）
                models.record_probe(True, 300, "认证 100ms；考勤 200ms", "proxy")
            return RED

        async def fake_notify(title, msg, sendkey=None):
            pushed.append(title)

        async def fake_sleep(seconds):
            pass

        monkeypatch.setattr("app.core.client.probe", fake_probe)
        monkeypatch.setattr("app.core.notify.notify", fake_notify)
        monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)
        asyncio.run(scheduler._probe_job())
        assert len(calls) == 4
        assert not pushed
        assert models.get_setting("probe_last_state") == "up"
    finally:
        _restore_db()


def test_warmup_during_confirm_aborts(monkeypatch, tmp_path):
    """复核撞上换节点缓冲期：暖机噪声不构成证据，本次判红中止、状态不动。"""
    _use_tmp_db(tmp_path)
    try:
        pushed = []
        calls = _patch(monkeypatch, [RED, WARMUP_RED], pushed)
        asyncio.run(scheduler._probe_job())
        assert len(calls) == 2
        assert not pushed
        assert models.get_setting("probe_last_state") is None  # 状态未被污染
    finally:
        _restore_db()


def test_overlap_lock_skips(monkeypatch, tmp_path):
    """上一次判红复核进行中（锁占用）：本次整点探测直接跳过，不重复探测。"""
    _use_tmp_db(tmp_path)
    try:
        pushed = []
        calls = _patch(monkeypatch, [RED], pushed)

        async def hold_and_run():
            async with scheduler._probe_lock:
                await scheduler._probe_job()

        asyncio.run(hold_and_run())
        assert not calls
        assert not pushed
    finally:
        _restore_db()


def test_recovery_notifies(monkeypatch, tmp_path):
    """down 后恢复：单次绿即推恢复通知（好消息宜快）并置 up。"""
    _use_tmp_db(tmp_path)
    try:
        models.set_setting("probe_last_state", "down")
        pushed = []
        _patch(monkeypatch, [OK], pushed)
        asyncio.run(scheduler._probe_job())
        assert any("已恢复" in t for t in pushed)
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


def test_down_alert_suppressed_in_sign_window(monkeypatch, tmp_path):
    """签到窗口内判红：状态置 down 但不告警（赛前守卫/门控的取消广播负责通知）；
    窗口外恢复时恢复通知照常。"""
    _use_tmp_db(tmp_path)
    try:
        pushed = []
        calls = _patch(monkeypatch, [RED, RED, RED, RED], pushed)
        monkeypatch.setattr(scheduler, "_in_sign_window", lambda: True)
        asyncio.run(scheduler._probe_job())
        assert len(calls) == 4  # 初测 + 3 次复核照常
        assert not pushed       # 窗口内不告警
        assert models.get_setting("probe_last_state") == "down"

        # 恢复通知不受窗口影响（此处仍在窗口内也应通知——好消息不压）
        monkeypatch.setattr(scheduler, "_in_sign_window", lambda: False)
        pushed.clear()
        _patch(monkeypatch, [OK], pushed)
        asyncio.run(scheduler._probe_job())
        assert any("已恢复" in t for t in pushed)
        assert models.get_setting("probe_last_state") == "up"
    finally:
        _restore_db()
