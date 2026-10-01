"""可及性热力图聚合逻辑单测：ok/fail 两态与小时内最新生效。"""
import sqlite3
from datetime import datetime

from app import db, models
from app.config import settings


def _use_tmp_db(tmp_path):
    """把全局连接指向临时库。测试结束时调用方恢复。"""
    object.__setattr__(settings, "data_dir", str(tmp_path))  # frozen dataclass 绕过
    db._conn = None
    db.init()


def _restore_db():
    db._conn = None
    object.__setattr__(settings, "data_dir", "data")


def _insert(hour: int, ok: bool, channel: str, date: str, minute: int = 7):
    db.conn().execute(
        "INSERT INTO probe_logs (ok, latency_ms, detail, channel, created_at)"
        " VALUES (?,?,?,?,?)",
        (int(ok), 10, "", channel, f"{date} {hour:02d}:{minute:02d}:00"),
    )
    db.conn().commit()


def test_heatmap_two_states(tmp_path):
    _use_tmp_db(tmp_path)
    try:
        today = datetime.now(models.TZ).strftime("%Y-%m-%d")
        _insert(6, True, "proxy", today)
        _insert(7, True, "direct", today)   # 存量 direct 记录同样归并到 ok
        _insert(8, False, "proxy", today)
        # 9 点混合：先失败（自动探测）后成功（手动检测）——最新一条生效，标绿
        _insert(9, False, "proxy", today, minute=7)
        _insert(9, True, "proxy", today, minute=40)
        # 10 点混合：先成功后失败——最新一条是失败，标红
        _insert(10, True, "proxy", today, minute=7)
        _insert(10, False, "proxy", today, minute=50)

        probe_row_today = None
        for row in models.probe_heatmap(days=7):
            if row["date"] == today:
                probe_row_today = row
        assert probe_row_today is not None
        cells = probe_row_today["cells"]
        assert cells[6] == "ok"
        assert cells[7] == "ok"
        assert cells[8] == "fail"
        assert cells[9] == "ok"    # 手动检测成功覆盖整点失败
        assert cells[10] == "fail"  # 最新一条是失败
        assert cells[11] is None    # 无数据
        assert len(cells) == 24
    finally:
        _restore_db()


def test_record_probe_roundtrip(tmp_path):
    _use_tmp_db(tmp_path)
    try:
        models.record_probe(True, 123, "HTTP 200", "proxy")
        row = models.latest_probe()
        assert row["ok"] == 1 and row["channel"] == "proxy" and row["latency_ms"] == 123
    finally:
        _restore_db()


def test_old_db_migrates_channel_column(tmp_path):
    """v0.1.3 及以前的存量 probe_logs 表（无 channel 列）升级后可用。"""
    tmp_path.mkdir(exist_ok=True)
    raw = sqlite3.connect(tmp_path / "lazy-clerk.db")
    raw.execute("CREATE TABLE probe_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ok INTEGER NOT NULL, latency_ms INTEGER, detail TEXT, created_at TEXT NOT NULL)")
    raw.execute("INSERT INTO probe_logs (ok, latency_ms, detail, created_at)"
                " VALUES (1, 10, 'HTTP 200', '2026-09-15 08:07:00')")
    raw.commit()
    raw.close()

    _use_tmp_db(tmp_path)
    try:
        row = models.latest_probe()
        assert row["channel"] == "direct"  # 迁移补默认值
        models.record_probe(True, 20, "HTTP 200", "proxy")  # 新写入正常
    finally:
        _restore_db()


# ---------- probe() 探针逻辑：代理单通道双端口；判红前全量重测换节点 ----------

import asyncio  # noqa: E402

from app.core import client  # noqa: E402


def _proxy_on():
    """假装配好了 mihomo 代理。"""
    object.__setattr__(settings, "proxy_url", "http://127.0.0.1:7890")
    object.__setattr__(settings, "mihomo_api", "http://127.0.0.1:9090")


def _proxy_off():
    object.__setattr__(settings, "proxy_url", "")
    object.__setattr__(settings, "mihomo_api", "")


async def _fake(value):
    return value


