"""FastAPI 入口：挂载路由与调度器（设计稿第 3 章）。"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import models
from .config import settings
from .core.scheduler import shutdown as scheduler_shutdown
from .core.scheduler import start as scheduler_start
from .routes import admin, register, user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lazy-clerk")

app = FastAPI(title="lazy-clerk", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    max_age=7 * 24 * 3600,  # 7 天滑动过期
    same_site="lax",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(register.router)
app.include_router(user.router)
app.include_router(admin.router)


@app.on_event("startup")
async def startup() -> None:
    from . import db
    db.init()
    # 管理员密码：首次启动哈希入库，此后仅校验（改密码需改环境变量并清库或手动更新）
    if settings.admin_password and not models.get_setting("admin_password_hash"):
        models.set_setting("admin_password_hash", models.sha256(settings.admin_password))
        log.info("管理员密码已哈希入库")
    if not settings.admin_password:
        log.warning("未配置 ADMIN_PASSWORD，管理页不可用")
    scheduler_start()


@app.on_event("shutdown")
async def shutdown() -> None:
    scheduler_shutdown()
