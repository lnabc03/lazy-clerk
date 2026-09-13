"""宿舍版管理 Web：仅管理员，随用随开（127.0.0.1:8787）。

签到由 Windows 计划任务执行（见 dorm/main.py），本进程只做管理面：
账号管理、状态总览与检测、日志查看、管理员设置。崩溃或关闭不影响签到。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import webbrowser

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from starlette.middleware.sessions import SessionMiddleware

from app import models
from app.config import settings
from app.core import signer
from app.core.client import verify_account
from app.routes.common import (clear_login_failures, is_admin, login_locked,
                               make_badge, mask_account, mask_sendkey,
                               record_login_failure, redirect)

log = logging.getLogger("lazy-clerk.dorm")


def bundle_dir() -> str:
    """打包后模板/静态资源在 PyInstaller 解压目录，源码时在项目根目录。"""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_env = Environment(
    loader=ChoiceLoader([
        FileSystemLoader(os.path.join(bundle_dir(), "dorm", "templates")),
        FileSystemLoader(os.path.join(bundle_dir(), "app", "templates")),
    ]),
    autoescape=select_autoescape(["html"]),
)
templates = Jinja2Templates(env=_env)


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, ctx)


app = FastAPI(title="lazy-clerk 宿舍版", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    max_age=7 * 24 * 3600,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=os.path.join(bundle_dir(), "app", "static")),
          name="static")


def open_browser_later(url: str = "http://127.0.0.1:8787/admin") -> None:
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()


PERIOD_TEXT = {"am": "上午", "pm": "下午"}
LOG_RESULT_TEXT = {
    "success": "成功", "failed": "失败", "skipped": "已签过",
    "no_schedule": "无需签到", "manual": "需人工", "checked": "检测",
}
LOG_RESULT_COLOR = {
    "success": "success", "skipped": "success", "no_schedule": "success",
    "failed": "danger", "manual": "warning", "checked": "info",
}


# ---------- 登录 ----------

@app.get("/")
async def index(request: Request):
    return redirect("/admin" if is_admin(request) else "/admin/login")


@app.get("/admin/login")
async def login_page(request: Request, msg: str = "", error: str = ""):
    if is_admin(request):
        return redirect("/admin")
    return render(request, "admin_login.html", msg=msg, error=error, configured=True)


@app.post("/admin/login")
async def login_submit(request: Request, password: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    locked = login_locked(ip, scope="admin")
    if locked:
        return redirect("/admin/login", error=f"失败次数过多，请 {locked // 60 + 1} 分钟后再试")
    stored = models.get_setting("admin_password_hash")
    if not stored or models.sha256(password) != stored:
        record_login_failure(ip, scope="admin")
        return redirect("/admin/login", error="密码错误")
    clear_login_failures(ip, scope="admin")
    request.session.clear()
    request.session["admin"] = True
    return redirect("/admin")


@app.post("/admin/logout")
async def logout(request: Request):
    request.session.clear()
    return redirect("/admin/login", msg="已退出")


# ---------- 管理页 ----------

@app.get("/admin")
async def admin_page(request: Request, msg: str = "", error: str = ""):
    if not is_admin(request):
        return redirect("/admin/login")
    today = models.today_results()
    users = [{
        "user": u,
        "account_masked": mask_account(u.account),
        "am": make_badge(today.get(u.id, {}).get("am")),
        "pm": make_badge(today.get(u.id, {}).get("pm")),
    } for u in models.list_users()]
    admin_sendkey = (models.get_setting("serverchan_sendkey")
                     or settings.serverchan_sendkey)
    return render(request, "admin.html", msg=msg, error=error,
                  users=users,
                  logs=models.recent_logs(50),
                  period_text=PERIOD_TEXT,
                  log_result_text=LOG_RESULT_TEXT,
                  log_result_color=LOG_RESULT_COLOR,
                  admin_sendkey_masked=mask_sendkey(admin_sendkey))


# ---------- 账号管理 ----------

@app.post("/admin/users/add")
async def add_user(request: Request, nickname: str = Form(""), account: str = Form(...),
                   password: str = Form(...), sendkey: str = Form("")):
    """新增账号：先通过医院 SSO 真实认证，通过才入库（与注册同一套验证）。"""
    if not is_admin(request):
        return redirect("/admin/login")
    account = account.strip()
    if not account or not password:
        return redirect("/admin", error="工号和密码不能为空")
    if models.get_user_by_account(account):
        return redirect("/admin", error="该工号已存在")
    err = await verify_account(account, password)
    if err:
        return redirect("/admin", error=f"医院平台认证未通过：{err}")
    models.create_user(nickname.strip() or account, account, password,
                       sendkey.strip() or None)
    return redirect("/admin", msg=f"账号 {account} 已添加")


@app.post("/admin/users/{user_id}/password")
async def set_user_password(request: Request, user_id: int, password: str = Form(...)):
    """改账号密码：同样先过医院 SSO 认证。fetch 调用，返回 JSON。"""
    if not is_admin(request):
        return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
    user = models.get_user(user_id)
    if not user:
        return JSONResponse({"ok": False, "msg": "用户不存在"})
    if len(password) < 4:
        return JSONResponse({"ok": False, "msg": "密码过短"})
    err = await verify_account(user.account, password)
    if err:
        return JSONResponse({"ok": False, "msg": f"医院平台认证未通过：{err}"})
    models.update_password(user_id, password)
    return JSONResponse({"ok": True, "msg": f"{user.nickname} 的密码已更新"})


@app.post("/admin/users/{user_id}/sendkey")
async def set_user_sendkey(request: Request, user_id: int, sendkey: str = Form("")):
    """录入/清除账号的微信推送 SendKey。fetch 调用，返回 JSON。"""
    if not is_admin(request):
        return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
    user = models.get_user(user_id)
    if not user:
        return JSONResponse({"ok": False, "msg": "用户不存在"})
    models.update_sendkey(user_id, sendkey.strip() or None)
    return JSONResponse({"ok": True, "msg": f"{user.nickname} 的 SendKey 已更新"})


@app.post("/admin/users/{user_id}/toggle")
async def toggle_user(request: Request, user_id: int):
    if not is_admin(request):
        return redirect("/admin/login")
    user = models.get_user(user_id)
    if user:
        models.set_user_enabled(user_id, not user.enabled)
        return redirect("/admin", msg=f"{user.nickname} 已{'启用' if not user.enabled else '停用'}")
    return redirect("/admin", error="用户不存在")


@app.delete("/admin/users/{user_id}")
async def remove_user(request: Request, user_id: int):
    if not is_admin(request):
        return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
    models.delete_user(user_id)
    return JSONResponse({"ok": True})


# ---------- 签到操作 ----------

@app.post("/admin/sign-now/{user_id}")
async def sign_now(request: Request, user_id: int):
    if not is_admin(request):
        return redirect("/admin/login")
    user = models.get_user(user_id)
    if not user:
        return redirect("/admin", error="用户不存在")
    outcome = await signer.sign_user_manual(user, log_prefix="[管理员手动]")
    if outcome.result in (signer.RESULT_SUCCESS, signer.RESULT_SKIPPED, signer.RESULT_NO_SCHEDULE):
        return redirect("/admin", msg=f"{user.nickname}: {outcome.message}")
    return redirect("/admin", error=f"{user.nickname}: {outcome.message}")


@app.post("/admin/check-all")
async def check_all(request: Request):
    """签到状态检测：逐一登录医院系统，刷新所有启用账号的当日真实状态。"""
    if not is_admin(request):
        return redirect("/admin/login")
    users = [u for u in models.list_users() if u.enabled]
    for u in users:
        try:
            await signer.check_user_status(u)
        except Exception:
            log.exception("状态检测失败 user=%s", u.account)
    return redirect("/admin", msg=f"已检测 {len(users)} 个启用账号，结果见总览表")


# ---------- 管理员设置 ----------

@app.post("/admin/settings/sendkey")
async def set_admin_sendkey(request: Request, sendkey: str = Form("")):
    if not is_admin(request):
        return redirect("/admin/login")
    models.set_setting("serverchan_sendkey", sendkey.strip())
    return redirect("/admin", msg="管理员 SendKey 已更新" if sendkey.strip()
                    else "管理员 SendKey 已清除")


@app.post("/admin/settings/password")
async def set_admin_password(request: Request, old_password: str = Form(...),
                             new_password: str = Form(...), new_password2: str = Form(...)):
    if not is_admin(request):
        return redirect("/admin/login")
    stored = models.get_setting("admin_password_hash")
    if not stored or models.sha256(old_password) != stored:
        return redirect("/admin", error="原密码错误")
    if len(new_password) < 8 or new_password != new_password2:
        return redirect("/admin", error="新密码至少 8 位且两次输入需一致")
    models.set_setting("admin_password_hash", models.sha256(new_password))
    return redirect("/admin", msg="管理员密码已修改")
