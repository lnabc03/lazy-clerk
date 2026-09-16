"""Web 层公共工具：模板、鉴权、限流、跳转。"""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import models

# 模板搜索路径：server/templates（公网版页面）→ app/templates（共享 base/admin_login）。
# 宿舍版用自己的模板环境（dorm/web.py），不走这里。
_ROOT = Path(__file__).parent.parent.parent
_template_dirs = [str(d) for d in (_ROOT / "server" / "templates", _ROOT / "app" / "templates")
                  if d.is_dir()]
templates = Jinja2Templates(directory=_template_dirs)


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


# ---------- 登录防爆破限流：同一 IP 1 分钟内失败 5 次锁 10 分钟（内存计数） ----------
# scope 区分入口（用户登录 / 管理员登录），互不影响

_FAIL_WINDOW = 60
_FAIL_LIMIT = 5
_LOCK_SECONDS = 600

_failures: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}


def login_locked(ip: str, scope: str = "user") -> int:
    """返回剩余锁定秒数，未锁定返回 0。"""
    until = _locked_until.get(f"{scope}:{ip}", 0)
    return max(0, int(until - time.time()))


def record_login_failure(ip: str, scope: str = "user") -> None:
    key = f"{scope}:{ip}"
    now = time.time()
    fails = [t for t in _failures.get(key, []) if now - t < _FAIL_WINDOW]
    fails.append(now)
    _failures[key] = fails
    if len(fails) >= _FAIL_LIMIT:
        _locked_until[key] = now + _LOCK_SECONDS
        _failures.pop(key, None)


def clear_login_failures(ip: str, scope: str = "user") -> None:
    key = f"{scope}:{ip}"
    _failures.pop(key, None)
    _locked_until.pop(key, None)


def mask_account(account: str) -> str:
    """账号打掩码：前 2 后 2 位，短账号全掩。"""
    return account[:2] + "***" + account[-2:] if len(account) > 4 else "***"


def mask_sendkey(sendkey: str | None) -> str:
    if not sendkey:
        return "未设置"
    return sendkey[:6] + "***" if len(sendkey) > 6 else "***"


# ---------- 管理页账号总览徽标（公网版/宿舍版共用） ----------

RESULT_LABEL = {
    "success": "成功", "failed": "失败", "skipped": "已签过",
    "no_schedule": "无需签到", "manual": "需人工",
}
RESULT_COLOR = {
    "success": "success", "skipped": "success", "no_schedule": "success",
    "failed": "danger", "manual": "warning",
}


def make_badge(log) -> dict | None:
    """把一条日志转成徽标显示。checked（状态检测）直接显示真实状态文本。"""
    if log is None:
        return None
    result, message = log["result"], (log["message"] or "")
    if result == "checked":
        text = message.replace("[检测] ", "")
        good = any(k in text for k in ("已签到", "已确认", "无排班", "借假"))
        return {"text": text, "color": "success" if good else "danger", "title": message}
    return {"text": RESULT_LABEL.get(result, result),
            "color": RESULT_COLOR.get(result, "secondary"), "title": message}


def next_run_text() -> str:
    """下次执行时间：下一个 6:53 或 13:53（实际触发在其后 0–5 分钟内随机，不晚于 :58）。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from ..config import settings

    now = datetime.now(ZoneInfo(settings.tz))
    for candidate in (now.replace(hour=6, minute=53, second=0, microsecond=0),
                      now.replace(hour=13, minute=53, second=0, microsecond=0)):
        if candidate > now:
            return candidate.strftime("约 %Y-%m-%d %H:%M")
    return (now.replace(hour=6, minute=53, second=0, microsecond=0)
            + timedelta(days=1)).strftime("约 %Y-%m-%d %H:%M")
