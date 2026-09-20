"""一句话摘要与场次分析聚合逻辑单测。"""
from datetime import datetime, timedelta

from app import db, models
from app.config import settings


def _use_tmp_db(tmp_path):
    object.__setattr__(settings, "data_dir", str(tmp_path))  # frozen dataclass 绕过
    db._conn = None
    db.init()


def _restore_db():
    db._conn = None
    object.__setattr__(settings, "data_dir", "data")


def _day(offset: int) -> str:
    return (datetime.now(models.TZ) - timedelta(days=offset)).strftime("%Y-%m-%d")


def _log(uid, date, period, result, message=""):
    db.conn().execute(
        "INSERT INTO sign_logs (user_id, date, period, result, message, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (uid, date, period, result, message, f"{date} 06:00:00"),
    )
    db.conn().commit()


def _attempt(uid, date, period, round_no, result, node="直连", message=""):
    db.conn().execute(
        "INSERT INTO sign_attempts (run_id, user_id, round, result, message, node, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (f"{date}-{period}", uid, round_no, result, message, node, f"{date} 06:0{round_no}:00"),
    )
    db.conn().commit()


def _green_day(uid, date):
    _log(uid, date, "am", "success")
    _log(uid, date, "pm", "success")


def test_summary_counts_and_streak(tmp_path):
    _use_tmp_db(tmp_path)
    try:
        u1 = models.create_user("甲", "1001", "pw", None)
        u2 = models.create_user("乙", "1002", "pw", None)
        _green_day(u1, _day(3))
        _green_day(u1, _day(2))
        _green_day(u1, _day(1))
        _green_day(u2, _day(1))  # u2 只有一天
        # 今天只跑了上午：不计入也不中断，从昨天数 → 3 天
        _log(u1, _day(0), "am", "success")
        s = models.service_summary()
        assert s["users"] == 2
        assert s["signs"] == 6 + 2 + 1  # 甲三天×2 + 乙一天×2 + 今天 am
        assert s["stable_days"] == 3
    finally:
        _restore_db()


def test_summary_today_failure_resets(tmp_path):
    _use_tmp_db(tmp_path)
    try:
        u1 = models.create_user("甲", "1001", "pw", None)
        _green_day(u1, _day(1))
        _log(u1, _day(0), "am", "failed", "窗口关闭（已尝试 5 次）: 超时")
        assert models.service_summary()["stable_days"] == 0
    finally:
        _restore_db()


def test_summary_ignores_manual_and_check_failures(tmp_path):
    _use_tmp_db(tmp_path)
    try:
        u1 = models.create_user("甲", "1001", "pw", None)
        _green_day(u1, _day(1))
        _log(u1, _day(0), "am", "success")
        _log(u1, _day(0), "pm", "success")
        # 手动签到失败与状态检测失败不算定时任务失败，不破环稳定日
        _log(u1, _day(0), "pm", "failed", "[管理员手动] 服务端拒绝: ...")
        _log(u1, _day(0), "pm", "failed", "[检测] 登录失败: ...")
        assert models.service_summary()["stable_days"] == 2
    finally:
        _restore_db()


def test_session_analysis_rounds_pace_nodes(tmp_path):
    _use_tmp_db(tmp_path)
    try:
        u1 = models.create_user("甲", "1001", "pw", None)
        u2 = models.create_user("乙", "1002", "pw", None)
        d = _day(0)
        # 甲第 1 轮成功（代理节点 A）；乙第 1 轮失败、第 2 轮成功（换到节点 B）
        _attempt(u1, d, "am", 1, "success", "🇭🇰节点A")
        _attempt(u2, d, "am", 1, "failed", "🇭🇰节点A", "SSO 网络错误: 502")
        _attempt(u2, d, "am", 2, "success", "🇭🇰节点B")
        out = models.session_analysis(days=7)
        assert len(out) == 1
        s = out[0]
        assert s["label"].endswith("上午")
        assert s["rounds"] == 2
        assert s["users"] == 2 and s["ok"] == 2 and not s["failed"]
        assert s["pace"] == [(1, 1), (2, 1)]
        assert s["nodes"] == ["节点A", "节点B"]  # 展示名剥掉订阅的国旗前缀
        assert s["detail"][0]["ok"] == ["甲"] and s["detail"][0]["fail"] == ["乙"]
        assert s["detail"][1]["ok"] == ["乙"]
    finally:
        _restore_db()


def test_session_analysis_final_failure_and_fallback(tmp_path):
    _use_tmp_db(tmp_path)
    try:
        u1 = models.create_user("甲", "1001", "pw", None)
        u2 = models.create_user("乙", "1002", "pw", None)
        # 今天 pm：乙两轮皆败 → 最终失败名单
        _attempt(u1, _day(0), "pm", 1, "success", "直连")
        _attempt(u2, _day(0), "pm", 1, "failed", "直连", "超时")
        _attempt(u2, _day(0), "pm", 2, "failed", "直连", "窗口关闭")
        # 昨天 pm：无逐轮数据（落库前），回落 sign_logs 终态
        _log(u1, _day(1), "pm", "success")
        _log(u2, _day(1), "pm", "no_schedule", "今日无该时段排班")
        out = {s["run_id"]: s for s in models.session_analysis(days=7)}
        today_pm = out[f"{_day(0)}-pm"]
        assert today_pm["ok"] == 1 and len(today_pm["failed"]) == 1
        assert today_pm["failed"][0]["name"] == "乙"
        assert "窗口关闭" in today_pm["failed_title"]
        old_pm = out[f"{_day(1)}-pm"]
        assert old_pm["rounds"] is None and old_pm["ok"] == 2 and not old_pm["nodes"]
    finally:
        _restore_db()
