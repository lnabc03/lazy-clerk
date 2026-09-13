"""个人版配置：exe 同目录的 config.json（明文，本机自担）+ 首次运行向导。

复用 app.core 的医院 SSO 认证：配置保存前先真实登录验证一次，填错当场发现。
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from dataclasses import dataclass


def base_dir() -> str:
    """配置/日志目录：打包后为 exe 所在目录，源码运行时为项目根目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


CONFIG_PATH = os.path.join(base_dir(), "config.json")
LOG_PATH = os.path.join(base_dir(), "sign.log")


@dataclass
class PersonalConfig:
    nickname: str
    account: str
    password: str
    sendkey: str  # 可为空字符串

    def to_dict(self) -> dict:
        return {
            "nickname": self.nickname, "account": self.account,
            "password": self.password, "sendkey": self.sendkey,
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
