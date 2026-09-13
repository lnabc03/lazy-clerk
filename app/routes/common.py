"""Web 层公共工具：模板、鉴权、限流、跳转。"""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import models

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str, msg: str = "", error: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url, status_code=303)


def current_user(request: Request) -> models.User | None:
    uid = request.session.get("user_id")
    return models.get_user(uid) if uid else None


def is_admin(request: Request) -> bool:
    return bool(request.session.get("admin"))


# ---------- /login 防爆破限流：同一 IP 1 分钟内失败 5 次锁 10 分钟（内存计数） ----------

_FAIL_WINDOW = 60
_FAIL_LIMIT = 5
_LOCK_SECONDS = 600

_failures: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}


def login_locked(ip: str) -> int:
    """返回剩余锁定秒数，未锁定返回 0。"""
    until = _locked_until.get(ip, 0)
    return max(0, int(until - time.time()))


def record_login_failure(ip: str) -> None:
    now = time.time()
    fails = [t for t in _failures.get(ip, []) if now - t < _FAIL_WINDOW]
    fails.append(now)
    _failures[ip] = fails
    if len(fails) >= _FAIL_LIMIT:
        _locked_until[ip] = now + _LOCK_SECONDS
        _failures.pop(ip, None)


def clear_login_failures(ip: str) -> None:
    _failures.pop(ip, None)
    _locked_until.pop(ip, None)


def mask_account(account: str) -> str:
    """账号打掩码：前 2 后 2 位，短账号全掩。"""
    return account[:2] + "***" + account[-2:] if len(account) > 4 else "***"


def mask_sendkey(sendkey: str | None) -> str:
    if not sendkey:
        return "未设置"
    return sendkey[:6] + "***" if len(sendkey) > 6 else "***"


def next_run_text() -> str:
    """下次执行时间：下一个 6:57 或 13:57（实际触发在其前后 3 分钟内随机）。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from ..config import settings

    now = datetime.now(ZoneInfo(settings.tz))
    for candidate in (now.replace(hour=6, minute=57, second=0, microsecond=0),
                      now.replace(hour=13, minute=57, second=0, microsecond=0)):
        if candidate > now:
            return candidate.strftime("约 %Y-%m-%d %H:%M")
    return (now.replace(hour=6, minute=57, second=0, microsecond=0)
            + timedelta(days=1)).strftime("约 %Y-%m-%d %H:%M")
