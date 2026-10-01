"""APScheduler 调度：两个 cron 签到任务 + 每小时可及性探测 + 每日日志清理（设计稿第 7 章）。

进程重启后任务自动恢复——错过即错过，重试窗口内自然覆盖，无需任务持久化。
仅公网版使用；个人版/宿舍版的调度外包给 Windows 计划任务（app/core/wintasks.py）。
"""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import models
from app.config import settings
from app.core import client, signer

log = logging.getLogger("lazy-clerk.scheduler")

scheduler = AsyncIOScheduler(timezone=settings.tz)


def _cleanup_logs() -> None:
    deleted = models.cleanup_logs(days=90)
    if deleted:
        log.info("清理 90 天前签到日志 %d 条", deleted)
    deleted = models.cleanup_probes(days=40)
    if deleted:
        log.info("清理 40 天前探测记录 %d 条", deleted)
    deleted = models.cleanup_attempts(days=90)
    if deleted:
        log.info("清理 90 天前签到尝试记录 %d 条", deleted)
    deleted = models.cleanup_dead_invites()
    if deleted:
        log.info("清理死邀请码 %d 个", deleted)
    deleted = models.cleanup_skip_flags()
    if deleted:
        log.info("清理过期手动取消标记 %d 个", deleted)


async def _probe_job() -> None:
    """每小时双端口探测并落库（登录页热力图数据源）。

    probe() 统一完成测量与出口调整（代理节点实测失败时全量重测换节点，
    app 是唯一决策大脑）；这里只维护状态机与告警：up（双端口可达）/ down。
    进入 down 需 20 秒后二次确认防抖动误报。
    """
    from app.core.client import probe
    from app.core.notify import notify

    p = await probe()
    if not p.ok and p.warmup:
        # 换节点缓冲期内的失败是暖机噪声，非可达性事实：不落库、不动状态机
        log.info("缓冲期内探测，跳过本次状态更新: %s", p.detail)
        return
    models.record_probe(p.ok, p.latency_ms, p.detail, p.channel)
    state = "up" if p.ok else "down"
    prev = models.get_setting("probe_last_state")

    if state == "down":
        if prev == "down":
            return  # 已知不可达，不重复告警
        await asyncio.sleep(20)
        p = await probe()  # 复核确认真的不通
        if p.warmup:
            log.info("复核处于缓冲期，跳过本次状态更新: %s", p.detail)
            return
        models.record_probe(p.ok, p.latency_ms, p.detail, p.channel)
        if not p.ok:
            models.set_setting("probe_last_state", "down")
            await notify("🚨医院系统暂时无法访问（定时探测）",
                         f"{p.detail}\n"
                         "故障期间的自动签到将自动取消，恢复后会另行通知。")
            return
        state = "up"

    models.set_setting("probe_last_state", state)
    if prev == "down":
        await notify("✅医院系统恢复可达", p.detail)


def start() -> None:
    # 拟人化：触发时间在基准点后 0–5 分钟内随机（jitter 只加不减，见
    # APScheduler _apply_jitter），即 5:00–5:05、13:00–13:05。
    # 打满全场：签到窗口（am 至 8:00、pm 至 14:30）内不限次重试，
    # 早开签是为了尽可能拉长拉锯覆盖面——若医院侧尚未开放签到，
    # 服务端拒绝按可重试失败处理，由重试循环等到窗口开放。
    scheduler.add_job(signer.sign_all, CronTrigger(hour=5, minute=0, jitter=300), args=["am"],
                      id="sign_am", name="上午签到")
    scheduler.add_job(signer.sign_all, CronTrigger(hour=13, minute=0, jitter=300), args=["pm"],
                      id="sign_pm", name="下午签到")
    scheduler.add_job(_cleanup_logs, CronTrigger(hour=3, minute=30),
                      id="cleanup", name="日志清理")
    # 每小时过 37 分探测（避开整点高峰，也避开签到窗口开局的前 20 分钟——
    # 日志实证 :07 探测会在 5:07/13:07 窗口关键期触发换节点），结果供热力图
    scheduler.add_job(_probe_job, CronTrigger(minute=37),
                      id="probe", name="可及性探测")
    # 启动后 30 秒补一次探测：mihomo 重启后 hospital 组默认 DIRECT、钉节点组
    # 默认订阅首个节点，都是未经实测的随机状态，尽快收敛到实测最优出口
    from datetime import datetime, timedelta
    scheduler.add_job(_probe_job, "date",
                      run_date=datetime.now() + timedelta(seconds=30),
                      id="probe_boot", name="启动探测")
    # 节点选点注入历史战绩（死节点禁用 + 延迟惩罚系数）；战绩是慢变量，
    # 每小时缓存，无需随签到节奏刷新
    client.set_node_scorer(models.node_score)
    scheduler.start()
    log.info("调度器已启动（时区 %s）", settings.tz)


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