def _patch_channels(monkeypatch, proxy, retest=(None, ""), current="proxy"):
    """mock 三条外部依赖：钉住节点双端口结果、全量重测结果、当前出口。

    proxy: (综合延迟ms|None, 节点名, 明细)；retest: (延迟ms|None, 节点名)。"""
    monkeypatch.setattr(client, "_last_switch", 0.0)  # 默认不在缓冲期
    monkeypatch.setattr(client, "_probe_proxy", lambda t=8.0: _fake(proxy))
    monkeypatch.setattr(client, "_proxy_group_retest", lambda t=8.0: _fake(retest))
    monkeypatch.setattr(client, "current_channel", lambda: _fake(current))


def _patch_switch(monkeypatch, calls: list):
    async def _switch(target):
        calls.append(target)
        return None
    monkeypatch.setattr(client, "mihomo_switch", _switch)


def test_probe_proxy_ok_no_switch(monkeypatch):
    """钉住节点双端口可达且出口已在代理：标 ok，不触发任何切换。"""
    _proxy_on()
    try:
        _patch_channels(monkeypatch,
                        proxy=(561, "节点A", "认证 561ms；考勤 600ms（节点A）"))
        calls = []
        _patch_switch(monkeypatch, calls)
        r = asyncio.run(client.probe())
        assert r.ok and r.channel == "proxy" and r.latency_ms == 561
        assert "考勤 600ms" in r.detail
        assert calls == []
    finally:
        _proxy_off()


def test_probe_proxy_ok_repins_exit(monkeypatch):
    """双端口可达但出口挂在 DIRECT（如 mihomo 重启后回默认）：拨回钉住组。"""
    _proxy_on()
    try:
        _patch_channels(monkeypatch,
                        proxy=(561, "节点A", "认证 561ms；考勤 600ms（节点A）"),
                        current="direct")
        calls = []
        _patch_switch(monkeypatch, calls)
        r = asyncio.run(client.probe())
        assert r.ok and r.channel == "proxy"
        assert calls == [f"{settings.proxy_group}-pin"]
    finally:
        _proxy_off()


def test_probe_node_dead_retest_rescues(monkeypatch):
    """钉住节点有端口不通：全量重测找到双端口都通的节点 → 可达（即时自愈）。"""
    _proxy_on()
    try:
        _patch_channels(monkeypatch,
                        proxy=(None, "死节点", "认证 561ms；考勤不通（死节点）"),
                        retest=(489, "节点B"))
        calls = []
        _patch_switch(monkeypatch, calls)
        r = asyncio.run(client.probe())
        assert r.ok and r.channel == "proxy" and r.latency_ms == 489
        assert "节点B" in r.detail
    finally:
        _proxy_off()


def test_probe_retest_empty_is_down_no_switch(monkeypatch):
    """钉住节点不通且全量重测也无可用节点：判不可达，维持现状不切换。"""
    _proxy_on()
    try:
        _patch_channels(monkeypatch,
                        proxy=(None, "死节点", "认证不通；考勤不通（死节点）"),
                        retest=(None, ""))
        calls = []
        _patch_switch(monkeypatch, calls)
        r = asyncio.run(client.probe())
        assert not r.ok and r.channel == "proxy"
        assert "全量重测无可用节点" in r.detail
        assert calls == []
    finally:
        _proxy_off()


def test_probe_no_proxy_direct_only(monkeypatch):
    """未配置代理（个人版/宿舍版）：只测直连双端口，代理通道函数不应被调用。"""
    _proxy_off()
    monkeypatch.setattr(client, "_probe_direct",
                        lambda t=8.0: _fake((False, 8000, "认证 450ms；考勤超时")))
    async def _boom(t=8.0):  # pragma: no cover - 不应被调用
        raise AssertionError("未配置代理不应测代理通道")
    monkeypatch.setattr(client, "_probe_proxy", _boom)
    r = asyncio.run(client.probe())
    assert not r.ok and r.channel == "direct"
    assert "考勤超时" in r.detail


