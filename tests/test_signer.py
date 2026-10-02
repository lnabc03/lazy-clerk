"""签到判定逻辑单测：find_target_row / decide（设计稿 4.1）。"""
import asyncio
from datetime import time

from app import models
from app.core import signer
from app.core.client import ProbeResult


def _row(status=None, day_off=0, date="2026-09-11", time_name="上午", rid=1):
    return {"ID": rid, "WeekDate": date, "TimeName": time_name,
            "SignInStatus": status, "DayOff": day_off}


async def _ok_probe(timeout=8.0, switch_on_fail=True):
    return ProbeResult(True, "HTTP 200", 10)


async def _ok_gate(ttl=240.0, on_fresh=None):
    """门控探针假：始终可达。"""
    return ProbeResult(True, "认证 10ms；考勤 20ms", 20)


async def _red_gate(ttl=240.0, on_fresh=None):
    """门控探针假：持续不可达（模拟医院关闭考勤端口）。"""
    return ProbeResult(False, "认证 450ms；考勤超时；全量重测无可用节点", 0)


def test_find_target_row():
    rows = [_row(rid=1), _row(time_name="下午", rid=2)]
    assert signer.find_target_row(rows, "2026-09-11", "am")["ID"] == 1
    assert signer.find_target_row(rows, "2026-09-11", "pm")["ID"] == 2
    assert signer.find_target_row(rows, "2026-09-12", "am") is None


def test_decide_signable():
    assert signer.decide(_row(status=None)).result == "sign"
    assert signer.decide(_row(status=0)).result == "sign"


def test_decide_already_signed():
    assert signer.decide(_row(status=1)).result == signer.RESULT_SKIPPED
    assert signer.decide(_row(status=10)).result == signer.RESULT_SKIPPED


def test_decide_leave_status():
    """借假（-1）＝已批准的假 → no_schedule 终态 + 告知本人；迟到等仍判需人工。"""
    o = signer.decide(_row(status=-1))
    assert o.result == signer.RESULT_NO_SCHEDULE
    assert o.notify and not o.retryable


def test_decide_manual():
    assert signer.decide(_row(status=-3)).result == signer.RESULT_MANUAL   # 迟到
    assert signer.decide(_row(status=None, day_off=1)).result == signer.RESULT_MANUAL  # 补签


def test_classify_rejection_leave_is_terminal():
    """请假/休假类拒绝 → no_schedule 终态：不重试不告警（实测文案「当天已请假，不可签到」）。"""
    o = signer.classify_rejection("当天已请假，不可签到")
    assert o.result == signer.RESULT_NO_SCHEDULE
    assert not o.retryable
    assert o.notify  # 请假需推送告知用户本人


def test_leave_rejection_pushes_user(monkeypatch):
    """请假类拒绝在重试编排中触发用户推送。"""
    pushed = []

    async def fake_push(user, period, message):
        pushed.append(message)

    async def fake_once(user, period):
        return signer.classify_rejection("当天已请假，不可签到")

    monkeypatch.setattr(signer, "push_no_sign_needed", fake_push)
    monkeypatch.setattr(signer, "sign_user_once", fake_once)
    outcome = asyncio.run(signer.sign_user_with_retry(
        _user(1, "a"), "am", log_fn=lambda *a: None))
    assert outcome.result == signer.RESULT_NO_SCHEDULE
    assert pushed and "请假" in pushed[0]


def test_plain_no_schedule_stays_silent(monkeypatch):
    """无排班的 no_schedule 是日常状态，不推送。"""
    async def fake_push(user, period, message):
        raise AssertionError("无排班不应推送")

    async def fake_once(user, period):
        return signer.SignOutcome(signer.RESULT_NO_SCHEDULE, "今日无该时段排班，无需签到")

    monkeypatch.setattr(signer, "push_no_sign_needed", fake_push)
    monkeypatch.setattr(signer, "sign_user_once", fake_once)
    outcome = asyncio.run(signer.sign_user_with_retry(
        _user(1, "a"), "am", log_fn=lambda *a: None))
    assert outcome.result == signer.RESULT_NO_SCHEDULE


