"""冒烟测试脚本（设计稿第 10 章）。独立于主项目运行，用真实账号按阶段验证。

用法：
  python scripts/smoke.py --account <工号> --password 'xxx'              # S0–S3（窗口外可跑）
  python scripts/smoke.py --account X --password Y --stage S4              # 真实签到（仅窗口内！）
  python scripts/smoke.py --stage S5 --sendkey 'SCT...'                    # 通知链路
  python scripts/smoke.py --stage S6 --url http://127.0.0.1:8787 --invite CODE

S1–S3 在窗口外可随时跑，是医院改版时的日常回归手段。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core import signer  # noqa: E402
from app.core.client import ATT_BASE, SSO_BASE, HospitalClient  # noqa: E402
from app.core.crypto import encrypt_password  # noqa: E402
from app.core.notify import notify  # noqa: E402

OK, FAIL = "\033[32m[PASS]\033[0m", "\033[31m[FAIL]\033[0m"


def report(stage: str, passed: bool, detail: str = "") -> bool:
    print(f"{OK if passed else FAIL} {stage}: {detail}")
    return passed


async def s0_crypto() -> bool:
    """S0：RSA 加密单测——密文解码后 128 字节（RSA-1024），每次结果不同。"""
    c1, c2 = encrypt_password("test-password"), encrypt_password("test-password")
    raw = base64.b64decode(c1)
    return report("S0 RSA 加密", len(raw) == 128 and c1 != c2,
                  f"密文 {len(raw)} 字节，两次加密结果{'不同' if c1 != c2 else '相同（异常）'}")


async def s1_login(account: str, password: str) -> bool:
    """S1：登录链路（窗口外可测）——三步走通，拿到 SSTokenCookie。"""
    try:
        async with HospitalClient(account, password) as client:
            await client.login()
            cookies = dict(client._client.cookies)
        return report("S1 登录链路", "SSTokenCookie" in cookies,
                      f"SSO={SSO_BASE} 考勤={ATT_BASE}，cookies: {list(cookies)}")
    except Exception as e:
        return report("S1 登录链路", False, f"{type(e).__name__}: {e}")


async def s2_attendance(account: str, password: str) -> bool:
    """S2：拉取考勤列表——返回包含本人当日记录，字段完整。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    try:
        async with HospitalClient(account, password) as client:
            await client.login()
            rows = await client.get_attendance(today)
        if not rows:
            return report("S2 考勤列表", False, f"{today} 返回空列表")
        required = {"ID", "WeekDate", "TimeName", "SignInStatus", "DayOff"}
        missing = required - set(rows[0])
        detail = f"{today} 共 {len(rows)} 行，字段: {sorted(rows[0])}"
        return report("S2 考勤列表", not missing, detail + (f"，缺字段 {missing}" if missing else ""))
    except Exception as e:
        return report("S2 考勤列表", False, f"{type(e).__name__}: {e}")


async def s3_decide(account: str, password: str) -> bool:
    """S3：目标行定位逻辑——输出判定结果，不实际提交。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    today = now.strftime("%Y-%m-%d")
    try:
        async with HospitalClient(account, password) as client:
            await client.login()
            rows = await client.get_attendance(today)
        passed = True
        for period in ("am", "pm"):
            row = signer.find_target_row(rows, today, period)
            if row is None:
                print(f"    {signer.PERIOD_NAME[period]}: 无排班记录（no_schedule）")
                continue
            outcome = signer.decide(row)
            print(f"    {signer.PERIOD_NAME[period]}: ID={row.get('ID')} "
                  f"SignInStatus={row.get('SignInStatus')} DayOff={row.get('DayOff')} "
                  f"→ {outcome.result}（{outcome.message}）")
        return report("S3 目标行定位", passed, "判定逻辑执行完毕（未提交签到）")
    except Exception as e:
        return report("S3 目标行定位", False, f"{type(e).__name__}: {e}")


async def s4_sign(account: str, password: str) -> bool:
    """S4：真实签到——仅可在窗口内验证！窗口外会收到服务端失败返回。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    in_window = (5, 0) <= (now.hour, now.minute) <= (8, 0) or (13, 0) <= (now.hour, now.minute) <= (14, 30)
    if not in_window:
        print(f"\033[33m[WARN]\033[0m 当前 {now.strftime('%H:%M')} 不在签到窗口"
              "（5:00–8:00 / 13:00–14:30），服务端大概率拒绝")
    period = "am" if now.hour < 12 else "pm"
    today = now.strftime("%Y-%m-%d")
    try:
        async with HospitalClient(account, password) as client:
            await client.login()
            rows = await client.get_attendance(today)
            row = signer.find_target_row(rows, today, period)
            if row is None:
                return report("S4 真实签到", False, f"{signer.PERIOD_NAME[period]}无排班记录")
            outcome = signer.decide(row)
            if outcome.result != "sign":
                return report("S4 真实签到", outcome.result == "skipped",
                              f"无需签到: {outcome.message}")
            resp = await client.sign(int(row["ID"]), status=1)
            ok = str(resp.get("Success")) == "1"
            return report("S4 真实签到", ok, f"服务端响应: {resp}")
    except Exception as e:
        return report("S4 真实签到", False, f"{type(e).__name__}: {e}")


