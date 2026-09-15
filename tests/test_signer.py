"""签到判定逻辑单测：find_target_row / decide（设计稿 4.1）。"""
import asyncio

from app import models
from app.core import signer


def _row(status=None, day_off=0, date="2026-09-11", time_name="上午", rid=1):
    return {"ID": rid, "WeekDate": date, "TimeName": time_name,
            "SignInStatus": status, "DayOff": day_off}


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
    ran = []

    async def fake_with_retry(user, period, log_fn=None):
        ran.append(user.account)
        if user.account == "a":
            raise RuntimeError("boom")
        return signer.SignOutcome(signer.RESULT_SUCCESS, "ok")

    monkeypatch.setattr(signer, "sign_user_with_retry", fake_with_retry)
    asyncio.run(signer.sign_all("am"))
    assert ran == ["a", "b"]
