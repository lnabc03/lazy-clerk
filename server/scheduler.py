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
from app.core import signer

log = logging.getLogger("lazy-clerk.scheduler")

scheduler = AsyncIOScheduler(timezone=settings.tz)


def _cleanup_logs() -> None:
    deleted = models.cleanup_logs(days=90)
    if deleted:
        log.info("清理 90 天前签到日志 %d 条", deleted)
    deleted = models.cleanup_probes(days=40)
    if deleted:
        log.info("清理 40 天前探测记录 %d 条", deleted)
    deleted = models.cleanup_dead_invites()
    if deleted:
        log.info("清理死邀请码 %d 个", deleted)


async def _probe_job() -> None:
    """每小时探测医院系统可及性并落库（登录页热力图数据源）。

    状态机：direct（直连正常）/ proxy（代理逃生中）/ down（不可达），
    翻转时通知管理员——进入 down 需二次确认防抖动误报；出口在直连与
    代理间切换也会播报（说明直连疑似被封或已解封）。匿名 GET + 每小时
    一次，远低于正常浏览量，不增封禁概率。
    """
    from app.core.client import probe
    from app.core.notify import notify

    p = await probe(switch_on_fail=False)  # 首探不逃生：直连秒级抖动不该触发切代理
    models.record_probe(p.ok, p.latency_ms, p.detail, p.channel)
    state = "down" if not p.ok else p.channel
    prev = models.get_setting("probe_last_state")

    if state == "down":
        if prev == "down":
            return  # 已知不可达，不重复告警
        await asyncio.sleep(20)
        p = await probe()  # 复核允许切代理逃生：直连真被封则走代理并标蓝
        models.record_probe(p.ok, p.latency_ms, p.detail, p.channel)
        if not p.ok:
            models.set_setting("probe_last_state", "down")
            await notify("🚨医院系统不可达（定时探测）",
                         f"连续两次探测失败：{p.detail}\n"
                         "直连与代理均不可用，签到将自动跳过，请关注节点状态。")
            return
        state = p.channel

    if state == prev:
        return
    models.set_setting("probe_last_state", state)
    if prev == "down":
        await notify("✅医院系统恢复可达",
                     f"出口：{'直连' if state == 'direct' else '代理'}，"
                     f"{p.detail}（{p.latency_ms}ms）")
    elif prev and prev != state:
        # direct ↔ proxy 切换：代理逃生启动或直连恢复
        if state == "proxy":
            await notify("🔀已切换到代理出口",
                         "直连疑似被医院防火墙拦截，流量经代理节点出入，"
                         "签到不受影响。直连恢复后会自动切回。")
        else:
            await notify("🔀已恢复直连出口", f"直连恢复可用，{p.detail}（{p.latency_ms}ms）")


def start() -> None:
    # 拟人化：触发时间在基准点后 0–5 分钟内随机（jitter 只加不减，见
    # APScheduler _apply_jitter），即 6:53–6:58、13:00–13:05。
    # 上午上限不晚于 :58——签到约花 10 秒，保证 7:00 医院系统提醒发出前已完成；
    # 下午窗口 13:00 开启即签（14:00 提醒前留足缓冲）
    scheduler.add_job(signer.sign_all, CronTrigger(hour=6, minute=53, jitter=300), args=["am"],
                      id="sign_am", name="上午签到")
    scheduler.add_job(signer.sign_all, CronTrigger(hour=13, minute=0, jitter=300), args=["pm"],
                      id="sign_pm", name="下午签到")
    scheduler.add_job(_cleanup_logs, CronTrigger(hour=3, minute=30),
                      id="cleanup", name="日志清理")
    # 每小时过 7 分探测（避开整点高峰），结果供登录页热力图
    scheduler.add_job(_probe_job, CronTrigger(minute=7),
                      id="probe", name="可及性探测")
    scheduler.start()
    log.info("调度器已启动（时区 %s）", settings.tz)


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