async def s5_notify(sendkey: str) -> bool:
    """S5：通知链路——Server 酱测试消息送达。"""
    ok = await notify("lazy-clerk 冒烟测试 S5", "Server 酱通知链路验证。", sendkey=sendkey)
    return report("S5 通知链路", ok, "推送已发出，请检查微信" if ok else "推送失败")


async def s6_register(url: str, invite: str, account: str, password: str) -> bool:
    """S6：注册闭环——真实医院账号 + 邀请码注册 → 自动登录 → 邀请码失效。

    v0.3 起注册需通过医院 SSO 真实认证，因此必须用真实账号；
    测试后请在管理页删除该注册账号（会同时清除其日志）。
    """
    import httpx

    async with httpx.AsyncClient(base_url=url, follow_redirects=True, timeout=20) as c:
        r = await c.post("/register", data={
            "nickname": "冒烟测试", "account": account, "password": password,
            "invite_code": invite, "sendkey": "",
        })
        registered = "/me" in str(r.url)
        # 邀请码应已失效：用同一码再注册应失败（未走到医院验证即被拒则更好）
        r2 = await c.post("/register", data={
            "nickname": "冒烟测试2", "account": account, "password": password,
            "invite_code": invite, "sendkey": "",
        })
        reused_blocked = "error" in str(r2.url)
    return report("S6 注册闭环", registered and reused_blocked,
                  f"注册并自动登录={'是' if registered else '否'}，"
                  f"邀请码复用被拒={'是' if reused_blocked else '否'}。"
                  f"注意：请在管理页删除该注册账号")


async def main() -> None:
    p = argparse.ArgumentParser(description="lazy-clerk 冒烟测试")
    p.add_argument("--account", help="工号")
    p.add_argument("--password", help="密码")
    p.add_argument("--sendkey", help="Server 酱 SendKey（S5）")
    p.add_argument("--url", default="http://127.0.0.1:8787", help="服务地址（S6）")
    p.add_argument("--invite", help="邀请码（S6，会被消耗）")
    p.add_argument("--stage", help="只跑某个阶段（如 S1）；默认 S0–S3")
    args = p.parse_args()

    stages = [args.stage.upper()] if args.stage else ["S0", "S1", "S2", "S3"]
    results = []
    for s in stages:
        if s == "S0":
            results.append(await s0_crypto())
        elif s in ("S1", "S2", "S3", "S4"):
            if not (args.account and args.password):
                sys.exit(f"{s} 需要 --account 和 --password")
            results.append(await {"S1": s1_login, "S2": s2_attendance,
                                  "S3": s3_decide, "S4": s4_sign}[s](args.account, args.password))
        elif s == "S5":
            if not args.sendkey:
                sys.exit("S5 需要 --sendkey")
            results.append(await s5_notify(args.sendkey))
        elif s == "S6":
            if not (args.invite and args.account and args.password):
                sys.exit("S6 需要 --invite（会消耗一个邀请码）及真实 --account/--password"
                         "（v0.3 起注册需医院 SSO 认证）")
            results.append(await s6_register(args.url, args.invite,
                                             args.account, args.password))
        else:
            sys.exit(f"未知阶段: {s}")

    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