def test_classify_rejection_unknown_is_retryable():
    """未识别的拒绝 → 可重试失败（可能是临时故障）。"""
    o = signer.classify_rejection("系统繁忙，请稍后再试")
    assert o.result == signer.RESULT_FAILED
    assert o.retryable


def _user(uid, account):
    return models.User(id=uid, nickname=account, account=account, password="p",
                       sendkey=None, enabled=True, created_at="")


def test_sign_all_slow_account_does_not_block_others(monkeypatch):
    """回归（v0.1.0 缺陷）：前序账号进入重试循环不得阻塞后续账号。"""
    monkeypatch.setattr(models, "list_users", lambda: [_user(1, "a"), _user(2, "b")])
    monkeypatch.setattr(signer.random, "uniform", lambda lo, hi: 0)
    monkeypatch.setattr(signer, "probe", _ok_probe)

    b_done = asyncio.Event()

    async def fake_with_retry(user, period, log_fn=None):
        if user.account == "a":
            await b_done.wait()  # a 卡在重试中，直到 b 完成才返回
        else:
            b_done.set()
        return signer.SignOutcome(signer.RESULT_SUCCESS, "ok")

    monkeypatch.setattr(signer, "sign_user_with_retry", fake_with_retry)

    # 顺序执行的话这里必然死锁，5 秒超时即失败；并行则立即通过
    asyncio.run(asyncio.wait_for(signer.sign_all("am"), timeout=5))
    assert b_done.is_set()


def test_sign_all_crash_isolated(monkeypatch):
    """单账号流程崩溃不影响其他账号执行。"""
    monkeypatch.setattr(models, "list_users", lambda: [_user(1, "a"), _user(2, "b")])
    monkeypatch.setattr(signer.random, "uniform", lambda lo, hi: 0)
    monkeypatch.setattr(signer, "probe", _ok_probe)
    ran = []

    async def fake_with_retry(user, period, log_fn=None):
        ran.append(user.account)
        if user.account == "a":
            raise RuntimeError("boom")
        return signer.SignOutcome(signer.RESULT_SUCCESS, "ok")

    monkeypatch.setattr(signer, "sign_user_with_retry", fake_with_retry)
    asyncio.run(signer.sign_all("am"))
    assert ran == ["a", "b"]


def test_retry_unbounded_until_stop_time(monkeypatch):
    """可重试失败不限次数（打满全场），由 STOP_TIME 截断并推最终告警。"""
    calls = []

    async def fake_once(user, period):
        calls.append("try")
        if calls.count("try") >= 12:
            # 越过旧 8 次预算后模拟窗口关闭（STOP_TIME 每轮重读，现场改即生效）
            monkeypatch.setattr(signer, "STOP_TIME",
                                {"am": time(0, 0), "pm": time(0, 0)})
        return signer.SignOutcome(signer.RESULT_FAILED, "SSO 网络错误", retryable=True)

    async def fake_push(user, period, reason, attempts):
        calls.append(f"push:{attempts}")

    monkeypatch.setattr(signer, "sign_user_once", fake_once)
    monkeypatch.setattr(signer, "push_final_failure", fake_push)
    monkeypatch.setattr(signer, "probe_cached", _ok_gate)  # 门控始终绿，不影响重试
    monkeypatch.setattr(signer, "RETRY_INTERVAL", -100)  # sleep 负值立即返回
    monkeypatch.setattr(signer, "STOP_TIME", {"am": time(23, 59), "pm": time(23, 59)})
    monkeypatch.setattr(signer.random, "uniform", lambda lo, hi: 0)

    outcome = asyncio.run(signer.sign_user_with_retry(
        _user(1, "a"), "am", log_fn=lambda *a: None))
    assert outcome.result == signer.RESULT_FAILED
    assert calls.count("try") == 12  # 不受次数预算限制
    assert "push:12" in calls


