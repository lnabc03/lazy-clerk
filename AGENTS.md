# AGENTS.md

给 AI 编码助手的项目指引。读完这份再动手。

## 项目是什么

lazy-clerk：实习医生考勤自动签到。每天 5:00/13:00 自动登录医院平台
（SSO 1118 端口 → 考勤 1198 端口的完整链路）完成签到，微信推送结果。
三种形态共用一套核心：`personal/`（单人 exe）、`dorm/`（宿舍常开机 + 管理页）、
`server/`（公网版：FastAPI + SQLite + mihomo 代理 sidecar + APScheduler，Docker 部署）。

## 仓库结构与分层铁律

```
app/        三形态共用核心：core/(client/signer/notify/crypto)、models、db、config、templates
server/     公网版：routes、scheduler、mihomo、Dockerfile
dorm/       宿舍版      personal/   个人版
tests/      pytest，无 fixture 框架约定，全部用 monkeypatch + tmp 库
```

- **分层方向不可逆**：`app/core/client.py` 是被三形态共享的最底层，
  不许 import models/db（server 的节点战绩通过 `client.set_node_scorer()` 注入，
  这是既有的反向依赖解法，照此模式扩展）。`models` 可以 import client。
- 医院平台改版只需改 `app/core/client.py`，三形态同时生效——别把医院协议
  细节泄进 server/dorm/personal。
- 配置全部集中在 `app/config.py` 的 `settings`，其余模块只 import 不读 env。

## 代理选点：单一大脑原则（2026-10-01 定稿，勿破坏）

换节点决策**只能**发生在 `probe()` → `_proxy_group_retest()` 一条链上：

- **粘性**：重测先复验当前钉住节点，双端口通过就保持不动。
- **暖机**：换节点后 `WARMUP_SECONDS`(180s) 内只测不换；`ProbeResult.warmup=True`
  的失败是非决策级证据——不计红、不落库、不告警，所有消费方（门控/赛前守卫/
  调度状态机）都要中性化处理。
- **类别分层 + 战绩降权**：IEPL > 未知 > 直连 > 中转，层内按
  （最差端口延迟 × `node_score` 系数）排序；scorer 返回 None = 死节点禁用。
- **不要在请求链路里加换节点逻辑**（历史教训：连败 2 次就换节点的请求级自愈
  造成每 1-3 分钟抖动一次，已删除）。
- 后台依据见 `server/mihomo/KNOWN_ISSUES.md` 开头两节，改代理逻辑前先读。

## 签到语义（容易踩的坑）

- 终态以 `sign_logs` 为权威；`sign_attempts` 只是过程证据（日志分析、节点
  战绩都遵守这个口径）。终态枚举见 `signer.py` 顶部 RESULT_* 常量，
  含 `cancelled`（连红 8 次自动取消 / 管理员手动取消）。
- 节点战绩只统计「平台可用场次」（场内有成功的 run），医院侧故障日
  （如 2026-10-01 考勤端口关闭）不许污染节点战绩。
- 通知文案只陈述探测事实，不做故障归因猜测（"疑似防火墙"这类措辞已被清除，勿回潮）。
- 整轮取消的通知只有一个出口：`signer.broadcast_round_cancelled()`（赛前守卫/
  连红取消/管理员手动取消共用，settings 原子占位去重，同场次用户只收一条）。
  签到窗口内（含赛前半小时）定时探测故障告警静默，由取消广播覆盖。
- httpx 超时异常 `str()` 为空，日志/通知必须过 `_fmt_exc()`。

## 开发命令

```bash
python3 -m pytest tests/ -q        # 全量测试（test_scheduler 需 apscheduler，
                                   # 宿主机没有会自动 skip；改调度逻辑时务必
                                   # 用隔离环境跑它，勿删 importorskip）
cd server && docker-compose build lazy-clerk && docker-compose up -d lazy-clerk   # 部署公网版
```

- 提交即部署：公网版跑在生产容器里，改完跑测试 → commit → 重建容器 →
  检查 `docker logs` 零报错 + 启动探测正确落库 `probe_logs`。
- 中文注释/docstring；注释解释"为什么"（常带事故/数据出处），不复述代码。
- 时区统一 `settings.tz`（Asia/Shanghai），落库时间字符串格式 `%Y-%m-%d %H:%M:%S`。

## 测试约定

- mock 外部依赖用 `monkeypatch.setattr` 打模块属性（如 `client._probe_proxy`），
  模块级全局状态（`_last_switch`/`_node_scorer`/`models._node_stats_cache`）
  必须在 fake 里重置，否则测试间互相污染（暖机时钟尤其隐蔽）。
- DB 测试用 `_use_tmp_db(tmp_path)` 指向临时库，finally 里 `_restore_db()`。
