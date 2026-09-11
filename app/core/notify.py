"""Server 酱推送。单接口封装，一个 POST 完事。"""
from __future__ import annotations

import logging

import httpx

from ..config import settings

log = logging.getLogger("lazy-clerk.notify")


async def notify(title: str, msg: str, sendkey: str | None = None) -> bool:
    """推送消息。优先级：参数 sendkey > 管理页配置的 SendKey > 环境变量；都无则记录告警。"""
    from .. import models  # 延迟 import 避免循环

    key = sendkey or models.get_setting("serverchan_sendkey") or settings.serverchan_sendkey
    if not key:
        log.warning("未配置 SendKey，跳过推送: %s", title)
        return False
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
