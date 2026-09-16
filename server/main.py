"""公网版 FastAPI 入口：挂载路由与调度器（设计稿第 3 章）。

共享层在 app/（core/models/db/config + Web 公共工具），本目录只装公网版专属：
路由、页面模板、APScheduler 调度、部署文件。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app import models
from app.config import settings

from . import scheduler
from .routes import admin, register, user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx 每请求一条 INFO（含 mihomo API 轮询），噪音远大于价值
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lazy-clerk")

app = FastAPI(title="lazy-clerk", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    max_age=7 * 24 * 3600,  # 7 天滑动过期
    same_site="lax",
)
app.include_router(register.router)
app.include_router(user.router)
app.include_router(admin.router)


@app.on_event("startup")
async def startup() -> None:
    from app import db
    db.init()
    # 管理员密码：首次启动哈希入库，此后仅校验（改密码在管理页"管理员设置"）
    if settings.admin_password and not models.get_setting("admin_password_hash"):
        if len(settings.admin_password) < 12:
            log.warning("ADMIN_PASSWORD 少于 12 位，建议加强（README 要求 ≥12 位）")
        models.set_setting("admin_password_hash", models.sha256(settings.admin_password))
        log.info("管理员密码已哈希入库")
    if not settings.admin_password:
        log.warning("未配置 ADMIN_PASSWORD，管理页不可用")
    scheduler.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    scheduler.shutdown()