def test_set_setting_if_absent_dedupes(tmp_path):
    """连红取消广播的并发占位去重：只有首个写入者拿到 True。"""
    _use_tmp_db(tmp_path)
    try:
        assert models.set_setting_if_absent("outage_notified:2026-10-01:am", "1") is True
        assert models.set_setting_if_absent("outage_notified:2026-10-01:am", "1") is False
    finally:
        _restore_db()


def test_cleanup_skip_flags(tmp_path):
    """过期手动取消标记只清昨天及以前的，今天的保留。"""
    _use_tmp_db(tmp_path)
    try:
        today = datetime.now(models.TZ).strftime("%Y-%m-%d")
        models.set_setting(f"skip_sign:{today}:am", "1")
        models.set_setting("skip_sign:2020-01-01:am", "1")
        models.set_setting("skip_sign:2020-01-01:pm", "1")
        models.set_setting("probe_last_state", "up")  # 其他键不受影响
        assert models.cleanup_skip_flags() == 2
        assert models.get_setting(f"skip_sign:{today}:am") == "1"
        assert models.get_setting("skip_sign:2020-01-01:am") is None
        assert models.get_setting("probe_last_state") == "up"
    finally:
        _restore_db()


# ---------- _probe_direct 真实 HTTP 双端口（本地服务器） ----------

import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _serve(status=200) -> ThreadingHTTPServer:
    handler = type("H", (_Handler,), {"status": status})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _port(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}"


def test_probe_direct_both_ports_alive(monkeypatch):
    sso, att = _serve(200), _serve(200)
    try:
        monkeypatch.setattr(client, "SSO_BASE", _port(sso))
        monkeypatch.setattr(client, "ATT_BASE", _port(att))
        ok, ms, detail = asyncio.run(client._probe_direct(5.0))
        assert ok and "认证" in detail and "考勤" in detail
    finally:
        sso.shutdown()
        att.shutdown()


def test_probe_direct_att_down_reports_layer(monkeypatch):
    """考勤端口关闭（SSO 正常）：判不可达，明细点名是考勤层——2026-10-01 事故形态。"""
    sso = _serve(200)
    dead = _serve(200)
    dead_port = _port(dead)
    dead.shutdown()  # 立即关停 → 连接拒绝
    try:
        monkeypatch.setattr(client, "SSO_BASE", _port(sso))
        monkeypatch.setattr(client, "ATT_BASE", dead_port)
        ok, ms, detail = asyncio.run(client._probe_direct(5.0))
        assert not ok
        assert "认证" in detail.split("；")[0] and "ms" in detail.split("；")[0]
        assert "考勤" in detail.split("；")[1] and "ms" not in detail.split("；")[1]
    finally:
        sso.shutdown()


def test_probe_direct_5xx_down_4xx_alive(monkeypatch):
    """5xx 算端口不通；4xx/302 等任何非 5xx 响应都算端口活着。"""
    s500, s404, s200 = _serve(500), _serve(404), _serve(200)
    try:
        monkeypatch.setattr(client, "SSO_BASE", _port(s500))
        monkeypatch.setattr(client, "ATT_BASE", _port(s200))
        ok, _, detail = asyncio.run(client._probe_direct(5.0))
        assert not ok and "500" in detail
        monkeypatch.setattr(client, "SSO_BASE", _port(s404))
        ok, _, _ = asyncio.run(client._probe_direct(5.0))
        assert ok
    finally:
        s500.shutdown()
        s404.shutdown()
        s200.shutdown()


# ---------- _proxy_group_retest 两阶段选点 ----------

def _fake_mihomo(monkeypatch, sso_delays, att_delays, put_calls, current=""):
    """sso_delays: group delay 返回值；att_delays: 单节点 delay 值（None=不通）；
    current: hospital-pin 当前钉住的节点（空 = 未钉，跳过粘性复验）。"""
    monkeypatch.setattr(client, "_node_scorer", None)

    class R:
        def __init__(self, code, data):
            self.status_code, self._d = code, data

        def json(self):
            return self._d

    async def fake_get(c, path, **kw):
        if path.startswith("/group/"):
            return R(200, sso_delays)
        if path.endswith("/delay"):
            name = path.split("/")[2]
            d = att_delays.get(name)
            return R(200, {"delay": d}) if d else R(408, {})
        return R(200, {"now": current})  # /proxies/{group}-pin

    async def fake_put(c, path, name):
        put_calls.append(name)

        class P:
            status_code = 204
        return P()

    monkeypatch.setattr(client, "_mihomo_get", fake_get)
    monkeypatch.setattr(client, "_mihomo_put", fake_put)


