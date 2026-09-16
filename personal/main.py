"""lazy-clerk 个人版

双击运行进入交互模式：首次使用自动进入配置引导，之后显示今日状态与功能菜单。
也可以在终端使用命令：lazy-clerk.exe setup | sign | status | install | uninstall
"""
from __future__ import annotations

import asyncio
import os
import sys

# 源码直接运行（python personal/main.py）时补项目根目录到 sys.path
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import notify, signer, wintasks  # noqa: E402
from app.models import User  # noqa: E402
from personal.config import LOG_PATH, load, wizard  # noqa: E402

# 个人版无数据库：关闭推送的管理员兜底回退，否则兜底会无谓地初始化 SQLite
notify.set_admin_key_resolver(None)

TASK_NAMES = {"am": "lazy-clerk-sign-am", "pm": "lazy-clerk-sign-pm"}
TASK_TIMES = {"am": "06:58", "pm": "13:00"}

RESULT_TEXT = {
    "success": "签到成功", "skipped": "已经签过", "no_schedule": "今日此时段无需签到",
    "failed": "签到失败", "manual": "需要人工处理", "sign": "待签到",
}


LOG_MAX_BYTES = 512 * 1024  # sign.log 上限，超出保留尾部一半（服务器/宿舍版有 90 天清理，个人版靠这个）


