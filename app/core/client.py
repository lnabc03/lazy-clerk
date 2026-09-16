"""HospitalClient：统一登录平台（SSO）登录链路 + 实习考勤系统接口。

登录链路（设计稿 2.2，三步）：
  ① GET  1118/                  → 匿名会话 cookie
  ② POST 1118/Home/SubmitVerify → RSA 密码 + 任意 4 位验证码 → SSO Token（GUID）
  ③ POST 1198/ (Token=<guid>)   → SSTokenCookie 考勤会话

会话分钟级过期，因此每次签到都新建 client 完整跑一遍链路，不做保活。
"""
from __future__ import annotations

import asyncio
import random
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


# ---------- 双通道连通性探针 ----------

@dataclass
class ProbeResult:
    ok: bool
    detail: str            # 双通道人读摘要，如 "直连超时（疑似被防火墙拦截）；代理 561ms（🇭🇰|…）"
    latency_ms: int
    channel: str = "direct"  # 生效出口：direct=直连 / proxy=代理（热力图分色依据）；双不通时为当前选择


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


async def _mihomo_get(c: httpx.AsyncClient, path: str, **kw) -> httpx.Response:
    return await c.get(f"{settings.mihomo_api}{path}",
                       headers={"Authorization": f"Bearer {settings.mihomo_secret}"}, **kw)


async def _probe_direct(timeout: float) -> tuple[bool, int, str]:
    """直连通道：本机（不经代理）匿名 GET SSO 首页。返回 (ok, 耗时ms, 状态描述)。

    不登录、不带任何凭据，与浏览器打开登录页同构——封禁触发面在认证接口的
    频次/失败率，分钟级以下的匿名 GET 不增封禁概率。trust_env=False 保证
    不受环境变量里的 HTTP_PROXY 影响，测的是真直连。
    """
    start = time.monotonic()
    ms = lambda: int((time.monotonic() - start) * 1000)  # noqa: E731
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     trust_env=False, headers={"User-Agent": UA}) as c:
            r = await c.get(SSO_BASE + "/")
        if r.status_code < 500:
            return True, ms(), "正常"
        return False, ms(), f"HTTP {r.status_code} 服务端错误"
    except httpx.ConnectTimeout:
        return False, ms(), "超时（疑似被防火墙拦截）"
    except httpx.HTTPError as e:
        return False, ms(), f"网络错误: {type(e).__name__}"


async def _probe_proxy(timeout: float) -> tuple[int | None, str]:
    """代理通道：mihomo delay API 经 hospital-auto 当前节点实测医院首页。

    返回 (延迟ms, 节点名)；当前节点不可用或 API 不可达返回 (None, 节点名或"")。
    只读不改：测的是 url-test 组当前选中的节点，不触发任何切换。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout + 5) as c:
            g = await _mihomo_get(c, f"/proxies/{settings.proxy_group}-auto")
            node = g.json().get("now", "")
            r = await _mihomo_get(c, f"/proxies/{settings.proxy_group}-auto/delay",
                                  params={"url": SSO_BASE + "/", "timeout": int(timeout * 1000)})
            if r.status_code == 200:
                return int(r.json()["delay"]), node
            return None, node
    except Exception:
        return None, ""


async def _proxy_group_retest(timeout: float) -> tuple[int | None, str]:
    """全量重测 hospital-auto 所有节点，返回 (最佳延迟ms, 对应节点名)。

    副作用即用途：url-test 按最新结果立即重选节点——当前节点死亡时的
    即时自愈，不等 5 分钟测速周期。失败节点不在响应里；无可用节点返回 (None, "")。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout + 10) as c:
            r = await _mihomo_get(c, f"/group/{settings.proxy_group}-auto/delay",
                                  params={"url": SSO_BASE + "/", "timeout": int(timeout * 1000)})
            delays = {k: v for k, v in r.json().items() if isinstance(v, int) and v > 0}
            if not delays:
                return None, ""
            node = min(delays, key=lambda k: delays[k])
            return delays[node], node
    except Exception:
        return None, ""


async def probe(timeout: float = 8.0) -> ProbeResult:
    """双通道统一探针：直连与代理各测一次，按结果调整出口。手动检测、每小时
    定时探测、赛前守卫共用这一个行为——app 是出口决策的唯一大脑
    （mihomo hospital 组是 selector，只执行不自主切换）。

    决策矩阵：
      直连通           → 出口 DIRECT（含直连恢复后的自动切回）
      直连不通、代理通 → 出口 hospital-auto
      双不通           → 维持现状（防误切），判不可达；判死前会先对代理池
                        全量重测一次，顺带完成节点自愈
    """
    d_ok, d_ms, d_detail = await _probe_direct(timeout)
    if not (settings.proxy_url and settings.mihomo_api):
        return ProbeResult(d_ok, f"直连 {d_ms}ms" if d_ok else f"直连{d_detail}",
                           d_ms, "direct")

    p_ms, p_node = await _probe_proxy(timeout)
    if p_ms is None and not d_ok:
        # 直连已死且当前节点也不通：全量重测，换节点再定论
        p_ms, p_node = await _proxy_group_retest(timeout)

    proxy_part = (f"代理 {p_ms}ms" if p_ms is not None else "代理不可用")
    if p_node:
        proxy_part += f"（{p_node}）"
    detail = f"直连 {d_ms}ms；{proxy_part}" if d_ok else f"直连{d_detail}；{proxy_part}"

    if d_ok:
        target, channel, ok, ms = "DIRECT", "direct", True, d_ms
    elif p_ms is not None:
        target, channel, ok, ms = f"{settings.proxy_group}-auto", "proxy", True, p_ms
    else:
        return ProbeResult(False, detail, d_ms, await current_channel())
    want = "direct" if target == "DIRECT" else "proxy"
    if await current_channel() != want:
        await mihomo_switch(target)
    return ProbeResult(ok, detail, ms, channel)


