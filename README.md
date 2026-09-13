# lazy-clerk

实习医生考勤自动签到系统。在医院统一登录平台（SSO）与实习考勤系统之间自动完成
每天上/下午两个时段的签到，签到结果推送到微信（Server 酱）。

## 三种形态

同一套签到核心（`app/core/`），三种部署形态，按场景任选其一：

| | 个人版 | 宿舍版 | 公网版 |
| --- | --- | --- | --- |
| 一句话定位 | 每人电脑上一个 exe | 一台常开电脑服务全宿舍 | 服务器上多账号服务 |
| 适用规模 | 1 人 | 4–6 人 | < 20 人 |
| 运行环境 | Windows，双击即用 | Windows，长期开机 | Linux + Docker |
| 调度方式 | Windows 计划任务 | Windows 计划任务 | APScheduler（容器内） |
| 触发时间 | 6:58 / 13:58 准点 | 6:53–6:58 / 13:53–13:58 随机 | 6:53–6:58 / 13:53–13:58 随机 |
| 管理界面 | 命令行交互菜单 | 浏览器管理页（仅管理员，随用随开） | 浏览器（用户页 + 管理页） |
| 账号管理 | 本人配置向导 | 管理员代录（先过医院 SSO 验证） | 邀请码自助注册 |
| 数据存储 | config.json + sign.log | SQLite | SQLite |
| 风控暴露面 | 家庭正常 IP，最低 | 校园网 IP，低 | 机房 IP 代理多账号，**曾被封禁** |
| 文档 | [personal/README.md](personal/README.md) | [dorm/README.md](dorm/README.md) | [server/README.md](server/README.md) |

选择建议：一个人用选**个人版**；宿舍合用一台常开电脑选**宿舍版**；有可信网络环境的
服务器再考虑**公网版**（2026-09 曾因"单 IP 代理多账号"被医院 SSO 封禁，解封后有限维护）。

## 仓库结构

```
app/        三版共享层：core（登录链路/签到编排/推送/计划任务助手）、models、db、config
server/     公网版：FastAPI 路由与页面、APScheduler、Dockerfile、部署与冒烟脚本
dorm/       宿舍版：exe 入口（web/sign/install/uninstall）+ 管理员页面模板
personal/   个人版：exe 入口（交互菜单 + setup/sign/status/install/uninstall）
tests/      核心逻辑单测（pytest）
```

医院系统改版时只需改 `app/core/client.py`，三版同时生效。设计与决策的完整记录见
[lazy-clerk 设计稿.md](lazy-clerk%20设计稿.md)（含三分支一致性约定 15.4）。

## 风险声明（必读）

- **密码明文存储**：签到链路必须持有可登录的原始密码，无法哈希存储。个人版存本机
  config.json，宿舍/公网版存 SQLite。请妥善保管数据文件，不要发给别人。
- 目标系统为 HTTP 明文协议，凭证与签到请求在链路上不加密——这是医院系统的现状，
  本工具无法改变。
- 本工具仅自动化"本人按时到岗后的例行点击"，**不支持也不应用于虚构出勤**。
- 使用者需自行确认所在单位对自动化签到的管理规定。

## License

MIT
