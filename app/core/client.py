"""HospitalClient：统一登录平台（SSO）登录链路 + 实习考勤系统接口。

登录链路（设计稿 2.2，三步）：
  ① GET  1118/                  → 匿名会话 cookie
  ② POST 1118/Home/SubmitVerify → RSA 密码 + 任意 4 位验证码 → SSO Token（GUID）
  ③ POST 1198/ (Token=<guid>)   → SSTokenCookie 考勤会话

会话分钟级过期，因此每次签到都新建 client 完整跑一遍链路，不做保活。
"""
from __future__ import annotations

import re

import httpx

from .crypto import encrypt_password

SSO_BASE = "http://my.gzsums.net:1118"
ATT_BASE = "http://my.gzsums.net:1198"
CLIENT_URL = "Http://my.gzsums.net:1198"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class AuthError(Exception):
    """服务器正常响应但认证失败（密码错误/账号锁定等）——重试无意义。"""


class LoginError(Exception):
    """登录链路异常（网络错误、页面结构变化等）——可重试。"""


class HospitalClient:
    def __init__(self, account: str, password: str, timeout: float = 15.0):
        self.account = account
        self.password = password
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": UA},
        )

    async def __aenter__(self) -> "HospitalClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    # ---------- 登录 ----------

    @staticmethod
    def _extract_token(resp: httpx.Response) -> str | None:
        """从响应体与重定向链的 Location 头中提取 SSO Token（GUID）。"""
        candidates = [resp.text]
        for r in resp.history:
            if "location" in r.headers:
                candidates.append(r.headers["location"])
        if "location" in resp.headers:
            candidates.append(resp.headers["location"])
        for text in candidates:
            m = _GUID_RE.search(text or "")
            if m:
                return m.group(0)
        return None

    @staticmethod
    def _extract_error_msg(resp: httpx.Response) -> str:
        """SSO 认证失败时会在登录页里注入 var msg = '...'，提取为可读原因。"""
        m = re.search(r"var\s+msg\s*=\s*'([^']*)'", resp.text)
        if m and m.group(1).strip():
            return m.group(1).strip()
        excerpt = re.sub(r"<[^>]+>", " ", resp.text)
        return re.sub(r"\s+", " ", excerpt).strip()[:200] or "（空响应）"

    async def login(self) -> None:
        try:
            r1 = await self._client.get(SSO_BASE + "/")
            r1.raise_for_status()

            r2 = await self._client.post(
                SSO_BASE + "/Home/SubmitVerify",
                data={
                    "Account": self.account,
                    "Password": encrypt_password(self.password),
                    "authCode": "1234",   # 验证码纯前端校验，任意 4 位即可（设计稿 2.4）
                    "imgCode": "",
                    "ClientUrl": CLIENT_URL,
                },
            )
            r2.raise_for_status()
        except httpx.HTTPError as e:
            raise LoginError(f"SSO 网络错误: {e}") from e

        token = self._extract_token(r2)
        if not token:
            raise AuthError(f"认证失败: {self._extract_error_msg(r2)}")

        try:
            r3 = await self._client.post(ATT_BASE + "/", data={"Token": token})
            r3.raise_for_status()
        except httpx.HTTPError as e:
            raise LoginError(f"考勤系统会话交换网络错误: {e}") from e

        if "SSTokenCookie" not in self._client.cookies:
            raise LoginError("考勤系统未签发 SSTokenCookie（Token 交换失败）")

    # ---------- 考勤接口 ----------

    async def get_attendance(self, date: str) -> list[dict]:
        """拉取考勤列表。date 格式 YYYY-MM-DD，startTime=endTime=当日。"""
        try:
            r = await self._client.get(
                ATT_BASE + "/ExOrg/GetStuAndTeacher",
                params={"startTime": date, "endTime": date, "stuId": self.account},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LoginError(f"拉取考勤列表失败: {e}") from e
        data = r.json()
        if not isinstance(data, list):
            raise LoginError(f"考勤列表返回格式异常: {str(data)[:200]}")
        return data

    async def sign(self, record_id: int, status: int = 1) -> dict:
        """执行签到。返回服务端 JSON；Success=='1' 为成功，否则 Info 含原因。"""
        try:
            r = await self._client.post(
                ATT_BASE + "/ExOrg/SignStuCate",
                json={"id": record_id, "status": status},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LoginError(f"签到请求失败: {e}") from e
        return r.json()


async def verify_account(account: str, password: str) -> str | None:
    """对医院 SSO 做一次真实认证（注册/配置向导用）。通过返回 None，否则返回可读错误。"""
    try:
        async with HospitalClient(account, password) as client:
            await client.login()
        return None
    except AuthError as e:
        return str(e)
    except LoginError as e:
        return f"医院平台连接异常: {e}"
    except Exception as e:
        return f"验证异常: {type(e).__name__}: {e}"
