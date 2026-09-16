"""签到编排 + 重试 + 状态判定（设计稿第 4 章）。

- 每个账号、每个时段独立执行，账号间失败隔离
- 重试整体重跑主流程（会话分钟级过期，每次重新登录）
- 停止时间：上午 7:28 / 下午 14:23（窗口关闭前留缓冲）
- 通知去重：同一时段同一账号只推"首次（认证类）"与"最终"两次
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .. import models
from ..config import settings
from .client import AuthError, HospitalClient, LoginError, probe, probe_cached
from .notify import notify, notify_user_and_admin

log = logging.getLogger("lazy-clerk.signer")

TZ = ZoneInfo(settings.tz)

# 日志回调签名：(user_id, date, period, result, message)
from typing import Callable  # noqa: E402

LogFn = Callable[[int, str, str, str, str], None]

PERIOD_NAME = {"am": "上午", "pm": "下午"}
STOP_TIME = {"am": time(7, 28), "pm": time(14, 23)}
RETRY_INTERVAL = 300  # 秒
MAX_ATTEMPTS = 3      # 含首试。SSO 不可达是网络故障，短时间不会自愈，多试无益

RESULT_SUCCESS = "success"
RESULT_FAILED = "failed"
RESULT_SKIPPED = "skipped"       # 已签过
RESULT_NO_SCHEDULE = "no_schedule"  # 未排班/休假
RESULT_MANUAL = "manual"         # 需人工处理（迟到/补签/借假等）
RESULT_CHECKED = "checked"       # 管理页手动检测（非签到动作）

# 已签状态；-1 借假＝无需签到；其余非空状态（-3 迟到 / -4 补签待确认 ...）均需人工
SIGNED = {1, 10}

STATUS_TEXT = {
    None: "未签到", 0: "未签到", 1: "已签到·待确认", 10: "已确认",
    -1: "借假", -2: "早退", -3: "迟到", -4: "补签待确认",
    -5: "缺岗", -6: "擅自离岗", -10: "旷实习",
}


@dataclass
class SignOutcome:
    result: str
    message: str
    retryable: bool = False  # True 时进入重试循环
    notify: bool = False     # 静默终态（skipped/no_schedule）中需告知用户的场景


def find_target_row(rows: list[dict], date: str, period: str) -> dict | None:
    """定位目标行：WeekDate==当日 且 TimeName==当前时段。"""
    for row in rows:
        if str(row.get("WeekDate", ""))[:10] == date and row.get("TimeName") == PERIOD_NAME[period]:
            return row
    return None


def decide(row: dict) -> SignOutcome:
    """目标行状态判定（设计稿 4.1 步骤 3）。"""
    status = row.get("SignInStatus")
    day_off = int(row.get("DayOff") or 0)
    if status in SIGNED:
        return SignOutcome(RESULT_SKIPPED, f"已签到（状态 {status}），跳过")
    if day_off > 0:
        return SignOutcome(RESULT_MANUAL, f"补签场景（DayOff={day_off}），请人工处理")
    if status in (None, 0):
        return SignOutcome("sign", "待签到")  # 内部动作，不是终态
    if status == -1:
        # 借假＝已批准的假，与请假同待遇：终态不重试，告知本人（若非本人借假即异常信号）
        return SignOutcome(RESULT_NO_SCHEDULE, "今日已借假，无需签到", notify=True)
    return SignOutcome(RESULT_MANUAL, f"状态异常（SignInStatus={status}），请人工处理")


# 服务端拒绝中"本就不需要签到"的明确场景——终态，不重试、不告警。
# 按实测文案扩充，勿凭猜测添加。已观测：「当天已请假，不可签到」
NO_SIGN_NEEDED_INFO = ("请假", "休假")


def classify_rejection(info: str) -> SignOutcome:
    """SignStuCate 拒绝（Success != '1'）的分类。

    明确无需签到的（请假/休假）→ no_schedule 终态：不重试，推送告知用户本人
    （无排班的 no_schedule 是日常状态不推送，请假是偶发事件且需用户知情——
    若非本人请假即为异常信号）。
    """
    if any(k in info for k in NO_SIGN_NEEDED_INFO):
        return SignOutcome(RESULT_NO_SCHEDULE, f"服务端提示：{info}", notify=True)
    return SignOutcome(RESULT_FAILED, f"服务端拒绝: {info}", retryable=True)


async def sign_user_once(user: models.User, period: str) -> SignOutcome:
    """单账号单时段完整流程（登录 → 拉列表 → 判定 → 签到）。只跑一遍，不重试。"""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        async with HospitalClient(user.account, user.password) as client:
            await client.login()
            rows = await client.get_attendance(today)
            row = find_target_row(rows, today, period)
            if row is None:
                return SignOutcome(RESULT_NO_SCHEDULE, "今日无该时段排班，无需签到")
            outcome = decide(row)
            if outcome.result != "sign":
                return outcome
            resp = await client.sign(int(row["ID"]), status=1)
            if str(resp.get("Success")) == "1":
                return SignOutcome(RESULT_SUCCESS, "签到成功")
            return classify_rejection(str(resp.get("Info") or resp))
    except AuthError as e:
        # 密码错误/账号锁定：重试无意义，首次即推
        return SignOutcome(RESULT_FAILED, f"登录失败: {e}", retryable=False)
    except LoginError as e:
        return SignOutcome(RESULT_FAILED, str(e), retryable=True)
    except Exception as e:  # 医院改版等未预期异常：可重试，告警含摘要
        log.exception("签到流程未预期异常 user=%s", user.account)
        return SignOutcome(RESULT_FAILED, f"未预期异常: {type(e).__name__}: {e}", retryable=True)


def _period_label(period: str) -> str:
    return f"{datetime.now(TZ).strftime('%m-%d')} {PERIOD_NAME[period]}"


# 推送文案：免费版 Server 酱列表只露标题 → 状态前置进标题，正文从简
async def push_success(user: models.User, period: str) -> None:
    if user.sendkey:
        await notify(f"✅签到成功｜{user.nickname}｜{_period_label(period)}",
                     "自动签到成功。", sendkey=user.sendkey)


async def push_no_sign_needed(user: models.User, period: str, reason: str) -> None:
    """请假/休假等"无需签到"告知（仅用户本人，与成功推送同策略：无 SendKey 则静默）。"""
    if user.sendkey:
        await notify(f"🌴无需签到｜{user.nickname}｜{_period_label(period)}",
                     f"{reason}\n今日自动签到已跳过。若非本人请假，请留意考勤状态。",
                     sendkey=user.sendkey)


async def push_final_failure(user: models.User, period: str, reason: str,
                             attempts: int) -> None:
    # 附连通性诊断：区分「系统不可达（网络被封）」与「账号侧问题」，减少误判
    p = await probe_cached()
    if not p.ok:
        diag = (f"连通性探测：医院系统不可达（{p.detail}）——当前网络出口疑似被防火墙拦截，"
                "非账号问题，请换手机流量人工签到。")
    elif p.channel == "proxy":
        diag = "连通性探测：医院系统经代理可达（直连疑似被封）——疑似账号侧问题。"
    else:
        diag = "连通性探测：医院系统可正常访问——疑似账号侧问题。"
    await notify_user_and_admin(
        f"❌签到失败｜{user.nickname}｜{_period_label(period)}",
        f"{reason}\n已尝试 {attempts} 次，签到窗口即将关闭，请立即人工签到。\n{diag}",
        user.sendkey)


async def push_manual_needed(user: models.User, period: str, reason: str) -> None:
    await notify_user_and_admin(
        f"⚠️需人工处理｜{user.nickname}｜{_period_label(period)}",
        reason, user.sendkey)


async def push_login_failure(user: models.User, period: str, reason: str) -> None:
    await notify_user_and_admin(
        f"🔒登录失败｜{user.nickname}｜{_period_label(period)}",
        f"{reason}\n请检查密码或账号状态（可在登录页更正密码）。", user.sendkey)


RESULT_WORD = {
    RESULT_SUCCESS: "签到成功", RESULT_SKIPPED: "已签过", RESULT_NO_SCHEDULE: "无需签到",
    RESULT_FAILED: "失败", RESULT_MANUAL: "需人工",
}


async def push_manual_result(user: models.User, period: str, outcome: SignOutcome) -> None:
    """后台/个人页手动触发的签到（含已签过）也推送——兼作推送链路连通性探针。"""
    await notify(
        f"📢手动签到｜{user.nickname}｜{_period_label(period)}｜{RESULT_WORD.get(outcome.result, outcome.result)}",
        outcome.message,
        sendkey=user.sendkey)  # 无个人 SendKey 时回落管理员 key，保证必达


async def sign_user_with_retry(
    user: models.User,
    period: str,
    log_fn: "LogFn | None" = None,
) -> SignOutcome:
    """带重试的签到：成功/终态即停；可重试失败每 5 分钟重跑，超过停止时间推最终告警。

    log_fn(user_id, date, period, result, message)：日志落库回调，
    默认写 SQLite；个人版传入文件日志即可脱离数据库运行。
    """
    if log_fn is None:
        log_fn = lambda uid, date, p, result, msg: models.add_log(uid, date, p, result, msg)  # noqa: E731
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    attempts = 1

    while True:
        outcome = await sign_user_once(user, period)

        if outcome.result in (RESULT_SUCCESS, RESULT_SKIPPED, RESULT_NO_SCHEDULE):
            log_fn(user.id, today, period, outcome.result, outcome.message)
            if outcome.result == RESULT_SUCCESS:
                await push_success(user, period)
            elif outcome.notify:
                await push_no_sign_needed(user, period, outcome.message)
            return outcome

        if outcome.result == RESULT_MANUAL:
            log_fn(user.id, today, period, RESULT_MANUAL, outcome.message)
            await push_manual_needed(user, period, outcome.message)
            return outcome

        # failed
        if not outcome.retryable:
            # 认证类失败：首次即推，不重试
            log_fn(user.id, today, period, RESULT_FAILED, outcome.message)
            await push_login_failure(user, period, outcome.message)
            return outcome

        now = datetime.now(TZ).time()
        if attempts >= MAX_ATTEMPTS or now >= STOP_TIME[period]:
            log_fn(user.id, today, period, RESULT_FAILED,
                   f"重试耗尽（{attempts} 次）: {outcome.message}")
            await push_final_failure(user, period, outcome.message, attempts)
            return outcome

        log.info("签到失败将重试（第 %d/%d 次）user=%s period=%s: %s",
                 attempts + 1, MAX_ATTEMPTS, user.account, period, outcome.message)
        attempts += 1
        # 拟人化：重试间隔 5 分钟 ±1 分钟随机
        await asyncio.sleep(RETRY_INTERVAL + random.uniform(-60, 60))


async def sign_user_manual(user: models.User, log_prefix: str = "[手动]") -> SignOutcome:
    """Web 手动触发的单账号签到（公网版 /me 与管理页、宿舍版管理页共用）。

    单次尝试而非带重试——HTTP 请求不能挂起半小时；
    推送必发，兼作推送链路的连通性探针。个人版手动签到走 with_retry（交互菜单等得起）。
    """
    period = current_period()
    outcome = await sign_user_once(user, period)
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    models.add_log(user.id, today, period, outcome.result, f"{log_prefix} {outcome.message}")
    await push_manual_result(user, period, outcome)
    return outcome


async def _sign_one(user: models.User, period: str) -> None:
    """单个账号的签到任务：崩溃只记日志，不影响其他账号。"""
    try:
        outcome = await sign_user_with_retry(user, period)
        log.info("user=%s %s → %s %s", user.account, period, outcome.result, outcome.message)
    except Exception:
        log.exception("账号签到流程崩溃（已隔离）user=%s", user.account)


async def _preflight(period: str) -> bool:
    """赛前探针：双通道探测（测量 + 出口调整 + 落库），不可达则 20 秒后复核再放弃整轮。

    通知管理员（含诊断详情）+ 广播所有配了 SendKey 的启用用户（精简指引），
    避免系统故障日出现不知情缺勤。复核防止单次抖动误杀整轮。
    """
    first = await probe()
    models.record_probe(first.ok, first.latency_ms, first.detail, first.channel)
    if first.ok:
        return True
    log.warning("赛前探测失败（%s），20 秒后复核", first.detail)
    await asyncio.sleep(20)
    second = await probe()
    models.record_probe(second.ok, second.latency_ms, second.detail, second.channel)
    if second.ok:
        return True
    log.warning("赛前探测复核仍失败（%s），本轮签到放弃", second.detail)
    label = f"{datetime.now(TZ).strftime('%m-%d')} {PERIOD_NAME[period]}"
    await notify(f"🚨医院系统不可达｜{label}",
                 f"签到前探测连续失败（{second.detail}），本轮签到未执行。\n"
                 "当前网络出口疑似被医院防火墙拦截，请换手机流量人工签到。")
    for u in models.list_users():
        if u.enabled and u.sendkey:
            await notify(f"🚨自动签到取消｜{label}",
                         "本轮自动签到因网络问题取消，请在企微自行完成。",
                         sendkey=u.sendkey)
    return False


async def sign_all(period: str) -> None:
    """定时任务入口：赛前探测 → 每个账号一个独立任务，错峰启动、互不阻塞。

    不能顺序 await——某账号进入 5 分钟重试循环会阻塞后面所有账号
    （实测：一人请假被拒，后续账号全部错过签到窗口）。
    """
    log.info("开始 %s 时段签到", period)
    if not await _preflight(period):
        return
    users = [u for u in models.list_users() if u.enabled]
    tasks = []
    for i, user in enumerate(users):
        if i:
            # 拟人化：账号间随机间隔 10–40 秒，避免多账号同一秒齐发命中风控
            delay = random.uniform(10, 40)
            log.info("账号间随机间隔 %.0f 秒", delay)
            await asyncio.sleep(delay)
        tasks.append(asyncio.create_task(_sign_one(user, period)))
    for t in tasks:
        await t
    log.info("%s 时段签到结束", period)


def current_period() -> str:
    """手动签到用：按当前时间推断时段（12 点前算上午，之后算下午）。"""
    return "am" if datetime.now(TZ).hour < 12 else "pm"


async def check_user_status(user: models.User) -> None:
    """管理页"签到状态检测"：登录医院系统拉取当日真实状态，写入 checked 日志。

    不执行签到、不推送，只刷新状态总览。失败隔离由调用方保证。
    """
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        async with HospitalClient(user.account, user.password) as client:
            await client.login()
            rows = await client.get_attendance(today)
    except AuthError as e:
        models.add_log(user.id, today, current_period(), RESULT_FAILED,
                       f"[检测] 登录失败: {e}")
        return
    except Exception as e:
        models.add_log(user.id, today, current_period(), RESULT_FAILED,
                       f"[检测] {type(e).__name__}: {e}")
        return

    for period in ("am", "pm"):
        row = find_target_row(rows, today, period)
        if row is None:
            msg = "无排班"
        else:
            status = row.get("SignInStatus")
            msg = STATUS_TEXT.get(status, f"未知状态({status})")
            if status in SIGNED and row.get("SignInTime"):
                msg += f" {str(row['SignInTime'])[10:16]}"
            if int(row.get("DayOff") or 0) > 0:
                msg += "（补签场景）"
        models.add_log(user.id, today, period, RESULT_CHECKED, f"[检测] {msg}")
