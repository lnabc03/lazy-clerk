"""个人版签到时间自定义：parse_task_time 校验（格式 + 签到窗口）。"""
from personal.config import DEFAULT_TIMES, parse_task_time


def test_default_times_are_valid():
    for period, t in DEFAULT_TIMES.items():
        normalized, err = parse_task_time(period, t)
        assert err is None
        assert normalized == t


def test_valid_times_normalized():
    assert parse_task_time("am", "5:00") == ("05:00", None)   # 窗口起点，可取
    assert parse_task_time("am", "5:30") == ("05:30", None)   # 单位数小时归一化
    assert parse_task_time("am", "07:59") == ("07:59", None)
    assert parse_task_time("pm", "13:00") == ("13:00", None)  # 窗口起点，可取
    assert parse_task_time("pm", "14:29") == ("14:29", None)


def test_outside_window_rejected():
    assert parse_task_time("am", "04:59")[1] is not None
    assert parse_task_time("am", "08:00")[1] is not None   # 终点 = 窗口关闭，不可取
    assert parse_task_time("pm", "12:59")[1] is not None
    assert parse_task_time("pm", "14:30")[1] is not None   # 终点 = 窗口关闭，不可取
    # 时段不通用：上午时刻不能用于下午任务
    assert parse_task_time("pm", "05:00")[1] is not None
    assert parse_task_time("am", "13:00")[1] is not None


def test_bad_format_rejected():
    for bad in ("", "abc", "五点", "5pm", "7:5", "25:00", "07:60", "5:00:00", "--"):
        normalized, err = parse_task_time("am", bad)
        assert normalized is None
        assert err is not None
