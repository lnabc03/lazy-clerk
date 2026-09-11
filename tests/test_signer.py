"""签到判定逻辑单测：find_target_row / decide（设计稿 4.1）。"""
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