def test_group_retest_skips_node_with_att_closed(monkeypatch):
    """SSO 最快但考勤不通的节点要被跳过，钉两端口都通且最差延迟最小者。"""
    puts = []
    _fake_mihomo(monkeypatch,
                 sso_delays={"快节点": 100, "中节点": 200, "慢节点": 300, "坏节点": "x"},
                 att_delays={"快节点": None, "中节点": 250, "慢节点": 320},
                 put_calls=puts)
    ms, node = asyncio.run(client._proxy_group_retest(5.0))
    assert node == "中节点" and ms == 250
    assert puts == ["中节点"]


def test_group_retest_no_dual_port_node_returns_none(monkeypatch):
    """全池考勤端口都不通（医院关闭考勤端口的真实形态）：不钉选，报无可用节点。"""
    puts = []
    _fake_mihomo(monkeypatch,
                 sso_delays={"节点A": 100, "节点B": 200},
                 att_delays={"节点A": None, "节点B": None},
                 put_calls=puts)
    ms, node = asyncio.run(client._proxy_group_retest(5.0))
    assert ms is None and node == "" and puts == []


def test_group_retest_sticky_keeps_current_node(monkeypatch):
    """粘性：当前钉住节点双端口复验通过就保持不动，哪怕池里有明显更快的。"""
    puts = []
    _fake_mihomo(monkeypatch,
                 sso_delays={"快节点": 50, "当前节点": 500},
                 att_delays={"快节点": 60, "当前节点": 550},
                 put_calls=puts, current="当前节点")
    ms, node = asyncio.run(client._proxy_group_retest(5.0))
    assert node == "当前节点" and ms == 550 and puts == []


def test_group_retest_sticky_falls_through_when_current_dead(monkeypatch):
    """当前钉住节点死了才走池子换节点。"""
    puts = []
    _fake_mihomo(monkeypatch,
                 sso_delays={"死节点": 100, "活节点": 200},
                 att_delays={"死节点": None, "活节点": 220},
                 put_calls=puts, current="死节点")
    ms, node = asyncio.run(client._proxy_group_retest(5.0))
    assert node == "活节点" and ms == 220 and puts == ["活节点"]


def test_group_retest_prefers_iepl_over_faster_relay(monkeypatch):
    """类别分层：中转节点延迟再低，只要 IEPL 池有可用就不碰中转
    （日志实证：IEPL 成功率 42% vs 中转 16%）。"""
    puts = []
    _fake_mihomo(monkeypatch,
                 sso_delays={"🇭🇰|香港家宽-中转 02": 50, "🇭🇰|香港家宽-IEPL 02": 400},
                 att_delays={"🇭🇰|香港家宽-中转 02": 60, "🇭🇰|香港家宽-IEPL 02": 450},
                 put_calls=puts)
    ms, node = asyncio.run(client._proxy_group_retest(5.0))
    assert node == "🇭🇰|香港家宽-IEPL 02" and ms == 450
    assert puts == ["🇭🇰|香港家宽-IEPL 02"]


def test_group_retest_scorer_excludes_dead_node(monkeypatch):
    """战绩判死（scorer 返回 None）的节点被跳过，落到次优类别。"""
    _fake_mihomo(monkeypatch,
                 sso_delays={"🇭🇰|香港-IEPL 03": 100, "🇭🇰|香港-中转 02": 300},
                 att_delays={"🇭🇰|香港-IEPL 03": 120, "🇭🇰|香港-中转 02": 320},
                 put_calls=(puts := []))
    monkeypatch.setattr(client, "_node_scorer",
                        lambda n: None if "IEPL" in n else 1.0)
    ms, node = asyncio.run(client._proxy_group_retest(5.0))
    assert node == "🇭🇰|香港-中转 02" and puts == ["🇭🇰|香港-中转 02"]


