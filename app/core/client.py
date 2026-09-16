"""HospitalClient：统一登录平台（SSO）登录链路 + 实习考勤系统接口。

登录链路（设计稿 2.2，三步）：
  ① GET  1118/                  → 匿名会话 cookie
  ② POST 1118/Home/SubmitVerify → RSA 密码 + 任意 4 位验证码 → SSO Token（GUID）
  ③ POST 1198/ (Token=<guid>)   → SSTokenCookie 考勤会话

会话分钟级过期，因此每次签到都新建 client 完整跑一遍链路，不做保活。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import httpx

from .crypto import encrypt_password
from ..config import settings

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


# ---------- 连通性探针 ----------

@dataclass
class ProbeResult:
    ok: bool
    detail: str
    latency_ms: int
    channel: str = "direct"  # direct=直连出口 / proxy=代理出口（热力图分色依据）


async def current_channel() -> str:
    """查 mihomo 当前为医院流量选的出口：DIRECT→direct，其余→proxy。

    未配置代理或 API 不可达时按部署形态推断：有代理配置按 proxy 记
    （API 挂了多半代理也挂了，宁可记成代理），无代理配置即直连。
    """
    if not settings.mihomo_api:
        return "proxy" if settings.proxy_url else "direct"
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{settings.mihomo_api}/proxies/{settings.proxy_group}",
                            headers={"Authorization": f"Bearer {settings.mihomo_secret}"})
            now = r.json().get("now", "")
            return "direct" if now == "DIRECT" else "proxy"
    except Exception:
        return "proxy" if settings.proxy_url else "direct"


_GROUP_TYPES = {"URLTest", "Fallback", "Selector", "LoadBalance"}


async def mihomo_group() -> dict | None:
    """代理组状态（管理页卡片）：{now, nodes: [{name, delay}]}。

    延迟取健康检查的历史结果（被动读取，不产生新流量）。
    子组（hospital-auto）自动展开一层，列出真实节点。
    未配置或 API 不可达返回 None。
    """
    if not settings.mihomo_api:
        return None
    auth = {"Authorization": f"Bearer {settings.mihomo_secret}"}

    async def fetch(c: httpx.AsyncClient, name: str) -> dict:
        r = await c.get(f"{settings.mihomo_api}/proxies/{name}", headers=auth)
        return r.json()

    def node_entry(p: dict) -> dict:
        hist = p.get("history") or []
        return {"name": p.get("name", ""), "delay": hist[-1].get("delay") if hist else None}

    try:
        async with httpx.AsyncClient(timeout=5) as c:
            g = await fetch(c, settings.proxy_group)
            nodes = []
            for name in g.get("all", []):
                if name in ("DIRECT", "REJECT"):
                    continue
                p = await fetch(c, name)
                if p.get("type") in _GROUP_TYPES:
                    for sub in p.get("all", []):
                        if sub not in ("DIRECT", "REJECT"):
                            nodes.append(node_entry(await fetch(c, sub)))
                else:
                    nodes.append(node_entry(p))
            return {"now": g.get("now", ""), "nodes": nodes}
    except Exception:
        return None


async def mihomo_switch(target: str) -> str | None:
    """切换出口。DIRECT/hospital-auto 作用于主组，具体节点作用于测速子组。

    返回错误信息，成功为 None。注意：url-test 子组在下次测速（5 分钟）后
    可能按延迟自动重选——手动指定适合"当前节点不通先换一个顶着"。
    """
    if not settings.mihomo_api:
        return "未配置代理（MIHOMO_API 为空）"
    group = (settings.proxy_group if target in ("DIRECT", f"{settings.proxy_group}-auto")
             else f"{settings.proxy_group}-auto")
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.put(f"{settings.mihomo_api}/proxies/{group}",
                            headers={"Authorization": f"Bearer {settings.mihomo_secret}"},
                            json={"name": target})
        if r.status_code == 204:
            return None
        return f"mihomo 返回 HTTP {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return f"API 不可达: {type(e).__name__}"


async def probe(timeout: float = 8.0) -> ProbeResult:
    """连通性探针：匿名 GET SSO 首页，出口与签到链路完全一致。

    不登录、不带任何凭据，与浏览器打开登录页完全同构——封禁的触发面在
    认证接口的频次/失败率，分钟级以下的匿名 GET 不会增加封禁概率。
    """
    start = time.monotonic()
    latency = lambda: int((time.monotonic() - start) * 1000)  # noqa: E731
    channel, ok, detail = "direct", False, ""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"User-Agent": UA},
                                     proxy=settings.proxy_url or None) as c:
            r = await c.get(SSO_BASE + "/")
        ok = r.status_code < 500
        if ok:
            detail = f"HTTP {r.status_code}"
        elif settings.proxy_url:
            # 经代理的 5xx 多为节点侧失败（Clash 上游错误），不是医院服务端故障
            detail = f"HTTP {r.status_code}（代理节点或上游异常，换个节点试试）"
        else:
            detail = f"HTTP {r.status_code} 服务端错误"
    except httpx.ConnectTimeout:
        detail = "连接超时（疑似被防火墙拦截）"
    except httpx.ConnectError as e:
        detail = f"连接失败: {type(e).__name__}"
    except httpx.HTTPError as e:
        detail = f"网络错误: {type(e).__name__}"
    if ok:
        channel = await current_channel()
    return ProbeResult(ok, detail, latency(), channel)


_probe_cache: tuple[float, ProbeResult] | None = None


async def probe_cached(ttl: float = 60.0) -> ProbeResult:
    """带 60 秒缓存的探测：并行场景（多人同时失败告警）共享一次结果。"""
    global _probe_cache
    if _probe_cache and time.monotonic() - _probe_cache[0] < ttl:
        return _probe_cache[1]
    result = await probe()
    _probe_cache = (time.monotonic(), result)
    return result


class HospitalClient:
    def __init__(self, account: str, password: str, timeout: float = 15.0):
        self.account = account
        self.password = password
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": UA},
            proxy=settings.proxy_url or None,  # 配置了代理逃生通道则全程走同一出口
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
