"""APScheduler 调度：两个 cron 签到任务 + 每日日志清理（设计稿第 7 章）。

进程重启后任务自动恢复——错过即错过，重试窗口内自然覆盖，无需任务持久化。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .. import models
from ..config import settings
from . import signer

log = logging.getLogger("lazy-clerk.scheduler")

scheduler = AsyncIOScheduler(timezone=settings.tz)


def _cleanup_logs() -> None:
    deleted = models.cleanup_logs(days=90)
    if deleted:
        log.info("清理 90 天前签到日志 %d 条", deleted)


def start() -> None:
    scheduler.add_job(signer.sign_all, CronTrigger(hour=6, minute=58), args=["am"],
                      id="sign_am", name="上午签到")
    scheduler.add_job(signer.sign_all, CronTrigger(hour=13, minute=58), args=["pm"],
                      id="sign_pm", name="下午签到")
    scheduler.add_job(_cleanup_logs, CronTrigger(hour=3, minute=30),
                      id="cleanup", name="日志清理")
    scheduler.start()
    log.info("调度器已启动（时区 %s）", settings.tz)


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