def test_group_retest_scorer_reorders_within_class(monkeypatch):
    """层内按 延迟×战绩系数 排序：战绩差的低延迟节点输给战绩好的稍慢节点。"""
    _fake_mihomo(monkeypatch,
                 sso_delays={"🇭🇰|甲-IEPL 01": 100, "🇭🇰|乙-IEPL 01": 200},
                 att_delays={"🇭🇰|甲-IEPL 01": 110, "🇭🇰|乙-IEPL 01": 210},
                 put_calls=(puts := []))
    scores = {"🇭🇰|甲-IEPL 01": 3.0, "🇭🇰|乙-IEPL 01": 1.2}
    monkeypatch.setattr(client, "_node_scorer", scores.get)
    ms, node = asyncio.run(client._proxy_group_retest(5.0))
    # 甲 110×3.0=330 > 乙 210×1.2=252 → 选乙
    assert node == "🇭🇰|乙-IEPL 01" and puts == ["🇭🇰|乙-IEPL 01"]


def test_group_retest_scorer_fallback_when_pool_emptied(monkeypatch):
    """战绩过滤把池子掏空时放开过滤兜底：有节点可用就不判死。"""
    _fake_mihomo(monkeypatch,
                 sso_delays={"节点A": 100},
                 att_delays={"节点A": 120},
                 put_calls=(puts := []))
    monkeypatch.setattr(client, "_node_scorer", lambda n: None)
    ms, node = asyncio.run(client._proxy_group_retest(5.0))
    assert node == "节点A" and puts == ["节点A"]


def test_probe_warmup_skips_retest(monkeypatch):
    """换节点缓冲期内探测失败：如实报红但不重测不换（防抖动环），标记 warmup。"""
    _proxy_on()
    try:
        monkeypatch.setattr(client, "_last_switch", client.time.monotonic())
        retest_calls = []

        async def _retest(t=8.0):
            retest_calls.append(1)
            return (489, "节点B")
        monkeypatch.setattr(client, "_proxy_group_retest", _retest)
        monkeypatch.setattr(client, "_probe_proxy",
                            lambda t=8.0: _fake((None, "新节点", "认证不通；考勤不通（新节点）")))
        r = asyncio.run(client.probe())
        assert not r.ok and r.warmup and "缓冲期" in r.detail
        assert retest_calls == []
    finally:
        _proxy_off()


# ---------- models.node_score：节点战绩打分 ----------

def _att(run, node, result, n):
    for i in range(n):
        db.conn().execute(
            "INSERT INTO sign_attempts (run_id, user_id, round, result, message, node,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (run, 1, i + 1, result, "", node, "2026-09-30 13:00:00"))
    db.conn().commit()


def test_node_score_dead_star_and_unknown(tmp_path):
    """死节点 None、明星低惩罚、小样本不干预；医院故障日不污染战绩。"""
    _use_tmp_db(tmp_path)
    models._node_stats_cache = None
    try:
        # 平台可用场次（场内有成功）：死节点 10 连败，明星 6/10
        _att("2026-09-29-pm", "死节点", "failed", 10)
        _att("2026-09-29-pm", "明星", "success", 6)
        _att("2026-09-29-pm", "明星", "failed", 4)
        _att("2026-09-29-pm", "小样本", "failed", 3)
        # 医院故障场次（场内无成功）：20 连败不应计入任何节点战绩
        _att("2026-10-01-am", "背锅侠", "failed", 20)
        assert models.node_score("死节点") is None
        assert abs(models.node_score("明星") - 1.8) < 1e-6
        assert models.node_score("小样本") == 1.0
        assert models.node_score("背锅侠") == 1.0  # 故障场次的失败不计入
        assert models.node_score("没见过的新节点") == 1.0
    finally:
        models._node_stats_cache = None
        _restore_db()


# ---------- _fmt_exc：超时异常空消息补类型名 ----------

def test_fmt_exc_fills_empty_timeout_message():
    import httpx
    assert client._fmt_exc(httpx.ConnectTimeout("")) == "ConnectTimeout"
    assert "ConnectError" in client._fmt_exc(httpx.ConnectError(" refused"))
