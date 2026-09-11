"""用户侧路由：/login /logout /me /me/sendkey /me/sign-now。

登录凭据即医院工号 + 医院密码（与 users 表明文比对，设计稿 6.3）。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Form, Request

from .. import models
from ..core import signer
from .common import (clear_login_failures, current_user, login_locked,
                     mask_sendkey, next_run_text, record_login_failure,
                     redirect, render)

router = APIRouter()


@router.get("/")
async def index(request: Request):
    return redirect("/me" if current_user(request) else "/login")


@router.get("/login")
async def login_page(request: Request, msg: str = "", error: str = ""):
    if current_user(request):
        return redirect("/me")
    return render(request, "login.html", msg=msg, error=error)


@router.post("/login")
async def login_submit(request: Request, account: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    locked = login_locked(ip)
    if locked:
        return redirect("/login", error=f"失败次数过多，请 {locked // 60 + 1} 分钟后再试")

    user = models.get_user_by_account(account.strip())
    if not user or user.password != password:
        record_login_failure(ip)
        return redirect("/login", error="账号或密码错误")
    if not user.enabled:
        return redirect("/login", error="账号已停用，请联系管理员")

    clear_login_failures(ip)
    request.session.clear()
    request.session["user_id"] = user.id
    return redirect("/me")


@router.post("/change-password")
async def change_password(request: Request, account: str = Form(...),
                          password: str = Form(...), password2: str = Form(...)):
    """更正密码（无需登录，凭医院平台真实认证）：登录页入口。

    该密码同时是本站登录凭据与医院签到凭据。先对 SSO 认证新密码，
    通过才更新入库——与注册同一套验证逻辑。
    """
    from .register import verify_hospital_account

    account = account.strip()
    if len(password) < 4 or password != password2:
        return redirect("/login", error="密码过短或两次输入不一致")
    user = models.get_user_by_account(account)
    if not user:
        return redirect("/login", error="该账号未注册")
    err = await verify_hospital_account(account, password)
    if err:
        return redirect("/login", error=f"医院平台认证未通过：{err}")
    models.update_password(user.id, password)
    return redirect("/login", msg="密码已更正，请用新密码登录")


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return redirect("/login", msg="已退出登录")


@router.get("/me")
async def me(request: Request, msg: str = "", error: str = ""):
    user = current_user(request)
    if not user:
        return redirect("/login")
    return render(request, "me.html", msg=msg, error=error, user=user,
                  sendkey_masked=mask_sendkey(user.sendkey),
                  next_run=next_run_text(),
                  logs=models.logs_for_user(user.id, days=7))


@router.post("/me/sendkey")
async def update_sendkey(request: Request, sendkey: str = Form("")):
    user = current_user(request)
    if not user:
        return redirect("/login")
    models.update_sendkey(user.id, sendkey.strip() or None)
    return redirect("/me", msg="SendKey 已更新")


@router.post("/me/sign-now")
async def sign_now(request: Request):
    user = current_user(request)
    if not user:
        return redirect("/login")
    period = signer.current_period()
    outcome = await signer.sign_user_once(user, period)
    today = datetime.now(signer.TZ).strftime("%Y-%m-%d")
    models.add_log(user.id, today, period, outcome.result, f"[手动] {outcome.message}")
    await signer.push_manual_result(user, period, outcome)  # 手动触发必推，兼测连通性
    if outcome.result in (signer.RESULT_SUCCESS, signer.RESULT_SKIPPED, signer.RESULT_NO_SCHEDULE):
        return redirect("/me", msg=outcome.message)
    return redirect("/me", error=outcome.message)
