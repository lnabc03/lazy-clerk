"""Server 酱推送。单接口封装，一个 POST 完事。"""
from __future__ import annotations

import logging
from typing import Callable

import httpx

from ..config import settings

log = logging.getLogger("lazy-clerk.notify")

def _default_admin_key() -> str | None:
    from .. import models  # 延迟 import 避免循环

    return models.get_setting("serverchan_sendkey") or settings.serverchan_sendkey or None


# 默认走数据库/.env（公网版、宿舍版）；个人版启动时显式关闭
_admin_key_resolver: Callable[[], str | None] | None = _default_admin_key


def set_admin_key_resolver(resolver: Callable[[], str | None] | None) -> None:
    """替换管理员 SendKey 的回退来源；传 None 表示无管理员兜底（个人版）。"""
    global _admin_key_resolver
    _admin_key_resolver = resolver


def _admin_key() -> str | None:
    if _admin_key_resolver is None:
        return None
    return _admin_key_resolver()


# 推送标题前缀：公网版（我们托管的服务器）启动时设为 "[server] "，与自建
# 个人版/宿舍版的推送区分开。默认空前缀，不设置即不影响其他版本。
_title_prefix = ""


def set_title_prefix(prefix: str) -> None:
    global _title_prefix
    _title_prefix = prefix


async def notify(title: str, msg: str, sendkey: str | None = None) -> bool:
    """推送消息。优先级：参数 sendkey > 管理员 SendKey 回退；都无则记录告警。"""
    key = sendkey or _admin_key()
    if not key:
        log.warning("未配置 SendKey，跳过推送: %s", title)
        return False
    title = _title_prefix + title
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"https://sctapi.ftqq.com/{key}.send",
                data={"title": title, "desp": msg},
            )
            r.raise_for_status()
            data = r.json()
        # Server 酱 Turbo 版返回 {"code": 0, ...}
        if data.get("code") not in (0, None):
            log.warning("Server 酱返回异常: %s", data)
            return False
        return True
    except Exception as e:  # 推送失败绝不影响签到主流程
        log.warning("推送失败: %s", e)
        return False


async def notify_user_and_admin(title: str, msg: str, user_sendkey: str | None) -> None:
    """失败类事件：推管理员；用户填了个人 SendKey 则同时推本人。"""
    await notify(title, msg)
    if user_sendkey:
        await notify(title, msg, sendkey=user_sendkey)
