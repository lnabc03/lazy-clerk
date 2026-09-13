"""Windows 计划任务助手：个人版/宿舍版共用。

schtasks 默认方式（不加 /it）创建的任务锁屏下照常运行；
睡眠唤醒与错过补跑 schtasks 命令行不支持，注册后走 PowerShell 补设置。
"""
from __future__ import annotations

import subprocess
import sys


def entry_command(entry_script: str, args: str) -> str:
    """计划任务调用的命令行：打包后直接 exe，源码时走 python 解释器。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {args}'
    return f'"{sys.executable}" "{entry_script}" {args}'


def create_daily(name: str, time_hhmm: str, args: str, entry_script: str) -> str | None:
    """注册每日任务（已存在则覆盖）。返回错误信息，成功为 None。"""
    r = subprocess.run(
        ["schtasks", "/create", "/tn", name, "/tr", entry_command(entry_script, args),
         "/sc", "daily", "/st", time_hhmm, "/f"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return (r.stderr or r.stdout).strip()
    return None


def enable_wakeup(name: str) -> bool:
    """开启睡眠唤醒与错过补跑。失败不阻断使用，仅睡眠时可能错过。"""
    ps = (f"$t = Get-ScheduledTask -TaskName '{name}'; "
          f"$t.Settings.WakeToRun = $true; "
          f"$t.Settings.StartWhenAvailable = $true; "
          f"Set-ScheduledTask -InputObject $t | Out-Null")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    return r.returncode == 0


def delete(name: str) -> None:
    subprocess.run(["schtasks", "/delete", "/tn", name, "/f"],
                   capture_output=True, text=True)


def exists(name: str) -> bool:
    r = subprocess.run(["schtasks", "/query", "/tn", name],
                       capture_output=True, text=True)
    return r.returncode == 0