def test_retry_enters_watch_and_cancels_after_red_streak(monkeypatch):
    """持续故障（门控探针连红）：只打一次登录链路即转入守候，连红 8 次取消本轮。"""
    calls = []

    async def fake_once(user, period):
        calls.append("try")
        return signer.SignOutcome(signer.RESULT_FAILED,
                                  "考勤系统会话交换网络错误: ConnectTimeout",
                                  retryable=True)

    pushed = []

    async def fake_notify(title, msg, sendkey=None):
        pushed.append((title, sendkey))

    monkeypatch.setattr(signer, "sign_user_once", fake_once)
    monkeypatch.setattr(signer, "probe_cached", _red_gate)
    monkeypatch.setattr(signer, "notify", fake_notify)
    monkeypatch.setattr(signer, "RETRY_INTERVAL", -100)
    monkeypatch.setattr(signer, "STOP_TIME", {"am": time(23, 59), "pm": time(23, 59)})
    monkeypatch.setattr(signer.random, "uniform", lambda lo, hi: 0)

    user = _user(1, "a")
    user.sendkey = "SCTxxx"
    outcome = asyncio.run(signer.sign_user_with_retry(user, "am", log_fn=lambda *a: None))
    assert outcome.result == signer.RESULT_CANCELLED
    assert "持续无法访问" in outcome.message
    assert calls.count("try") == 1  # 只打了首次登录，之后全部守候
    # 个人版模式：只推本人，标题带“自动签到取消”
    assert len(pushed) == 1 and "自动签到取消" in pushed[0][0]
    assert pushed[0][1] == "SCTxxx"


def test_retry_watch_recovers_and_signs(monkeypatch):
    """守候期间探测转绿：退出守候恢复正常签到流程。"""
    tries = []

    async def fake_once(user, period):
        tries.append(1)
        if len(tries) == 1:
            return signer.SignOutcome(signer.RESULT_FAILED, "SSO 网络错误: 502",
                                      retryable=True)
        return signer.SignOutcome(signer.RESULT_SUCCESS, "签到成功")

    gates = []

    async def flaky_gate(ttl=240.0, on_fresh=None):
        gates.append(1)
        if len(gates) == 1:
            return ProbeResult(False, "认证不通；考勤不通", 0)
        return ProbeResult(True, "认证 10ms；考勤 20ms", 20)

    async def fake_push(user, period):
        pass

    monkeypatch.setattr(signer, "sign_user_once", fake_once)
    monkeypatch.setattr(signer, "probe_cached", flaky_gate)
    monkeypatch.setattr(signer, "push_success", fake_push)
    monkeypatch.setattr(signer, "RETRY_INTERVAL", -100)
    monkeypatch.setattr(signer, "STOP_TIME", {"am": time(23, 59), "pm": time(23, 59)})
    monkeypatch.setattr(signer.random, "uniform", lambda lo, hi: 0)

    outcome = asyncio.run(signer.sign_user_with_retry(
        _user(1, "a"), "am", log_fn=lambda *a: None))
    assert outcome.result == signer.RESULT_SUCCESS
    assert len(tries) == 2 and len(gates) == 2


def test_preflight_admin_skip_logs_cancelled(monkeypatch, tmp_path):
    """管理员提前取消：赛前守卫不探测不打登录，为启用用户写 cancelled 终态日志
    （账号总览徽标与日志分析才能如实显示「本轮取消」）。"""
    from app import db
    object.__setattr__(models.settings, "data_dir", str(tmp_path))
    db._conn = None
    db.init()
    try:
        from datetime import datetime
        u1 = models.create_user("甲", "1001", "pw", None)
        u2 = models.create_user("乙", "1002", "pw", None)
        models.set_user_enabled(u2, False)
        today = datetime.now(signer.TZ).strftime("%Y-%m-%d")
        models.set_setting(signer.skip_key(today, "am"), "1")

        async def _boom(timeout=8.0):
            raise AssertionError("已取消的场次不应发起探测")
        monkeypatch.setattr(signer, "probe", _boom)

        signed = []

        async def fake_sign_one(user, period):
            signed.append(user.account)
        monkeypatch.setattr(signer, "_sign_one", fake_sign_one)

        asyncio.run(signer.sign_all("am"))
        assert not signed
        rows = models.today_results()
        assert rows[u1]["am"]["result"] == signer.RESULT_CANCELLED
        assert "取消" in rows[u1]["am"]["message"]
        assert u2 not in rows  # 停用用户不写
    finally:
        db._conn = None
        object.__setattr__(models.settings, "data_dir", "data")


