"""管理员路由：登录、账号总览、启用/停用、删除、邀请码、签到状态检测、手动签到、连通性探测。"""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from app import models
from app.config import settings
from app.core import signer
from app.routes.common import (clear_login_failures, is_admin, login_locked,
                               make_badge, mask_account, mask_sendkey,
                               record_login_failure, redirect, render)

router = APIRouter(prefix="/admin")


@router.get("/login")
async def admin_login_page(request: Request, msg: str = "", error: str = ""):
    return render(request, "admin_login.html", msg=msg, error=error,
                  configured=bool(models.get_setting("admin_password_hash")))


@router.post("/login")
async def admin_login(request: Request, password: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    locked = login_locked(ip, scope="admin")
    if locked:
        return redirect("/admin/login", error=f"失败次数过多，请 {locked // 60 + 1} 分钟后再试")
    stored = models.get_setting("admin_password_hash")
    if not stored:
        return redirect("/admin/login", error="未配置管理员密码（ADMIN_PASSWORD）")
    if models.sha256(password) != stored:
        record_login_failure(ip, scope="admin")
        return redirect("/admin/login", error="密码错误")
    clear_login_failures(ip, scope="admin")
    request.session.clear()
    request.session["admin"] = True
    return redirect("/admin")


@router.post("/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return redirect("/admin/login", msg="已退出")


@router.get("")
async def admin_page(request: Request, msg: str = "", error: str = ""):
    if not is_admin(request):
        return redirect("/admin/login")
    from app.core.client import mihomo_group
    today = models.today_results()
    invite_by_user = models.invite_of_users()
    users = [{
        "user": u,
        "account_masked": mask_account(u.account),
        "invite": invite_by_user.get(u.id),
        "am": make_badge(today.get(u.id, {}).get("am")),
        "pm": make_badge(today.get(u.id, {}).get("pm")),
    } for u in models.list_users()]
    admin_sendkey = (models.get_setting("serverchan_sendkey")
                     or settings.serverchan_sendkey)
    return render(request, "admin.html", msg=msg, error=error,
                  users=users,
                  unused_invites=models.list_unused_invites(),
                  admin_sendkey_masked=mask_sendkey(admin_sendkey),
                  heatmap=models.probe_heatmap(days=7),
                  probe_latest=models.latest_probe(),
                  summary=models.service_summary(),
                  sessions=models.session_analysis(days=7),
                  probe_url="/admin/probe",
                  proxy_configured=bool(settings.proxy_url),
                  proxy_info=await mihomo_group() if settings.mihomo_api else None)


@router.post("/proxy/switch")
async def proxy_switch(request: Request, target: str = Form(...)):
    """切换代理出口：DIRECT 恢复直连优先，auto 立即全量重测并钉最快节点，节点名手动钉选。"""
    if not is_admin(request):
        return redirect("/admin/login")
    from app.core.client import mihomo_switch, strip_node_flag
    label = {"DIRECT": "直连", "auto": "自动重选最快节点"}.get(target, strip_node_flag(target))
    err = await mihomo_switch(target)
    if err:
        return redirect("/admin", error=f"切换失败：{err}")
    return redirect("/admin", msg=f"出口已切换：{label}")


@router.post("/users/{user_id}/toggle")
async def toggle_user(request: Request, user_id: int):
    if not is_admin(request):
        return redirect("/admin/login")
    user = models.get_user(user_id)
    if user:
        models.set_user_enabled(user_id, not user.enabled)
        return redirect("/admin", msg=f"{user.nickname} 已{'启用' if not user.enabled else '停用'}")
    return redirect("/admin", error="用户不存在")


@router.delete("/users/{user_id}")
async def remove_user(request: Request, user_id: int):
    if not is_admin(request):
        return redirect("/admin/login")
    models.delete_user(user_id)
    return {"ok": True}


@router.post("/invites/generate")
async def generate_invite(request: Request):
    """生成 1 个邀请码，明文常驻管理页（未使用列表）。"""
    if not is_admin(request):
        return redirect("/admin/login")
    code = models.generate_invite()
    return redirect("/admin", msg=f"已生成邀请码 {code}")


@router.post("/invites/{invite_id}/delete")
async def delete_invite(request: Request, invite_id: int):
    if not is_admin(request):
        return redirect("/admin/login")
    if models.delete_invite(invite_id):
        return redirect("/admin", msg="邀请码已删除")
    return redirect("/admin", error="该邀请码已被使用，不可删除")


@router.post("/probe")
async def probe_now(request: Request):
    """手动探测医院系统连通性（匿名 GET SSO 首页），结果入 probe_logs 供热力图。"""
    if not is_admin(request):
        return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
    from app.core.client import probe
    p = await probe()
    models.record_probe(p.ok, p.latency_ms, p.detail, p.channel)
    status = "✅ 可达" if p.ok else "❌ 不可达"
    via = "（代理出口）" if p.ok and p.channel == "proxy" else ""
    return {"ok": True, "msg": f"{status}：{p.detail}（{p.latency_ms}ms）{via}"}


@router.post("/check-one/{user_id}")
async def check_one(request: Request, user_id: int):
    """签到状态检测（单账号）：前端逐个串行调用，每完成一个立即落库并刷新该行。

    历史教训：全账号合并在一个请求里（哪怕限并发 3 + 错峰），代理链路抖动时
    总时长会顶破网关 60 秒超时——浏览器拿到 504 页面崩掉，完成了一半也看不见。
    代理环境下并发反而放大失败率（同节点多连接抽签，中转后端逐请求随机失败）。
    拆成单账号短请求后：每个请求独立成败，单个 504 只影响一行，页面不崩；
    账号间 3 秒间隔由前端控制。check_user_status 内部已捕获全部异常并落库，
    失败也会以红色徽标回显。
    """
    if not is_admin(request):
        return JSONResponse({"ok": False, "msg": "未登录"}, status_code=401)
    user = models.get_user(user_id)
    if not user:
        return JSONResponse({"ok": False, "msg": "用户不存在"}, status_code=404)
    try:
        checked = await signer.check_user_status(user)
    except Exception as e:
        return JSONResponse({"ok": False, "msg": f"{type(e).__name__}: {e}"})
    today = models.today_results().get(user.id, {})
    return {"ok": True, "checked": checked,
            "badges": {p: make_badge(today.get(p)) for p in ("am", "pm")}}


@router.post("/settings/sendkey")
async def set_admin_sendkey(request: Request, sendkey: str = Form("")):
    """更新管理员 SendKey（入库覆盖环境变量；留空清除覆盖，回落 .env）。"""
    if not is_admin(request):
        return redirect("/admin/login")
    models.set_setting("serverchan_sendkey", sendkey.strip())
    return redirect("/admin", msg="管理员 SendKey 已更新" if sendkey.strip()
                    else "已清除页面配置，回落 .env 中的 SendKey")


@router.post("/settings/password")
async def set_admin_password(request: Request, old_password: str = Form(...),
                             new_password: str = Form(...), new_password2: str = Form(...)):
    """修改管理员密码（立即生效，无需重启）。"""
    if not is_admin(request):
        return redirect("/admin/login")
    stored = models.get_setting("admin_password_hash")
    if not stored or models.sha256(old_password) != stored:
        return redirect("/admin", error="原密码错误")
    if len(new_password) < 8 or new_password != new_password2:
        return redirect("/admin", error="新密码至少 8 位且两次输入需一致")
    models.set_setting("admin_password_hash", models.sha256(new_password))
    return redirect("/admin", msg="管理员密码已修改")


@router.post("/sign-now/{user_id}")
async def admin_sign_now(request: Request, user_id: int):
    if not is_admin(request):
        return redirect("/admin/login")
    user = models.get_user(user_id)
    if not user:
        return redirect("/admin", error="用户不存在")
    outcome = await signer.sign_user_manual(user, log_prefix="[管理员手动]")
    if outcome.result in (signer.RESULT_SUCCESS, signer.RESULT_SKIPPED, signer.RESULT_NO_SCHEDULE):
        return redirect("/admin", msg=f"{user.nickname}: {outcome.message}")
    return redirect("/admin", error=f"{user.nickname}: {outcome.message}")
