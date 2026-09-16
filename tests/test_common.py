"""防爆破/防放大限流单测：按 scope 独立计数、独立阈值。"""
import time

from app.routes import common


def _reset():
    common._failures.clear()
    common._locked_until.clear()


def test_sso_scope_tighter_than_login():
    """sso scope（注册/改密，会真实请求医院 SSO）：1 分钟 3 次锁 30 分钟。"""
    _reset()
    ip = "1.2.3.4"
    for _ in range(2):
        common.record_login_failure(ip, scope="sso")
        assert common.login_locked(ip, scope="sso") == 0
    common.record_login_failure(ip, scope="sso")
    locked = common.login_locked(ip, scope="sso")
    assert 1700 < locked <= 1800


def test_login_scope_threshold_unchanged():
    """user/admin scope 维持 1 分钟 5 次锁 10 分钟。"""
    _reset()
    ip = "1.2.3.4"
    for _ in range(4):
        common.record_login_failure(ip, scope="user")
        assert common.login_locked(ip, scope="user") == 0
    common.record_login_failure(ip, scope="user")
    assert 590 < common.login_locked(ip, scope="user") <= 600


def test_scopes_are_independent():
    """同一 IP 在不同入口的计数互不影响。"""
    _reset()
    ip = "1.2.3.4"
    for _ in range(3):  # sso 锁了
        common.record_login_failure(ip, scope="sso")
    assert common.login_locked(ip, scope="sso") > 0
    assert common.login_locked(ip, scope="user") == 0
    assert common.login_locked(ip, scope="admin") == 0


def test_window_expiry_unlocks(monkeypatch):
    """计数窗口过期后重新计数，不再锁定。"""
    _reset()
    ip = "1.2.3.4"
    real_time = time.time
    for _ in range(2):
        common.record_login_failure(ip, scope="sso")
    # 61 秒后的第三次：前两次已出窗
    monkeypatch.setattr(common.time, "time", lambda: real_time() + 61)
    common.record_login_failure(ip, scope="sso")
    assert common.login_locked(ip, scope="sso") == 0


def test_clear():
    _reset()
    ip = "1.2.3.4"
    for _ in range(3):
        common.record_login_failure(ip, scope="sso")
    assert common.login_locked(ip, scope="sso") > 0
    common.clear_login_failures(ip, scope="sso")
    assert common.login_locked(ip, scope="sso") == 0
