"""lazy-clerk 宿舍版入口

双击 exe（或 start-web.bat）打开管理页；签到由 Windows 计划任务驱动，
跑完即退，不依赖任何常驻进程。

命令：
  lazy-clerk-dorm.exe            打开管理页（同 web）
  lazy-clerk-dorm.exe web        启动管理页 http://127.0.0.1:8787/admin
  lazy-clerk-dorm.exe sign am    签到全部启用账号（带 0–5 分钟随机延迟，计划任务用）
  lazy-clerk-dorm.exe sign-now   立即签到全部启用账号（无随机延迟，测试用）
  lazy-clerk-dorm.exe install    注册每天 6:53 / 13:53 的签到计划任务
  lazy-clerk-dorm.exe uninstall  删除签到计划任务
"""
from __future__ import annotations

import asyncio
import getpass
import logging
import os
import random
import sys

# 源码直接运行（python dorm/main.py）时补项目根目录到 sys.path
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TASKS = {"am": ("lazy-clerk-sign-am", "06:53"), "pm": ("lazy-clerk-sign-pm", "13:53")}


# ---------- sign ----------

async def cmd_sign(period: str | None, jitter: bool) -> int:
    from app import models
    from app.core import signer

    period = period or signer.current_period()
    if jitter:
        # 拟人化：随机延迟 0–5 分钟。任务注册在 :53，保证开始签到不晚于 :58，
        # 签到约 10 秒，赶在医院 7:00/14:00 系统提醒发出前完成
        delay = random.uniform(0, 300)
        print(f"随机延迟 {delay:.0f} 秒后开始{signer.PERIOD_NAME[period]}签到...")
        await asyncio.sleep(delay)
    await signer.sign_all(period)
    deleted = models.cleanup_logs(days=90)
    if deleted:
        print(f"已清理 90 天前日志 {deleted} 条")
    return 0


# ---------- web ----------

def _input_secret(prompt: str) -> str:
    """管道/重定向输入时 getpass 会死等控制台，退化为普通 input。"""
    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    return input(prompt).strip()


def _seed_admin() -> None:
    """首次运行：命令行引导设置管理员密码与 SendKey（此后在管理页修改）。"""
    from app import models

    print("=" * 46)
    print("  你好，欢迎使用 lazy-clerk 宿舍版")
    print("  首次运行，先设置管理员密码")
    print("=" * 46)
    while True:
        pw1 = _input_secret("\n管理员密码（至少 8 位，输入时不显示）: ")
        pw2 = _input_secret("再输一次: ")
        if len(pw1) >= 8 and pw1 == pw2:
            break
        print("密码至少 8 位且两次输入需一致。")
    sendkey = input("管理员微信推送 SendKey（可留空，之后可在管理页配置）: ").strip()
    models.set_setting("admin_password_hash", models.sha256(pw1))
    if sendkey:
        models.set_setting("serverchan_sendkey", sendkey)
    print("\n设置完成。\n")


def cmd_web() -> int:
    import uvicorn

    from app import db, models

    db.init()
    if not models.get_setting("admin_password_hash"):
        _seed_admin()

    from dorm.web import app, open_browser_later

    print("管理页：http://127.0.0.1:8787/admin")
    print("关闭本窗口即停止管理页；自动签到由计划任务执行，不受影响。")
    open_browser_later()
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
    return 0


# ---------- install / uninstall ----------

def cmd_install() -> int:
    from app.core import wintasks

    entry = os.path.abspath(__file__)
    for period, (name, hhmm) in TASKS.items():
        conflict = wintasks.conflicts_with(name, entry)
        if conflict:
            print(f"计划任务 {name} 已存在且指向其他程序：{conflict}")
            print("这台电脑可能装过另一形态，请先卸载旧的再开启。")
            return 1
        err = wintasks.create_daily(name, hhmm, f"sign {period}", entry)
        if err:
            print(f"注册计划任务失败：{err}")
            return 1
        if not wintasks.enable_wakeup(name):
            print("睡眠唤醒开启失败（不影响锁屏签到），电脑睡眠时可能错过签到。")
    print("自动签到已开启，每天 6:53–6:58 和 13:53–13:58 之间的随机时刻执行。")
    print("锁屏不影响签到；电脑插电时睡眠会自动唤醒执行。")
    print("错过时医院会在 7:00 和 14:00 提醒你。")
    return 0


def cmd_uninstall() -> int:
    from app.core import wintasks

    for name, _ in TASKS.values():
        wintasks.delete(name)
    print("自动签到已关闭。")
    return 0


# ---------- 入口 ----------

def main() -> int:
    if len(sys.argv) < 2:
        return cmd_web()
    cmd = sys.argv[1].lower()
    if cmd == "web":
        return cmd_web()
    if cmd == "sign":
        period = sys.argv[2].lower() if len(sys.argv) > 2 else None
        if period not in (None, "am", "pm"):
            print("时段只能是 am 或 pm")
            return 1
        return asyncio.run(cmd_sign(period, jitter=True))
    if cmd == "sign-now":
        return asyncio.run(cmd_sign(None, jitter=False))
    if cmd == "install":
        return cmd_install()
    if cmd == "uninstall":
        return cmd_uninstall()
    print(f"未知命令：{cmd}\n\n{__doc__}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