def _trim_log() -> None:
    try:
        if os.path.getsize(LOG_PATH) <= LOG_MAX_BYTES:
            return
        with open(LOG_PATH, "rb") as f:
            f.seek(-(LOG_MAX_BYTES // 2), os.SEEK_END)
            tail = f.read()
        with open(LOG_PATH, "wb") as f:
            f.write(tail)
    except OSError:
        pass


def file_log(user_id: int, date: str, period: str, result: str, message: str) -> None:
    """signer 的日志回调：个人版写本地文件。"""
    from datetime import datetime
    now = datetime.now().strftime("%H:%M:%S")
    _trim_log()
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{date} {now} {period} [{result}] {message}\n")


def make_user() -> User:
    cfg = load()
    if cfg is None:
        print("还没有配置，先运行 setup 完成首次配置。")
        sys.exit(1)
    return User(id=0, nickname=cfg.nickname, account=cfg.account,
                password=cfg.password, sendkey=cfg.sendkey or None,
                enabled=True, created_at="")


# ---------- 核心动作 ----------

async def cmd_sign() -> int:
    user = make_user()
    period = signer.current_period()
    print(f"开始{signer.PERIOD_NAME[period]}签到，失败时每 5 分钟自动重试。")
    outcome = await signer.sign_user_with_retry(user, period, log_fn=file_log)
    # 用户主动触发的签到无论结果如何都推送，兼作微信推送的连通性确认
    if outcome.result != signer.RESULT_SUCCESS:  # success 时重试函数内已推过
        await signer.push_manual_result(user, period, outcome)
    print(f"结果：{RESULT_TEXT.get(outcome.result, outcome.result)} — {outcome.message}")
    return 0 if outcome.result != signer.RESULT_FAILED else 1


async def cmd_status() -> int:
    from datetime import datetime

    from app.core.client import HospitalClient

    user = make_user()
    today = datetime.now(signer.TZ).strftime("%Y-%m-%d")
    print(f"正在查询 {today} 的考勤...")
    try:
        async with HospitalClient(user.account, user.password) as client:
            await client.login()
            rows = await client.get_attendance(today)
    except Exception as e:
        print(f"查询失败：{e}")
        return 1
    if not rows:
        print("今日没有排班。")
        return 0
    print(f"{'时段':<6}{'状态':<14}{'签到时间':<10}{'科室'}")
    print("-" * 56)
    for row in rows:
        status = signer.STATUS_TEXT.get(row.get("SignInStatus"),
                                        f"未知({row.get('SignInStatus')})")
        sign_time = str(row.get("SignInTime") or "")[10:16]
        print(f"{row.get('TimeName', ''):<6}{status:<14}{sign_time:<10}{row.get('OrgName', '')}")
    return 0


def cmd_install() -> int:
    for period, name in TASK_NAMES.items():
        conflict = wintasks.conflicts_with(name, os.path.abspath(__file__))
        if conflict:
            print(f"计划任务 {name} 已存在且指向其他程序：{conflict}")
            print("这台电脑可能装过另一形态，请先卸载旧的再开启。")
            return 1
        err = wintasks.create_daily(name, TASK_TIMES[period], "sign",
                                    os.path.abspath(__file__))
        if err:
            print(f"注册计划任务失败：{err}")
            return 1
        if not wintasks.enable_wakeup(name):
            print("睡眠唤醒开启失败（不影响锁屏签到），电脑睡眠时可能错过签到。")
    print("自动签到已开启，每天 6:58 和 13:00 准时执行。")
    print("锁屏不影响签到；电脑插电时睡眠会自动唤醒执行。")
    print("错过时医院会在 7:00 和 14:00 提醒你。")
    return 0


def cmd_uninstall() -> int:
    for name in TASK_NAMES.values():
        wintasks.delete(name)
    print("自动签到已关闭。")
    return 0


def tasks_installed() -> int:
    """返回已注册的计划任务数量（0–2）。"""
    return sum(1 for name in TASK_NAMES.values() if wintasks.exists(name))


# ---------- 交互模式 ----------

def pause() -> None:
    input("\n按回车继续...")


async def interactive() -> int:
    cfg = load()

    # 首次运行：直接进配置引导
    if cfg is None:
        print("=" * 46)
        print("  你好，欢迎使用 lazy-clerk")
        print("  实习考勤自动签到 · 个人版")
        print("=" * 46)
        print("\n第一次使用，先花半分钟完成配置：\n")
        await wizard()
        answer = input("\n现在开启每天 6:58 / 13:00 的自动签到吗？[Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            cmd_install()
        print("\n都设置好了，祝你拥有美好的一天~")
        pause()
        return 0

    # 已配置：状态一览 + 菜单
    print("=" * 46)
    print(f"  你好，{cfg.nickname}")
    print("=" * 46)
    await cmd_status()
    n_tasks = tasks_installed()
    if n_tasks == 2:
        print("\n自动签到：已开启（每天 6:58 / 13:00）")
    elif n_tasks == 1:
        print("\n自动签到：异常，只注册了一个时段，建议重新开启")
    else:
        print("\n自动签到：未开启")

    while True:
        print("\n----------")
        print("  1. 立即签到")
        print("  2. 刷新状态")
        print("  3. 更改配置")
        print("  4. 关闭自动签到" if n_tasks > 0 else "  4. 开启自动签到")
        print("  0. 退出")
        choice = input("\n输入数字: ").strip()

        if choice == "0":
            print("\n祝你拥有美好的一天~")
            pause()
            return 0
        elif choice == "1":
            await cmd_sign()
        elif choice == "2":
            await cmd_status()
        elif choice == "3":
            await wizard()
            cfg = load()
        elif choice == "4":
            if n_tasks > 0:
                cmd_uninstall()
            else:
                cmd_install()
            n_tasks = tasks_installed()
        else:
            print("输入 0-4 之间的数字。")
            continue
        pause()


# ---------- 入口 ----------

USAGE = """lazy-clerk 个人版

用法：
  lazy-clerk.exe            打开交互界面
  lazy-clerk.exe setup      配置账号密码
  lazy-clerk.exe sign       立即签到
  lazy-clerk.exe status     查询今日考勤
  lazy-clerk.exe install    开启每天自动签到
  lazy-clerk.exe uninstall  关闭自动签到
"""


def main() -> int:
    if len(sys.argv) < 2:
        return asyncio.run(interactive())
    cmd = sys.argv[1].lower()
    if cmd == "setup":
        asyncio.run(wizard())
        return 0
    if cmd == "sign":
        return asyncio.run(cmd_sign())
    if cmd == "status":
        return asyncio.run(cmd_status())
    if cmd == "install":
        return cmd_install()
    if cmd == "uninstall":
        return cmd_uninstall()
    print(f"未知命令：{cmd}\n\n{USAGE}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
