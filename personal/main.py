"""lazy-clerk 个人版入口（PyInstaller 单 exe）。

用法：
  lazy-clerk.exe setup       首次配置向导（验证医院账号后写入 config.json）
  lazy-clerk.exe sign        执行当前时段签到（带 5 分钟重试；计划任务调用此命令）
  lazy-clerk.exe status      查询今日考勤状态
  lazy-clerk.exe install     注册 Windows 计划任务（每天 6:58 / 13:58 自动签到）
  lazy-clerk.exe uninstall   删除计划任务

设计要点：
- 核心链路（client/signer/notify/crypto）复用 app/core，与服务器版单点维护
- 无数据库：日志写 exe 同目录 sign.log
- 不补签：进程没跑（电脑关机/睡眠）就错过，由医院 7:00/14:00 系统提醒兜底
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

# 源码直接运行（python personal/main.py）时补项目根目录到 sys.path
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import signer  # noqa: E402
from app.models import User  # noqa: E402
from personal.config import LOG_PATH, load, wizard  # noqa: E402

TASK_NAMES = {"am": "lazy-clerk-sign-am", "pm": "lazy-clerk-sign-pm"}
TASK_TIMES = {"am": "06:58", "pm": "13:58"}

USAGE = __doc__


def file_log(user_id: int, date: str, period: str, result: str, message: str) -> None:
    """signer 的日志回调：个人版写本地文件。"""
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{date} {period} [{result}] {message}\n")


def make_user() -> User:
    cfg = load()
    if cfg is None:
        print("[错误] 未找到配置。请先运行: lazy-clerk.exe setup")
        sys.exit(1)
    return User(id=0, nickname=cfg.nickname, account=cfg.account,
                password=cfg.password, sendkey=cfg.sendkey or None,
                enabled=True, created_at="")


async def cmd_sign() -> int:
    user = make_user()
    period = signer.current_period()
    print(f"当前时段: {signer.PERIOD_NAME[period]}，开始签到流程（失败自动每 5 分钟重试）...")
    outcome = await signer.sign_user_with_retry(user, period, log_fn=file_log)
    print(f"结果: {outcome.result} - {outcome.message}")
    return 0 if outcome.result != signer.RESULT_FAILED else 1


async def cmd_status() -> int:
    from datetime import datetime

    from app.core.client import HospitalClient

    user = make_user()
    today = datetime.now(signer.TZ).strftime("%Y-%m-%d")
    print(f"正在查询 {today} 考勤状态...")
    try:
        async with HospitalClient(user.account, user.password) as client:
            await client.login()
            rows = await client.get_attendance(today)
    except Exception as e:
        print(f"[错误] 查询失败: {e}")
        return 1
    if not rows:
        print("今日无排班记录。")
        return 0
    print(f"{'时段':<6}{'状态':<14}{'签到时间':<10}{'科室'}")
    print("-" * 60)
    for row in rows:
        status = signer.STATUS_TEXT.get(row.get("SignInStatus"),
                                        f"未知({row.get('SignInStatus')})")
        sign_time = str(row.get("SignInTime") or "")[10:16]
        print(f"{row.get('TimeName', ''):<6}{status:<14}{sign_time:<10}{row.get('OrgName', '')}")
    return 0


def _task_cmd(action: str) -> str:
    """计划任务调用的命令行：打包后直接 exe，源码时走 python 解释器。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {action}'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" {action}'


def cmd_install() -> int:
    for period, name in TASK_NAMES.items():
        r = subprocess.run(
            ["schtasks", "/create", "/tn", name, "/tr", _task_cmd("sign"),
             "/sc", "daily", "/st", TASK_TIMES[period], "/f"],
            capture_output=True, text=True)
        ok = r.returncode == 0
        print(f"[{'成功' if ok else '失败'}] 计划任务 {name}"
              f"（每天 {TASK_TIMES[period]}）{'' if ok else ': ' + (r.stderr or r.stdout).strip()}")
        if not ok:
            return 1
    print("\n完成。电脑需在签到时段处于开机状态（睡眠请合盖前注意）；"
          "错过时段时医院系统会在 7:00/14:00 推送提醒兜底。")
    return 0


def cmd_uninstall() -> int:
    for name in TASK_NAMES.values():
        r = subprocess.run(["schtasks", "/delete", "/tn", name, "/f"],
                           capture_output=True, text=True)
        print(f"[{'成功' if r.returncode == 0 else '失败'}] 删除计划任务 {name}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(USAGE)
        return 0
    cmd = sys.argv[1].lower()
    if cmd == "setup":
        wizard()
        return 0
    if cmd == "sign":
        return asyncio.run(cmd_sign())
    if cmd == "status":
        return asyncio.run(cmd_status())
    if cmd == "install":
        return cmd_install()
    if cmd == "uninstall":
        return cmd_uninstall()
    print(f"未知命令: {cmd}\n{USAGE}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
