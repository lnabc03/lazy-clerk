# lazy-clerk 公网版

服务器上的多账号自动签到服务：FastAPI 单体应用 + SQLite，Docker 单容器部署，
服务 < 20 人规模。

> **风控警示**：2026-09 本形态曾因"单 IP 代理多账号"被医院 SSO 封禁服务器 IP。
> 解封后已加拟人化随机调度（触发时刻、账号间隔、重试间隔均带随机），但机房 IP
> 代理多账号的结构性风险仍在，仅在可信网络环境的服务器上使用，并优先推荐个人版/宿舍版。

## 特性

- 邀请码自助注册，注册前先对医院 SSO 真实认证工号密码
- 用户页：近 7 天签到记录（精确到秒）、手动签到、配置个人 SendKey、更正密码、
  医院连通性检测、自助停用/启用自动签到、注销账号
- 管理页：账号总览（真实状态徽标）、签到状态检测、医院连通性检测、邀请码管理、
  手动代签、管理员设置
- 医院系统可及性热力图（近 7 天 × 24 小时，每小时匿名探测），登录页/用户页/管理页
  同图展示；可及性状态翻转时自动推送管理员（封禁/解封第一时间知晓）
- 每天 6:53–6:58 / 13:00–13:05 随机时刻自动签到，多账号并行、随机间隔 10–40 秒错峰
- 赛前守卫：签到开跑前探测医院系统，连续两次不可达则整轮跳过并通知管理员，
  不在被封网络上无效敲门
- 失败最多重试 2 次（共 3 次尝试，间隔约 5 分钟随机）；最终失败推送附连通性诊断，
  区分「系统不可达」与「账号问题」
- 代理逃生通道（可选）：医院封服务器 IP 时，mihomo sidecar 直连优先、被封自动切
  订阅里最快节点、恢复自动切回；热力图蓝格标记代理时段，出口切换推送管理员
- 签到结果微信推送：成功推本人，请假/失败/需人工分别按场景推本人或管理员+本人
- 登录防爆破限流（用户/管理员入口独立计数）

## 安装（首次部署）

前置条件：Docker 20.10+ 与 compose 插件；服务器能访问医院平台：

```bash
curl -s -o /dev/null -w "%{http_code}\n" --max-time 10 http://my.gzsums.net:1118/   # 应 200
```

部署：

```bash
cd server
cp .env.example .env && chmod 600 .env
# 编辑 .env 三项必填：ADMIN_PASSWORD（≥12 位）/ SERVERCHAN_SENDKEY / SESSION_SECRET（openssl rand -hex 32）
mkdir -p data
docker compose up -d --build
docker compose logs --tail 50   # 应看到"调度器已启动（时区 Asia/Shanghai）"
```

- 端口映射 `127.0.0.1:8787:8000`——**只监听本机回环**，反代/HTTPS/域名由部署者自行处理
  （反代需传递 `X-Forwarded-For` 与 `X-Forwarded-Proto`，登录限流依赖真实 IP）
- `ADMIN_PASSWORD` 只在数据库首次初始化时播种，此后在管理页"管理员设置"修改
- 数据在 `server/data/lazy-clerk.db`，备份即复制该文件；应用每日 3:30 自动清理 90 天前日志

### 从旧布局（v0.1.x）升级

旧部署的 `.env` 和 `data/` 在部署目录根部，新布局移入 `server/`：

```bash
mv .env server/.env && mv data server/data
cd server && docker compose up -d --build
```

## 使用

- 用户页 `http://127.0.0.1:8787/`，管理页 `http://127.0.0.1:8787/admin`
- 生成邀请码：管理页"生成一个"，或 `docker compose exec lazy-clerk python server/scripts/gen_invites.py --count 10`
- 冒烟验证（医院改版后的日常回归手段，S1–S3 窗口外可跑）：

```bash
docker compose exec lazy-clerk python server/scripts/smoke.py --account <工号> --password '<密码>'
docker compose exec lazy-clerk python server/scripts/smoke.py --stage S5 --sendkey '<SendKey>'
```

## 注意事项

- **每账号独立 IP 是做不到的**——多账号必然共享出口 IP，这是本形态被风控的根源；
  控制账号规模（< 20），不要在同一服务器上叠加其他会访问医院平台的自动化
- 容器内时区由 `TZ` 控制（默认 Asia/Shanghai），签到时间不对先查 `docker compose exec lazy-clerk date`
- 容器以非 root 运行；镜像内已装 tzdata；uvicorn 以 `--proxy-headers` 运行

## 风险

- **用户密码明文存储**（签到链路必须持有可登录的原始密码），请自行加固服务器，
  使用者注册前应知悉这一点
- 目标系统为 HTTP 明文协议，凭证在链路上不加密——医院系统现状，本工具无法改变
- 仅自动化"本人按时到岗后的例行点击"，不支持也不应用于虚构出勤

## 运维速查

| 需求 | 命令 |
| --- | --- |
| 看日志 | `docker compose logs -f --tail 100` |
| 重启 | `docker compose restart` |
| 升级（压缩包方式） | 解压新包覆盖代码（包内不含 `.env` 与 `data/`，覆盖安全）→ `docker compose up -d --build`（启用了代理则用 `docker compose --profile proxy up -d --build`） |
| 进容器排查 | `docker compose exec lazy-clerk bash` |
| 代理节点管理 | `server/scripts/node.sh list / use <节点名> / direct` |

## IP 被封：启用代理逃生通道

医院防火墙按 IP 画像封禁（机房 IP、异常集中的校园网 IP 都可能中招），解封周期不可控。
本形态内置可选的 mihomo sidecar：签到流量直连优先，直连健康检查失败（被封）自动切到
订阅里延迟最低的节点，直连恢复自动切回。全程无需值守。

启用（一次性）：

```bash
cd server
# .env 追加四行（见 .env.example 注释）：PROXY_SUB_URL / MIHOMO_SECRET / PROXY_URL / MIHOMO_API
docker compose --profile proxy up -d
```

之后管理页热力图会出现蓝格（代理正常），出口切换时管理员会收到 🔀 推送；
管理页「代理出口」卡片可随时查看当前出口、各节点实测延迟并手动切换
（等价于 `server/scripts/node.sh`）。
注意：商用节点的出口同样是机房 IP，画像风险并未消除只是转移——个人版/宿舍版冗余保留。

## 故障排查

| 现象 | 排查 |
| --- | --- |
| 容器起不来 | `docker compose logs`；常见为 `.env` 缺失或格式错误 |
| 页面 200 但签到全部失败 | 容器内 `curl http://my.gzsums.net:1118/` 确认医院平台可达（不通 ≈ IP 被封） |
| pip 构建卡死 | Dockerfile 已内置清华镜像源，勿移除；确认服务器能访问镜像站 |
| 用户登录被锁 | 限流为 1 分钟 5 次失败锁 10 分钟，等锁定过期即可 |
| 告警收不到 | 跑一遍 S5 冒烟；检查管理页"管理员设置"里的 SendKey |
