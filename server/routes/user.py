"""用户侧路由：/login /logout /me /me/sendkey /me/sign-now /me/toggle /me/delete。

登录凭据即医院工号 + 医院密码（与 users 表明文比对，设计稿 6.3）。
"""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from app import models
from app.core import signer
from app.routes.common import (clear_login_failures, current_user, login_locked,
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
    return render(request, "login.html", msg=msg, error=error,
                  heatmap=models.probe_heatmap(days=7),
                  probe_latest=models.latest_probe())


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
    # 停用账号允许登录（仅暂停自动签到），用户可随时自助改回；
    # 要彻底移出系统请用删除（管理页或 /me 注销）。

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
    from app.core.client import verify_account

    # 本接口会对医院 SSO 发起真实认证：限流在一切校验之前，防被当 SSO 放大器
    ip = request.client.host if request.client else "unknown"
    locked = login_locked(ip, scope="sso")
    if locked:
        return redirect("/login", error=f"操作过于频繁，请 {locked // 60 + 1} 分钟后再试")
    record_login_failure(ip, scope="sso")
    account = account.strip()
    if len(password) < 4 or password != password2:
        return redirect("/login", error="密码过短或两次输入不一致")
    user = models.get_user_by_account(account)
    if not user:
        return redirect("/login", error="该账号未注册")
    err = await verify_account(account, password)
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
                  logs=models.logs_for_user(user.id, days=7),
                  heatmap=models.probe_heatmap(days=7),
                  probe_latest=models.latest_probe(),
                  probe_url="/me/probe")


@router.post("/me/probe")
async def probe_self(request: Request):
    """用户自助探测医院系统连通性（匿名 GET SSO 首页），结果入 probe_logs。fetch 调用。"""
    if not current_user(request):
        return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
    from app.core.client import probe
    p = await probe()
    models.record_probe(p.ok, p.latency_ms, p.detail, p.channel)
    status = "✅ 可达" if p.ok else "❌ 不可达"
    via = "（代理出口）" if p.ok and p.channel == "proxy" else ""
    return JSONResponse({"ok": True, "msg": f"{status}：{p.detail}（{p.latency_ms}ms）{via}"})


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
    outcome = await signer.sign_user_manual(user)
    if outcome.result in (signer.RESULT_SUCCESS, signer.RESULT_SKIPPED, signer.RESULT_NO_SCHEDULE):
        return redirect("/me", msg=outcome.message)
    return redirect("/me", error=outcome.message)


@router.post("/me/toggle")
async def toggle_self(request: Request):
    """自助停用/启用自动签到（与管理页账号开关同一语义）。

    停用仅暂停自动签到，不影响登录，用户可随时改回。
    """
    user = current_user(request)
    if not user:
        return redirect("/login")
    models.set_user_enabled(user.id, not user.enabled)
    return redirect("/me", msg=f"自动签到已{'启用' if not user.enabled else '停用'}")


@router.post("/me/delete")
async def delete_self(request: Request):
    """注销：删除本账号及全部签到日志（与管理页删除同一语义），随后退出登录。"""
    user = current_user(request)
    if not user:
        return redirect("/login")
    models.delete_user(user.id)
    request.session.clear()
    return redirect("/login", msg="账号已注销，感谢使用")
