"""个人版配置：exe 同目录的 config.json（明文，本机自担）+ 首次运行向导。

复用 app.core 的医院 SSO 认证：配置保存前先真实登录验证一次，填错当场发现。
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import time


def base_dir() -> str:
    """配置/日志目录：打包后为 exe 所在目录，源码运行时为项目根目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


CONFIG_PATH = os.path.join(base_dir(), "config.json")
LOG_PATH = os.path.join(base_dir(), "sign.log")

# 默认触发时间；用户可在「配置并启用自动签到」时自定义
DEFAULT_TIMES = {"am": "05:00", "pm": "13:00"}

# 医院签到窗口（设计稿 2.6）：触发时刻必须落在窗口内，终点为打满全场的停止时间，
# 本身不可取（8:00 触发 = 窗口已关，当天必错过）
WINDOWS = {"am": (time(5, 0), time(8, 0)), "pm": (time(13, 0), time(14, 30))}


def parse_task_time(period: str, text: str) -> tuple[str | None, str | None]:
    """校验并归一化触发时间。返回 ("HH:MM", None) 或 (None, 错误原因)。"""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m:
        return None, f"格式不对：「{text}」，请按 HH:MM 输入（如 05:30）"
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None, f"不是合法时刻：「{text}」"
    t = time(h, mi)
    start, end = WINDOWS[period]
    label = "上午" if period == "am" else "下午"
    if not (start <= t < end):
        fmt = lambda x: x.strftime("%H:%M")  # noqa: E731
        return None, (f"{label}签到窗口是 {fmt(start)}–{fmt(end)}，"
                      f"「{text}」不在窗口内")
    return f"{h:02d}:{mi:02d}", None


@dataclass
class PersonalConfig:
    nickname: str
    account: str
    password: str
    sendkey: str  # 可为空字符串
    time_am: str = DEFAULT_TIMES["am"]  # 计划任务触发时刻 HH:MM
    time_pm: str = DEFAULT_TIMES["pm"]

    def to_dict(self) -> dict:
        return {
            "nickname": self.nickname, "account": self.account,
            "password": self.password, "sendkey": self.sendkey,
            "time_am": self.time_am, "time_pm": self.time_pm,
        }


def load() -> PersonalConfig | None:
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return PersonalConfig(
            nickname=d["nickname"], account=d["account"],
            password=d["password"], sendkey=d.get("sendkey", ""),
            # 旧版 config.json 无时间字段，回落默认
            time_am=d.get("time_am") or DEFAULT_TIMES["am"],
            time_pm=d.get("time_pm") or DEFAULT_TIMES["pm"],
        )
    except (KeyError, json.JSONDecodeError):
        return None


def save(cfg: PersonalConfig) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)


async def wizard() -> PersonalConfig:
    """配置向导：交互输入 → 医院 SSO 真实验证 → 写入 config.json。"""
    from app.core.client import verify_account

    print("-" * 46)
    print("  配置账号")
    print("-" * 46)
    nickname = input("你的称呼: ").strip()
    account = input("工号: ").strip()
    # 管道/重定向输入时 getpass 会死等控制台，退化为普通 input
    if sys.stdin.isatty():
        password = getpass.getpass("统一登录平台密码（输入时不显示）: ").strip()
    else:
        password = input("统一登录平台密码: ").strip()
    print("微信推送教程：https://sct.ftqq.com/login/ 扫码登录即可获取 SendKey")
    sendkey = input("Server 酱 SendKey（可留空）: ").strip()

    if not account or not password:
        print("\n工号和密码不能为空。")
        sys.exit(1)

    print("\n正在向医院平台验证账号密码...")
    err = await verify_account(account, password)
    if err:
        print(f"验证未通过：{err}")
        print("请检查密码后重试。")
        sys.exit(1)

    cfg = PersonalConfig(
        nickname=nickname or account,
        account=account, password=password, sendkey=sendkey,
    )
    save(cfg)
    print("\n验证通过，配置已保存。")
    return cfg
