"""Windows 计划任务助手：个人版/宿舍版共用。

schtasks 默认方式（不加 /it）创建的任务锁屏下照常运行；
睡眠唤醒与错过补跑 schtasks 命令行不支持，注册后走 PowerShell 补设置。

编码注意：schtasks 输出跟随系统区域（中文 Windows 为 GBK），PowerShell
管道输出编码不定——统一按字节捕获后显式解码，避免解码失败导致
reader 线程崩溃、stdout 变 None（实测冻结核实过的坑）。
"""
from __future__ import annotations

import os
import subprocess
import sys


def _run(cmd: list[str], encoding: str) -> tuple[int, str]:
    """以字节捕获子进程输出并按指定编码容错解码，返回 (returncode, stdout)。"""
    r = subprocess.run(cmd, capture_output=True)
    out = (r.stdout or b"").decode(encoding, errors="replace")
    err = (r.stderr or b"").decode(encoding, errors="replace")
    return r.returncode, (out + err).strip()


def _run_ps(script: str) -> tuple[int, str]:
    """跑 PowerShell 并强制其管道输出为 UTF-8。"""
    return _run(["powershell", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + script],
                encoding="utf-8")


def entry_command(entry_script: str, args: str) -> str:
    """计划任务调用的命令行：打包后直接 exe，源码时走 python 解释器。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {args}'
    return f'"{sys.executable}" "{entry_script}" {args}'


def create_daily(name: str, time_hhmm: str, args: str, entry_script: str) -> str | None:
    """注册每日任务（已存在则覆盖）。返回错误信息，成功为 None。"""
    rc, out = _run(["schtasks", "/create", "/tn", name,
                    "/tr", entry_command(entry_script, args),
                    "/sc", "daily", "/st", time_hhmm, "/f"], encoding="gbk")
    return None if rc == 0 else out


def enable_wakeup(name: str) -> bool:
    """开启睡眠唤醒与错过补跑。失败不阻断使用，仅睡眠时可能错过。"""
    rc, _ = _run_ps(
        f"$t = Get-ScheduledTask -TaskName '{name}'; "
        f"$t.Settings.WakeToRun = $true; "
        f"$t.Settings.StartWhenAvailable = $true; "
        f"Set-ScheduledTask -InputObject $t | Out-Null")
    return rc == 0


def delete(name: str) -> None:
    _run(["schtasks", "/delete", "/tn", name, "/f"], encoding="gbk")


def exists(name: str) -> bool:
    rc, _ = _run(["schtasks", "/query", "/tn", name], encoding="gbk")
    return rc == 0


def existing_action(name: str) -> str | None:
    """已存在任务的执行命令；任务不存在返回 None。"""
    rc, out = _run_ps(
        f"$t = Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue; "
        f"if ($t) {{ ($t.Actions | ForEach-Object {{ $_.Execute + ' ' + $_.Arguments }}) -join ' ' }}")
    if rc != 0:
        return None
    return out or None


def conflicts_with(name: str, entry_script: str) -> str | None:
    """任务已存在且指向别的程序时返回其命令（另一形态冲突），否则 None。

    个人版/宿舍版任务同名（一机一形态），install 前用它防止静默覆盖。
    """
    action = existing_action(name)
    if not action:
        return None
    marker = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(entry_script)
    if marker.lower() in action.lower():
        return None
    return action
