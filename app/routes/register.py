"""用户注册（邀请码 + 医院账号验证）。注册成功即写 session 并跳转 /me。

注册前先用所填工号/密码对医院统一登录平台做一次真实认证（设计稿 v0.3）：
认证不通过则拒绝注册并展示 SSO 返回的原因（如弱密码拦截、密码错误）。
"""
from __future__ import annotations

from fastapi import APIRouter, Form, Request

from .. import models
from ..core.client import AuthError, HospitalClient, LoginError
from .common import redirect, render

router = APIRouter()


async def verify_hospital_account(account: str, password: str) -> str | None:
    """对医院 SSO 做一次真实认证。通过返回 None，否则返回可读错误信息。"""
    try:
        async with HospitalClient(account, password) as client:
            await client.login()
        return None
    except AuthError as e:
        return str(e)
    except LoginError as e:
        return f"医院平台连接异常: {e}"
    except Exception as e:
        return f"验证异常: {type(e).__name__}: {e}"


@router.get("/register")
async def register_page(request: Request, msg: str = "", error: str = ""):
    return render(request, "register.html", msg=msg, error=error)


@router.post("/register")
async def register_submit(
    request: Request,
    nickname: str = Form(...),
    account: str = Form(...),
    password: str = Form(...),
    invite_code: str = Form(...),
    sendkey: str = Form(""),
):
    nickname, account = nickname.strip(), account.strip()
    if not all([nickname, account, password, invite_code.strip()]):
        return redirect("/register", error="昵称、账号、密码、邀请码均为必填")
    if len(password) < 4:
        return redirect("/register", error="密码过短")
    if models.get_user_by_account(account):
        return redirect("/register", error="该账号已注册")

    # 先验证医院账号密码，通过才允许注册
    err = await verify_hospital_account(account, password)
    if err:
        return redirect("/register", error=f"医院平台认证未通过：{err}")

    # 先建号再核销邀请码；邀请码无效时回滚建号
    user_id = models.create_user(nickname, account, password, sendkey.strip() or None)
    if not models.use_invite(invite_code, user_id):
        models.delete_user(user_id)
        return redirect("/register", error="邀请码无效或已被使用")

    request.session.clear()
    request.session["user_id"] = user_id
    return redirect("/me", msg="注册成功，已自动登录")
