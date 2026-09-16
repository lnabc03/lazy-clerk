# 代理逃生通道（mihomo）已知问题与架构决策

> 记录公网版代理逃生通道（mihomo sidecar）的架构决策与已修复问题。
> v0.3.0 重构后：**app 双通道探针是出口决策的唯一大脑**，mihomo 只执行。

## 架构（v0.3.0 起）

- `hospital` 组为 **selector**（`DIRECT` / `hospital-auto`），不自主切换——
  出口选择由 app 探针按双通道实测决定（见 `app/core/client.py probe()`）。
- `hospital-auto` 为 url-test（每 5 分钟按医院首页实测延迟选最快节点），
  负责**节点级**自动选优；`exclude-filter` 剔除套餐信息类假节点。
- 探针每测一次 = 直连（本机）+ 代理（mihomo delay API 经当前节点）各一发；
  直连不通且当前节点也不通时，触发全量重测即时换节点，不等测速周期。

## 关键运维认知（2026-09-16 线上排查定论）

### 1. 502 只能来自代理链路，直连失败永远表现为超时

医院对服务器 IP 的封禁是**静默丢包**，直连失败 = 超时，不产生 HTTP 状态码。
app 所有医院请求统一走 `PROXY_URL`，因此每一个 502 都是代理链路产生的：

- mihomo 日志**有** `dial hospital ... error` 警告 → mihomo 到节点拨号失败
  （自产 502，如组被手动切到 DIRECT 而直连被封时）；
- mihomo 日志**无**拨号警告 → 拨号成功，502 是节点上游（机场中转链路/
  家宽出口）返回后透传的，属节点链路质量问题。

### 2. 节点的逐连接随机失败，单样本健康检查抓不住

机场中转入口的后端出口疑似逐连接轮询：同一节点单次测速通过（url-test
每 5 分钟 1 个样本，判定「健康」），下一分钟真实请求仍可能 502。
实测香港家宽节点延迟在 381ms↔4418ms 间摆动并间或测速失败。

对策（已实施）：`HospitalClient._request` 对 5xx/传输错误有限重试
（登录链路与拉列表，签到动作本身靠外层 5 分钟重试循环兜底）；
check-all 限并发 3 + 错峰 1–3 秒/账号。

### 3. 单点操作成功、批量操作全挂 ≠ 网络配置不平行

所有请求同一条代理代码路径。逐请求随机失败率 p 时，单点操作（1–3 个请求）
经常蒙混过关；check-all（9 账号 × 3–4 请求 ≈ 30 次）几乎必踩——
现象上是「只有批量操作挂」，实质是请求数放大了同一失败率。

### 4. 医院侧疑似地域/信用限制，可用节点池远小于订阅规模

全量测速 100 个订阅节点，只有 18 个（港澳台的 16 个 + 2 个假节点）能拿到
医院首页的 200/302；日美欧节点长期全挂。选节点时以实测为准，勿迷信订阅规模。

## 已修复

### v0.3.0

- **fallback 组反复横跳/不切换**：组类型改 selector，出口决策收归 app 探针
  （原 fallback 健康检查对 DIRECT 的判定不稳，与 app 逃生切换互相覆盖）。
- **管理页代理卡片 404 刷屏**：节点列表改读 `/providers/proxies`
  （订阅节点不注册在 `/proxies` 命名空间），按实测延迟排序、只列可达节点。
- **节点死亡无自愈兜底**：探针在直连与当前节点双失败时触发全量重测
  （`/group/hospital-auto/delay`），即时重选节点，不等 5 分钟周期。
- **check-all 打爆节点链路**：并发 9 → 信号量限 3，错峰 0.5–2s → 1–3s。
- **502 文案误导**（「代理节点或上游异常」在 DIRECT 被墙时也出现）：
  双通道探针分开报告两通道状态，语义明确。
- **失败探测 channel 恒为 direct**：双不通时记录当前实际出口。
- **httpx INFO 日志噪音**：降为 WARNING。
- **订阅假节点参与测速**：`exclude-filter` 剔除（剩余流量/套餐到期等）。
- **订阅每天才刷新**：改为每小时（`interval: 3600`），节点上下线/换 IP 及时跟进。

### v0.2.0（部署修复）

- `docker-compose.yml`：mihomo 镜像自带 `ENTRYPOINT /mihomo`，compose 的
  `command: sh -c "sed ..."` 不覆盖 ENTRYPOINT，sed 注入从未执行。
  修复：加 `entrypoint: ["sh", "-c"]`，命令改 list 单元素形式。
- `config.template.yaml`：`allow-lan: false` 时 mixed-port 只绑容器内
  127.0.0.1，app 容器经 docker 网络连不上。修复：`allow-lan: true`
  （7890 未映射宿主，仅 docker 内网可达）。

## 备注

- `Unsolicited response received on idle HTTP channel starting with "forbid..."`
  日志：每 5 分钟整批出现于**健康检查**（非订阅拉取，订阅每天一次）——
  某些节点出口对医院 URL 返回拦截页（出口 IP 也被医院拉黑），迟到字节
  落在已空闲的 HTTP 连接上所致。无害，量大时可把 mihomo log-level 调 error。
- 镜像拉取：`docker.1ms.run` 对 `metacubex/mihomo` 大层会卡死，部署侧改用
  `dockerproxy.net/metacubex/mihomo:latest` 拉取后 `docker tag` 成本地镜像。
- 启动后第一轮健康检查可能测出 `delay=0` 误报，下一轮（约 5 分钟）自愈。