_last_retest = 0.0


async def _retest_nodes_throttled() -> None:
    """真实流量连续失败时的节点自愈：全量重测迫使 url-test 立即换掉坏节点。

    探针的单次采样可能在节点坏相中碰巧通过，真实流量的连续失败才是
    更可信的「该换节点了」信号。全局节流 120 秒——多账号并发失败时只
    触发一次；测速开销对医院无感（节点池自带的 5 分钟健康检查同量级）。
    """
    global _last_retest
    if not settings.mihomo_api:
        return
    now = time.monotonic()
    if now - _last_retest < 120:
        return
    _last_retest = now
    await _proxy_group_retest(8.0)


# 订阅里混入的套餐信息类假节点（"剩余流量：49.06 GB"、"套餐到期：长期有效" 等）
_JUNK_NODE_RE = re.compile(r"剩余流量|套餐|到期|官网|客服|距离|重置|倍率")


async def mihomo_group() -> dict | None:
    """代理状态（管理页卡片）：{now, node, nodes: [{name, delay}]}。

    now 为 hospital 组当前选择（DIRECT / hospital-auto）；node 为自动选速
    当前节点。节点列表读 /providers/proxies——订阅节点不注册在 /proxies
    命名空间（直接查会 404 刷屏），只有 provider 接口能看到它们。
    延迟取健康检查的历史结果（被动读取，不产生新流量）；剔除假节点与
    近期测速全挂的节点，按最近延迟升序，取前 20 个。
    未配置或 API 不可达返回 None。
    """
    if not settings.mihomo_api:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            g = (await _mihomo_get(c, f"/proxies/{settings.proxy_group}")).json()
            auto = (await _mihomo_get(c, f"/proxies/{settings.proxy_group}-auto")).json()
            providers = (await _mihomo_get(c, "/providers/proxies")).json()
    except Exception:
        return None
    nodes = []
    for provider in providers.get("providers", {}).values():
        for p in provider.get("proxies", []):
            name = p.get("name", "")
            hist = p.get("history") or []
            delay = hist[-1].get("delay") if hist else None
            if not name or _JUNK_NODE_RE.search(name) or not delay:
                continue
            nodes.append({"name": name, "delay": delay})
    nodes.sort(key=lambda n: n["delay"])
    node = auto.get("now", "")
    if node and all(n["name"] != node for n in nodes):
        nodes.insert(0, {"name": node, "delay": None})  # 当前节点测速挂过也保留可见
    return {"now": g.get("now", ""), "node": node, "nodes": nodes[:20]}


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

    # 代理节点存在逐连接随机失败（中转入口后端轮询，抽到死节点 mihomo 即回
    # 502），单次失败率可达五成但整体可用。对幂等请求做有限重试；连续失败
    # 两次视为节点进入坏相，触发节点池全量重测换节点（节流 120s）后再试。
    # 签到动作（SignStuCate）不在此重试——外层 5 分钟重试循环会重新登录并
    # 先读状态，天然幂等。
    _RETRYABLE_EXC = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
                      httpx.ReadTimeout, httpx.WriteError, httpx.WriteTimeout,
                      httpx.PoolTimeout, httpx.RemoteProtocolError, httpx.ProxyError)

    async def _request(self, method: str, url: str, retries: int = 2,
                       **kw) -> httpx.Response:
        """5xx 与传输层错误有限重试（默认共 3 次，退避 ~1s/~2s）；4xx 与成功直接返回。"""
        last_resp: httpx.Response | None = None
        last_exc: httpx.HTTPError | None = None
        for attempt in range(retries + 1):
            try:
                r = await self._client.request(method, url, **kw)
                if r.status_code < 500:
                    return r
                last_resp, last_exc = r, None
            except self._RETRYABLE_EXC as e:
                last_resp, last_exc = None, e
            if attempt < retries:
                if attempt >= 1:
                    # 连续失败两次：多半是节点进入坏相而非单次抽签，先换节点再试
                    await _retest_nodes_throttled()
                await asyncio.sleep(0.8 * (2 ** attempt) + random.uniform(0, 0.6))
        if last_resp is not None:
            last_resp.raise_for_status()
        assert last_exc is not None  # retries+1 次全走异常分支才会到这
        raise last_exc

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
            r1 = await self._request("GET", SSO_BASE + "/")
            r1.raise_for_status()

            r2 = await self._request(
                "POST", SSO_BASE + "/Home/SubmitVerify",
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
            r3 = await self._request("POST", ATT_BASE + "/", data={"Token": token})
            r3.raise_for_status()
        except httpx.HTTPError as e:
            raise LoginError(f"考勤系统会话交换网络错误: {e}") from e

        if "SSTokenCookie" not in self._client.cookies:
            raise LoginError("考勤系统未签发 SSTokenCookie（Token 交换失败）")

    # ---------- 考勤接口 ----------

    async def get_attendance(self, date: str) -> list[dict]:
        """拉取考勤列表。date 格式 YYYY-MM-DD，startTime=endTime=当日。"""
        try:
            r = await self._request(
                "GET", ATT_BASE + "/ExOrg/GetStuAndTeacher",
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
