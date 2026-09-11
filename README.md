# lazy-clerk

实习医生考勤自动签到系统。每天 6:58 / 13:58 自动登录医院统一登录平台，进入实习考勤系统完成当前时段签到。

- FastAPI 单体应用，单容器部署
- 多账号（< 20 人），邀请码自助注册
- 用户登录后可查看近 7 天签到记录、手动签到、配置个人 Server 酱 SendKey
- 管理页查看各账号签到状态与历史
- 签到失败通过 Server 酱推送告警

## 快速开始

```bash
cp .env.example .env   # 填写 ADMIN_PASSWORD / SERVERCHAN_SENDKEY / SESSION_SECRET
docker compose up -d --build
```

> 服务器部署的完整流程（含数据持久化、验证清单、故障排查）见 [docs/deploy-server.md](docs/deploy-server.md)。
> 注意：`ADMIN_PASSWORD` 只在数据库首次初始化时播种，此后在管理页"管理员设置"中修改。

生成邀请码（明文只打印一次，请线下分发给同事）：

```bash
docker compose exec lazy-clerk python scripts/gen_invites.py --count 50
```

访问：用户页 `http://127.0.0.1:8787/`，管理页 `http://127.0.0.1:8787/admin`。

## 冒烟测试

部署前用真实账号验证核心链路：

```bash
python scripts/smoke.py --account <工号> --password <密码>   # S0–S3，窗口外可跑
python scripts/smoke.py --stage S5 --sendkey <SendKey>       # 通知链路
python scripts/smoke.py --stage S6 --url <服务地址> --invite <邀请码>  # 注册闭环
```

S1–S3 可随时重跑，是医院系统改版后的日常回归手段。

## 风险声明（必读）

- **用户密码明文存储**：签到链路必须持有可登录的原始密码，无法哈希存储。数据库文件已限制权限（容器内非 root 运行、数据卷挂载），请自行加固服务器。使用者注册前应知悉这一点。
- 目标系统为 HTTP 明文协议，凭证与签到请求在链路上不加密——这是医院系统的现状，本工具无法改变。
- 本工具仅自动化"本人按时到岗后的例行点击"，**不支持也不应用于虚构出勤**。
- 使用者需自行确认所在单位对自动化签到的管理规定。

## License

MIT
