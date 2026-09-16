"""签到判定逻辑单测：find_target_row / decide（设计稿 4.1）。"""
import asyncio
from datetime import time

from app import models
from app.core import signer
from app.core.client import ProbeResult


def _row(status=None, day_off=0, date="2026-09-11", time_name="上午", rid=1):
    return {"ID": rid, "WeekDate": date, "TimeName": time_name,
            "SignInStatus": status, "DayOff": day_off}


async def _ok_probe(timeout=8.0):
    return ProbeResult(True, "HTTP 200", 10)


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


def test_retry_capped_at_max_attempts(monkeypatch):
    """可重试失败最多尝试 MAX_ATTEMPTS 次（SSO 不可达不会自愈，多试无益）。"""
    calls = []

    async def fake_once(user, period):
        calls.append("try")
        return signer.SignOutcome(signer.RESULT_FAILED, "SSO 网络错误", retryable=True)

    async def fake_push(user, period, reason, attempts):
        calls.append(f"push:{attempts}")

    monkeypatch.setattr(signer, "sign_user_once", fake_once)
    monkeypatch.setattr(signer, "push_final_failure", fake_push)
    monkeypatch.setattr(signer, "RETRY_INTERVAL", -100)  # sleep 负值立即返回
    monkeypatch.setattr(signer, "STOP_TIME", {"am": time(23, 59), "pm": time(23, 59)})
    monkeypatch.setattr(signer.random, "uniform", lambda lo, hi: 0)

    outcome = asyncio.run(signer.sign_user_with_retry(
        _user(1, "a"), "am", log_fn=lambda *a: None))
    assert outcome.result == signer.RESULT_FAILED
    assert calls.count("try") == signer.MAX_ATTEMPTS == 3
    assert f"push:{signer.MAX_ATTEMPTS}" in calls


def test_sign_all_preflight_aborts_when_unreachable(monkeypatch):
    """赛前探测连续失败 → 整轮放弃：管理员收诊断，配了 SendKey 的启用用户收广播。"""
    probes = []

    async def fake_probe(timeout=8.0):
        probes.append(1)
        return ProbeResult(False, "连接超时（疑似被防火墙拦截）", 8000)

    async def fake_sleep(seconds):
        pass

    pushed = []

    async def fake_notify(title, msg, sendkey=None):
        pushed.append((title, sendkey))

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
    admin_alerts = [t for t, k in pushed if "不可达" in t and k is None]
    broadcasts = [(t, k) for t, k in pushed if "自动签到取消" in t]
    assert len(admin_alerts) == 1
    assert len(broadcasts) == 1 and broadcasts[0][1] == "SCTxxx"  # 仅启用且有 key 的用户