def test_admin_skip_flag_stops_retry_loop(monkeypatch, tmp_path):
    """管理员手动取消标记：DB 模式下重试循环每轮检查，立即以 skipped 收场。"""
    from app import db
    object.__setattr__(models.settings, "data_dir", str(tmp_path))
    db._conn = None
    db.init()
    try:
        from datetime import datetime
        today = datetime.now(signer.TZ).strftime("%Y-%m-%d")
        models.set_setting(signer.skip_key(today, "am"), "1")

        async def fake_once(user, period):
            raise AssertionError("已取消的场次不应发起登录")

        monkeypatch.setattr(signer, "sign_user_once", fake_once)
        outcome = asyncio.run(signer.sign_user_with_retry(_user(1, "a"), "am"))
        assert outcome.result == signer.RESULT_CANCELLED
        assert "取消" in outcome.message
    finally:
        db._conn = None
        object.__setattr__(models.settings, "data_dir", "data")


def test_sign_all_preflight_aborts_when_unreachable(monkeypatch, tmp_path):
    """赛前探测连续失败 → 整轮放弃：统一广播出口逐用户通知（每人一条，
    不再另发管理员诊断版——诊断留在管理页/热力图）。"""
    from app import db
    object.__setattr__(models.settings, "data_dir", str(tmp_path))
    db._conn = None
    db.init()
    try:
        probes = []

        async def fake_probe(timeout=8.0, switch_on_fail=True):
            probes.append(1)
            return ProbeResult(False, "认证 450ms；考勤超时", 8000)

        async def fake_sleep(seconds):
            pass

        pushed = []

        async def fake_notify(title, msg, sendkey=None):
            pushed.append((title, msg, sendkey))

        users = [_user(1, "with_key"), _user(2, "no_key"), _user(3, "disabled")]
        users[0].sendkey = "SCTxxx"      # 启用且有 SendKey → 应收广播
        users[2].enabled = False          # 停用 → 不收
        monkeypatch.setattr(models, "list_users", lambda: users)
        monkeypatch.setattr(signer, "probe", fake_probe)
        monkeypatch.setattr(signer.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(signer, "notify", fake_notify)

        signed = []

        async def fake_sign_one(user, period):
            signed.append(user.account)

        monkeypatch.setattr(signer, "_sign_one", fake_sign_one)

        asyncio.run(signer.sign_all("pm"))
        assert len(probes) == 2                    # 一次失败 + 一次复核
        assert not signed                          # 未进入账号签到
        broadcasts = [(t, m, k) for t, m, k in pushed if "自动签到取消" in t]
        assert len(broadcasts) == 1 and broadcasts[0][2] == "SCTxxx"  # 仅启用且有 key 的用户
        assert "医院系统暂时无法访问" in broadcasts[0][1]
        assert not [t for t, m, k in pushed if "无法访问" in t and k is None]  # 无管理员专属版
    finally:
        db._conn = None
        object.__setattr__(models.settings, "data_dir", "data")


def test_broadcast_round_cancelled_dedupes_across_paths(monkeypatch, tmp_path):
    """统一广播出口：同一场次赛前取消后再手动取消，用户只收一次（原子占位去重）。"""
    from app import db
    object.__setattr__(models.settings, "data_dir", str(tmp_path))
    db._conn = None
    db.init()
    try:
        pushed = []

        async def fake_notify(title, msg, sendkey=None):
            pushed.append((title, msg, sendkey))

        users = [_user(1, "with_key"), _user(2, "no_key")]
        users[0].sendkey = "SCTxxx"
        monkeypatch.setattr(models, "list_users", lambda: users)
        monkeypatch.setattr(signer, "notify", fake_notify)

        asyncio.run(signer.broadcast_round_cancelled("pm", "医院系统暂时无法访问"))
        asyncio.run(signer.broadcast_round_cancelled("pm", "管理员手动取消"))
        assert len(pushed) == 1  # 第二路径被去重
        assert "医院系统暂时无法访问" in pushed[0][1]
        assert pushed[0][2] == "SCTxxx"
        # 换个场次（上午）不受去重影响
        asyncio.run(signer.broadcast_round_cancelled("am", "管理员手动取消"))
        assert len(pushed) == 2 and "管理员手动取消" in pushed[1][1]
    finally:
        db._conn = None
        object.__setattr__(models.settings, "data_dir", "data")
