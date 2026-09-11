# lazy-clerk 服务端部署文档（交付服务端 agent 执行）

> **任务边界**：你的任务是将 lazy-clerk 以 Docker 容器方式运行在服务器上，做到
> `http://127.0.0.1:8787` 可正常访问、数据持久化、服务开机自启为止。
> **不要做**端口反代、域名绑定、HTTPS 证书——这些由需求方另行处理。
> 完成后按第 8 章"交付清单"汇报。

---

## 1. 前置条件检查（逐项确认后再动手）

```bash
docker --version && docker compose version   # 需要 Docker 20.10+ 与 compose 插件
curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://my.gzsums.net:1118/   # 应返回 200
curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://my.gzsums.net:1198/   # 应返回 200 或 3xx
ss -tlnp | grep 8787 || echo "8787 空闲"       # 确认端口无冲突
```

- **医院平台可达性是硬前提**：本服务每天要向 `my.gzsums.net:1118/1198` 发请求。
  若不通，立即停止部署并回报需求方（通常意味着服务器不在医院内网）。
- 若 8787 被占用：改 `docker-compose.yml` 的端口映射为 `127.0.0.1:<空闲端口>:8000`，
  并在交付清单中注明实际端口。

## 2. 获取代码

任选其一：

```bash
# 方式 A：git
git clone <仓库地址> lazy-clerk && cd lazy-clerk

# 方式 B：压缩包（需求方提供 lazy-clerk.tar.gz）
tar xzf lazy-clerk.tar.gz && cd lazy-clerk
```

确认目录内包含：`app/`、`scripts/`、`Dockerfile`、`docker-compose.yml`、
`requirements.txt`、`.env.example`。若缺任何一项，停止并回报。

## 3. 配置 .env

```bash
cp .env.example .env && chmod 600 .env
```

编辑 `.env`，三项必填：

```bash
ADMIN_PASSWORD=<强密码，至少 12 位>      # 管理页密码，仅首次启动播种入库
SERVERCHAN_SENDKEY=<需求方提供的 SendKey> # 管理员微信告警（Server 酱 Turbo）
SESSION_SECRET=<openssl rand -hex 32 生成>
TZ=Asia/Shanghai
```

说明：
- `ADMIN_PASSWORD` **只在数据库首次初始化时生效**，此后修改入口在管理页
  "管理员设置"里（数据库哈希优先）。这不是 bug，是设计。
- `SESSION_SECRET` 变更会使所有已登录 session 失效，除此之外无影响。

## 4.（可选）迁移内测数据

若需求方提供了内测数据库文件 `lazy-clerk.db`（含已注册用户与邀请码）：

```bash
mkdir -p data
# 将需求方给的 lazy-clerk.db 放到 data/lazy-clerk.db
```

此时 `ADMIN_PASSWORD` 不会再生效（库中已有管理员密码哈希），
管理员密码沿用内测期间的设置。若需求方未提供，跳过此步，部署后重新注册账号即可。

## 5. 构建与启动

```bash
mkdir -p data
docker compose up -d --build
docker compose ps        # 应为 running
docker compose logs --tail 50   # 应看到 "调度器已启动（时区 Asia/Shanghai）"
```

关键点（已固化在仓库文件中，无需手动改）：
- 容器以**非 root 用户**运行；镜像内已装 `tzdata`，时区由 `TZ` 控制
- 端口映射 `127.0.0.1:8787:8000`——**只监听本机回环**，等需求方的反代接手，
  不要改成 `0.0.0.0`
- `restart: unless-stopped`，开机自启已涵盖
- uvicorn 以 `--proxy-headers` 运行，信任反代传来的 `X-Forwarded-For`

## 6. 数据持久化

- 全部状态在 `./data/lazy-clerk.db`（SQLite），compose 已挂载 `./data:/app/data`
- **备份即复制该文件**。建议加一条宿主机 cron 每日备份，例如：

```bash
# 每日 4:00 备份，保留 14 份（可选但推荐）
0 4 * * * cp /path/to/lazy-clerk/data/lazy-clerk.db /path/to/backups/lazy-clerk-$(date +\%F).db && ls -t /path/to/backups/lazy-clerk-*.db | tail -n +15 | xargs -r rm
```

- 应用自身每日 3:30 清理 90 天前的签到日志，无需人工干预

## 7. 验证清单（全部通过才算完成）

```bash
# 7.1 页面可达（均应 200；/me、/admin 未登录返回 303 属正常）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/login
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/register
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/admin/login

# 7.2 核心链路冒烟（需求方提供一个真实工号/密码用于验证，窗口外可跑）
docker compose exec lazy-clerk python scripts/smoke.py --account <工号> --password '<密码>'
# 通过标准：S0–S3 全部 [PASS]

# 7.3 通知链路（需求方提供 SendKey）
docker compose exec lazy-clerk python scripts/smoke.py --stage S5 --sendkey '<SendKey>'
# 通过标准：[PASS] 且需求方微信收到 "lazy-clerk 冒烟测试 S5"

# 7.4 生成邀请码（明文会打印，记录并转交需求方）
docker compose exec lazy-clerk python scripts/gen_invites.py --count 10
```

**7.5 真实签到（S4）只能在签到窗口（5:00–8:00 / 13:00–14:30）内验证。**
若部署时不在窗口内，跳过此项并在交付清单中注明"待次日 6:58 自动首跑确认"。

## 8. 交付清单（部署完成后向需求方汇报）

- [ ] 服务地址：`http://127.0.0.1:8787`（或实际端口）
- [ ] 冒烟结果：S0–S3、S5 是否全过
- [ ] 邀请码明文（如已生成）
- [ ] 数据文件路径与备份策略落实情况
- [ ] 遗留事项（如 S4 待窗口验证）

## 9. 交接给需求方的反代注意事项（你只需转告，不要动手）

- 反代目标 `http://127.0.0.1:8787`，纯 HTTP 即可，HTTPS 终结在反代层
- 需传递真实客户端 IP（登录限流依赖它）：
  nginx 示例 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`
  并带 `proxy_set_header X-Forwarded-Proto $scheme;`
- 无 WebSocket、无长连接，普通反向代理即可
- 站点无静态资源分离需求，全部走反代即可

## 10. 运维速查

| 需求 | 命令 |
| --- | --- |
| 看日志 | `docker compose logs -f --tail 100` |
| 重启 | `docker compose restart` |
| 升级（代码更新后） | `git pull && docker compose up -d --build` |
| 回滚 | `git checkout <旧版本> && docker compose up -d --build` |
| 进容器排查 | `docker compose exec lazy-clerk bash` |
| 补生成邀请码 | `docker compose exec lazy-clerk python scripts/gen_invites.py --count 5` |

## 11. 故障排查

| 现象 | 排查 |
| --- | --- |
| 容器起不来 | `docker compose logs` 看报错；常见为 `.env` 缺失或格式错误 |
| 页面 200 但签到全部失败 | 容器内 `curl http://my.gzsums.net:1118/` 确认医院平台可达 |
| 签到时间不对/不触发 | `docker compose exec lazy-clerk date` 确认容器时区为 CST (+0800) |
| 用户登录被锁 | 登录限流为 1 分钟 5 次失败锁 10 分钟，等锁定过期即可 |
| 告警收不到 | 用 7.3 的 S5 复测；检查管理页"管理员设置"里的 SendKey |
