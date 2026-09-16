# 代理逃生通道（mihomo）已知问题与待排查项

> 记录公网版代理逃生通道（mihomo sidecar）在上线部署过程中发现的问题。
> 分「已修复」与「待排查」两部分；「待排查」不影响签到主流程，仅影响监控精度与体验。

## 已修复（本次提交）

### 1. `docker-compose.yml`：sed 注入脚本未执行

`metacubex/mihomo` 镜像自带 `ENTRYPOINT /mihomo`，而 compose 里用
`command: sh -c "sed ..."` 注入订阅链接/API 密钥——`command` 只覆盖 CMD、不覆盖
ENTRYPOINT，导致整段 `sh -c` 被当成参数喂给 `/mihomo`，sed 从未执行，mihomo 以默认
空配置启动（无订阅、无分流规则）。

- 修复：加 `entrypoint: ["sh", "-c"]`；命令改为 list 单元素形式（避免 compose 按
  空白拆成多参数）；`exec mihomo` 改为 `exec /mihomo`（二进制在 `/mihomo`，不在 PATH）。

### 2. `config.template.yaml`：`allow-lan: false` 导致 app 连不上代理

`allow-lan: false` 时 mixed-port 只绑定 mihomo 容器内部 `127.0.0.1:7890`，app 容器
经 docker 网络用 `mihomo:7890` 访问时连接失败（`All connection attempts failed`）。

- 修复：`allow-lan: true`。7890 未映射到宿主，仅 docker 内网可达，安全无虞；与
  `external-controller: 0.0.0.0:9090`（宿主只映射 127.0.0.1）口径一致。

## 待排查（不影响签到主流程）

### 3. fallback 组不自动切换（重点）

`hospital` fallback 组设计意图是「直连优先，直连被封自动跌到最快节点，恢复自动切回」，
但实测**不会自动切换**：

- 现象：直连被封时，DIRECT 健康检查已确认 `alive=false`、节点侧 `hospital-auto`
  已测出真实延迟（2409ms），`hospital` 组仍停在 `DIRECT`；`hospital` 组 `history` 一直为空。
- 证据：容器启动后观察约 8 分钟仍停在 DIRECT；主动发请求返回 502（直连超时）后仍不切换。
- 影响（已被 app 主动切换兜底）：签到主流程不受影响——app 赛前探针
  `probe(switch_on_fail=True)` 会主动调 `mihomo_switch("hospital-auto")` 切到代理
  （已实测到院 HTTP 200）。代价是：
  - 小时级探针在封禁期间记 `down`（红）而非 `proxy`（蓝），热力图偏红；
  - 每次签到首探失败后要等 20 秒复核才切，多一次无效探测。
- 待查方向：mihomo `fallback` 组对内置 `DIRECT` 代理、以及嵌套子组 `hospital-auto`
  的健康检查/切换触发条件；可能需要显式 `lazy: false` 或调整健康检查超时（全局
  `health-check` 块）后再验证。

### 4. 订阅 fetch 的 `forbid` 日志噪音

mihomo 拉订阅时反复打印
`Unsolicited response received on idle HTTP channel starting with "forbid..."`
（Go HTTP/2 协议层报错）。但订阅实际能加载成功（100 节点），疑似机场 CDN 对 HTTP/2
请求的异常响应或用户信息头（`Subscription-Userinfo`）解析失败所致，暂不影响功能。
日志量大时建议在机场侧确认订阅端点的 HTTP/2 兼容性，或临时把 mihomo 的订阅拉取
降级到 HTTP/1.1。

### 5. 启动后第一轮健康检查误报

容器启动约 5 秒后跑的第一轮健康检查，对节点测出 `delay=0`（误判不可达），需等下一轮
（实测约 3 分钟）才测出真实延迟。期间若依赖 fallback 自动切换，会短暂停留在 DIRECT。
属启动时序问题，通常自愈；与第 3 条（fallback 不切换）叠加时会被放大。

### 6. 订阅里混入非节点条目

订阅解码后混入 `剩余流量：49.06 GB`、`套餐到期：长期有效` 等非节点行，会出现在
`hospital-auto` 的节点列表里。url-test 实测不会选中它们，暂无功能影响；如需纯净列表
可在订阅侧清理，或在配置里用 provider 的过滤规则剔除。

## 备注

- 镜像拉取：`docker.1ms.run` 镜像加速器对 `metacubex/mihomo` 的大层拉取会卡死，
  部署侧改用 `dockerproxy.net/metacubex/mihomo:latest` 拉取后 `docker tag` 成本地镜像。
  属部署环境问题，非代码问题，故不入库。
