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

# 判红确认序列：初测红后第 20s / 170s / 300s 三次复核（全程约 5 分钟），
# 全红才告警。分钟级风控抖动（实测 3-16 分钟）因此不再产生告警对
_CONFIRM_WAITS = (20, 150, 130)
_probe_lock = asyncio.Lock()  # 复核期间整点任务跳过：状态机只允许一个写入者


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


def _in_sign_window() -> bool:
    """当前是否处于签到窗口（含赛前半小时准备期）。

    窗口内的故障由赛前守卫/门控的取消广播覆盖（broadcast_round_cancelled
    每人一条），定时探测再告警就是同一件事发两条（2026-10-02 上午实测
    困扰）。静默期状态机照常维护，恢复通知不受影响。"""
    from datetime import datetime, time as dtime
    now = datetime.now(models.TZ).time()
    return (dtime(4, 30) <= now < dtime(8, 30)) or (dtime(12, 30) <= now < dtime(15, 0))


async def _probe_job() -> None:
    """每小时双端口探测并落库（登录页热力图数据源）。

    probe() 统一完成测量与出口调整（代理节点实测失败时全量重测换节点，
    app 是唯一决策大脑）；这里只维护状态机与告警：up（双端口可达）/ down。

    判红迟滞（2026-10-07 起）：分钟级风控抖动实测 3-16 分钟，旧版 20 秒
    复核必然落在抖动期内——当日 11:37/13:37/19:37 三次抖动各产生一对
    告警+恢复通知。改为 5 分钟内 3 次复核全红才判 down 告警；确认期间
    其他入口（手动探测/门控/守望）先见到绿，以新鲜事实为准中止本次判红。
    恢复维持单次绿即通知（好消息宜快）。
    """
    from app.core.client import probe
    from app.core.notify import notify

    if _probe_lock.locked():
        # 上一次判红复核进行中（最长约 5 分钟）：本次整点探测跳过，
        # 状态机只有一个写入者，避免两个任务交替写状态
        log.info("上一次探测复核进行中，本次跳过")
        return
    async with _probe_lock:
        p = await probe()
        if not p.ok and p.warmup:
            # 换节点缓冲期内的失败是暖机噪声，非可达性事实：不落库、不动状态机
            log.info("缓冲期内探测，跳过本次状态更新: %s", p.detail)
            return
        models.record_probe(p.ok, p.latency_ms, p.detail, p.channel)
        prev = models.get_setting("probe_last_state")

        if p.ok:
            models.set_setting("probe_last_state", "up")
            if prev == "down":
                await notify("✅到医院系统的连接已恢复", p.detail)
            return
        if prev == "down":
            return  # 已知不可达，不重复告警

        # 初测红、此前 up：进入确认序列（5 分钟内 3 次复核全红才告警）
        first_at = models.now_str()
        for wait in _CONFIRM_WAITS:
            await asyncio.sleep(wait)
            p = await probe()
            if p.warmup:
                # 暖机噪声不构成证据：中止本次判红，状态不动
                log.info("复核处于缓冲期，本次判红中止: %s", p.detail)
                return
            models.record_probe(p.ok, p.latency_ms, p.detail, p.channel)
            if p.ok:
                log.info("复核转绿，判定为分钟级抖动，不告警: %s", p.detail)
                models.set_setting("probe_last_state", "up")
                return
        # 全部复核红。但确认期间其他入口可能实测到过绿（手动探测/门控/守望），
        # 以实测事实为准，避免发出过期告警（确认窗口内出现任何绿即中止）
        if models.has_green_probe_since(first_at):
            log.info("判红确认期间有其他入口的绿探测，中止告警")
            models.set_setting("probe_last_state", "up")
            return
        models.set_setting("probe_last_state", "down")
        if _in_sign_window():
            # 签到窗口内故障由赛前守卫/门控的取消广播通知，这里只记状态
            log.info("窗口内探测不可达，跳过告警（取消广播会覆盖）: %s", p.detail)
            return
        await notify("🚨探测告警：到医院系统的连接持续不通",
                     f"{p.detail}\n"
                     "已持续约 5 分钟、多次复核均不通（全部代理出口均失败）。"
                     "故障期间的自动签到将自动取消，恢复后会另行通知。")


async def _resurrect_job() -> None:
    """取消场次的复活守望：签到窗口内每 3 分钟一次。

    赛前/连红取消的场次在窗口内探测转绿即自动重拉（2026-10-07 pm 场：
    13:02 取消、13:05 恢复，cron 每场只触发一次无人重拉，全靠手动补签）。
    具体判定（管理员取消不复活/冷却期/并发护栏）收敛在
    signer.maybe_relaunch_round。"""
    from datetime import datetime, time as dtime
    now = datetime.now(models.TZ).time()
    if dtime(4, 30) <= now < dtime(8, 30):
        period = "am"
    elif dtime(12, 30) <= now < dtime(15, 0):
        period = "pm"
    else:
        return
    await signer.maybe_relaunch_round(period)


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
    # 取消场次复活守望：窗口小时段内每 3 分钟检查（精确窗口与复活条件在
    # 任务内与 maybe_relaunch_round 判定），赛前/连红取消的场次转绿即重拉
    scheduler.add_job(_resurrect_job, CronTrigger(hour="4-8,12-14", minute="*/3"),
                      id="resurrect", name="取消场次复活守望")
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
