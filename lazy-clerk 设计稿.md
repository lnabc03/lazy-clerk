# lazy-clerk 设计稿

> 实习医生考勤自动签到系统 · MIT 开源
> 更新：2026-09-20（三种形态平行维护：个人版 / 宿舍版 / 公网版。公网版代理逃生
> 通道已成熟——双通道探针统一决策出口、钉节点架构（见 `server/mihomo/KNOWN_ISSUES.md`），
> 已连续 3 天全账号全绿。三版共享 `app/core` 核心链路）

---

## 1. 项目概述

**lazy-clerk** 是一个面向实习医生的考勤自动签到工具。每天定时（上午约 5:00–5:05、下午约 13:00–13:05，具体见各形态）自动登录医院统一登录平台，进入实习考勤系统，完成当前时间段的签到。

- **License**：MIT
- **用户规模**：< 20 人，多账号
- **部署形态**：个人版（Windows exe 单人）/ 宿舍版（Windows exe + 管理页，4–6 人）/ 公网版（Linux + Docker 多账号，< 20 人），三版共享 `app/core`
- **技术栈**：Python 3.11+ / FastAPI / APScheduler / SQLite / httpx / pycryptodome / Jinja2

### 1.1 目标

- 每天两次定时全自动签到，含周末
- 支持多账号（< 20 人），用户通过网页自助注册（昵称 / 账号 / 密码）
- 注册/登录页（登录后简单汇总个人自动签到执行情况）
- 管理页可查看各账号签到状态与历史
- 签到失败时通过 Server 酱推送告警

### 1.2 非目标（明确不做）

- **不做补签自动化**（status=-4 的补签流程需导师确认，人工处理）

* 不做移动端 App

---

## 2. 目标系统逆向分析（已确认事实）

> 以下来自对登录页、考勤页前端代码及登录抓包的实际分析，为设计基础。

### 2.1 系统构成

| 系统          | 地址                          | 角色              |
| ----------- | --------------------------- | --------------- |
| 统一登录平台（SSO） | `http://my.gzsums.net:1118` | 账号密码认证，签发 Token |
| 实习考勤系统      | `http://my.gzsums.net:1198` | 考勤列表与签到接口       |

### 2.2 登录链路（3 步）

```
① GET  1118/                       → 获得 ASP.NET_SessionId（匿名会话）
② POST 1118/Home/SubmitVerify      → 表单字段：
       Account   = 工号（明文）
       Password  = RSA(密码)（见 2.3）
       authCode  = 任意 4 位数字（见 2.4）
       imgCode   = 空
       ClientUrl = Http://my.gzsums.net:1198
     → 认证通过后在响应中携带 SSO Token（GUID）
③ POST 1198/  (body: Token=<guid>) → 换取考勤系统会话
     → 获得 SSTokenCookie=TokenKey=<guid>
```

会话有效期仅数分钟 → **每次签到都完整执行登录链路**，不做会话保活。

### 2.3 密码加密：RSA-1024 / PKCS#1 v1.5

前端 `JsEncryptHelper.js` 使用 JSEncrypt，公钥硬编码：

```
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCC0hrRIjb3noDWNtbDpANbjt5Iwu2NFeDwU16Ec87ToqeoIm2KI+cOs81JP9aTDk/jkAlU97mN8wZkEMDr5utAZtMVht7GLX33Wx9XjqxUsDfsGkqNL8dXJklWDu9Zh80Ui2Ug+340d5dZtKtd+nv09QZqGjdnSp9PTfFDBY133QIDAQAB
```

Python 复现（pycryptodome）：

```python
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
import base64

def encrypt_password(plain: str) -> str:
    key = RSA.import_key(base64.b64decode(PUBLIC_KEY_B64))
    cipher = PKCS1_v1_5.new(key)
    return base64.b64encode(cipher.encrypt(plain.encode())).decode()
```

PKCS#1 v1.5 加密结果每次随机，与浏览器行为一致，无需额外处理。

### 2.4 验证码：纯前端生成，无需 OCR

登录页验证码由页面内 `GVerify` 类在 **Canvas 前端生成**，校验逻辑（`authCode != imgCode`）也在前端完成，验证码内容从未发送至服务器，服务器无从校验。

**结论：提交任意 4 位数字即可。ddddocr 方案废弃，项目无 OCR 依赖。**

（遗留风险：医院未来若改为服务端验证码，登录会失败并触发告警，届时再评估 OCR。）

### 2.4.1 SSO 弱密码拦截（实测发现）

SSO 对**弱密码拒绝认证**：响应为登录页 HTML，内嵌 `var msg = '包含弱密码片段：xx密码不符合规则，请修改后再登录'`。此类账号在浏览器同样无法登录，必须先在统一登录平台改密码。客户端从 `var msg` 提取可读失败原因写入日志与告警。

### 2.5 考勤接口（1198，需 SSTokenCookie）

**拉取考勤列表**

```
GET /ExOrg/GetStuAndTeacher?startTime=YYYY-MM-DD&endTime=YYYY-MM-DD&stuId=<工号>
```

返回数组，每行字段（关键）：

| 字段             | 含义                  |
| -------------- | ------------------- |
| `ID`           | 记录 ID（签到接口入参）       |
| `WeekDate`     | 日期，如 `2026-09-11`   |
| `TimeName`     | `上午` / `下午`         |
| `SignInStatus` | 签到状态码（见下表）          |
| `DayOff`       | `0`=正常可签到；`>0`=补签场景 |
| `SignInTime`   | 签到时间                |

**执行签到**

```
POST /ExOrg/SignStuCate
Content-Type: application/json

{"id": <记录ID>, "status": 1}
```

返回 `{"Success": "1", ...}` 为成功，否则 `Info` 字段含失败原因。

**SignInStatus 状态码表**（来自页面渲染逻辑）：

| 值        | 含义                                 |
| -------- | ---------------------------------- |
| null / 0 | 未签到（DayOff=0 时可正常签到；DayOff>0 时为补签） |
| **1**    | **已签到，待确认**                        |
| 10       | 已确认                                |
| -1       | 借假                                 |
| -2       | 早退                                 |
| -3       | 迟到                                 |
| -4       | 补签待确认                              |
| -5       | 缺岗                                 |
| -6       | 擅自离岗                               |
| -10      | 旷实习                                |

### 2.6 签到时间窗与系统提醒

- 可签到窗口：**上午 5:00–8:00，下午 13:00–14:30**
- 系统行为：**7:00 / 14:00 未签到者会收到系统提醒**（已签不提醒）——这是本项目免费的兜底告警

---

## 3. 总体架构

**FastAPI 单体应用，单容器部署**，一个进程内包含全部组件：

```
┌────────────────────── Docker 容器 ──────────────────────┐
│                                                          │
│   FastAPI (uvicorn)                                      │
│   ├── Web 层（Jinja2 服务端渲染）                         │
│   │     ├── /register        用户注册页（邀请码）         │
│   │     ├── /admin/login     管理员登录                   │
│   │     └── /admin           管理/状态页                  │
│   ├── 调度层  APScheduler (AsyncIOScheduler)             │
│   │     ├── cron 0 5 * * *  上午签到（+0–300s 随机抖动）  │
│   │     └── cron 0 13 * * * 下午签到（+0–300s 随机抖动）  │
│   ├── 核心层                                              │
│   │     ├── HospitalClient   登录链路 + 考勤接口 (httpx)  │
│   │     ├── crypto           RSA 密码加密 (pycryptodome)  │
│   │     ├── signer           签到编排 + 重试              │
│   │     └── notify           Server 酱推送                │
│   └── 数据层  SQLite（数据卷挂载）                         │
│            ├── users         注册账号                     │
│            ├── invite_codes  邀请码（哈希）               │
│            └── sign_logs     签到日志                     │
└──────────────────────────────────────────────────────────┘
```

**选型理由**：2 个页面 + 1 个定时任务 + 3 张表，前后端分离的复杂度不成比例；服务端渲染零构建链，一个 Dockerfile 上线。

---

## 4. 核心流程设计

### 4.1 定时签到主流程（每个账号、每个时段）

```
触发（cron 5:00 / 13:00 + 0–300s 随机抖动，时区 Asia/Shanghai）
  │
  ├─ 1. 登录：执行 2.2 节三步链路，拿到 SSTokenCookie
  │      失败 → 进入重试（4.2）
  │
  ├─ 2. 拉取今日考勤列表（startTime=endTime=今天）
  │
  ├─ 3. 定位目标行：WeekDate==今天 且 TimeName==当前时段
  │      ├─ 无此行（未排班/休假）→ 记日志 "无需签到"，不告警
  │      ├─ SignInStatus ∈ {1, 10} → 已签过，记日志，结束
  │      ├─ SignInStatus ∈ {-1, -3, ...} 或 DayOff>0
  │      │    → 非自动签到范围，记日志并推送提示（人工处理）
  │      └─ SignInStatus ∈ {null, 0} 且 DayOff==0 → 执行签到
  │
  └─ 4. POST SignStuCate {"id": ID, "status": 1}
         ├─ Success==1 → 记日志成功，结束（静默）
         └─ 否则 → 记日志失败原因，进入重试（4.2）
```

### 4.2 失败重试策略

| 时段 | 主触发   | 重试              | 停止时间              |
| -- | ----- | --------------- | ----------------- |
| 上午 | 5:00  | 每 5 分钟，不限次数 | 8:00（打满全场，窗口关闭即停）  |
| 下午 | 13:00 | 每 5 分钟，不限次数 | 14:30（打满全场，窗口关闭即停） |

- 重试**整体重跑主流程**（会话分钟级过期，每次必须重新登录）
- 不限次重试、由停止时间截断（v0.2.2 起）：代理时代失败是分钟级整段坏相，
  次数预算再大也可能被单个坏相吞掉，拉锯覆盖面只能靠时间窗口保证
- 同一时段同一账号：成功即停止；超过停止时间仍失败 → 推送最终告警
- 注意：拉锯期间用户可能收到 7:00 / 14:00 系统提醒——无害，属预期现象

### 4.3 并发

账号数 < 10，顺序执行即可（单账号全链路 < 5 秒）；无需并发框架，但各账号间**失败隔离**：任一账号异常不影响后续账号。

---

## 5. 数据模型（SQLite）

### 5.1 users

| 字段         | 类型          | 说明                     |
| ---------- | ----------- | ---------------------- |
| id         | INTEGER PK  |                        |
| nickname   | TEXT        | 昵称（用于通知与展示）            |
| account    | TEXT UNIQUE | 工号                     |
| password   | TEXT        | **明文**（见 8.2 风险声明）     |
| sendkey    | TEXT NULL   | 可选：个人 Server 酱 SendKey |
| enabled    | INTEGER     | 1=启用，0=停用              |
| created_at | TEXT        |                        |

### 5.2 invite_codes

| 字段        | 类型           | 说明                     |
| --------- | ------------ | ---------------------- |
| id        | INTEGER PK   |                        |
| code      | TEXT NULL    | 邀请码明文（v0.3 起存储，仅管理页可见） |
| code_hash | TEXT UNIQUE  | 邀请码的 SHA-256（注册校验用）    |
| used_by   | INTEGER NULL | 使用者 user id            |
| used_at   | TEXT NULL    |                        |

部署时预生成或管理页按需生成，**单次使用**，用后标记；被使用的邀请码作为"注册邀请码"列并入账号总览。

### 5.3 sign_logs

| 字段         | 类型         | 说明                                               |
| ---------- | ---------- | ------------------------------------------------ |
| id         | INTEGER PK |                                                  |
| user_id    | INTEGER FK |                                                  |
| date       | TEXT       | 签到日期                                             |
| period     | TEXT       | `am` / `pm`                                      |
| result     | TEXT       | `success` / `failed` / `skipped` / `no_schedule` |
| message    | TEXT       | 服务端返回或异常摘要                                       |
| created_at | TEXT       |                                                  |

管理页展示与排障的依据；定期清理（保留 90 天）。

---

## 6. Web 前端设计（Jinja2 服务端渲染）

### 6.1 路由

| 路由                         | 方法       | 鉴权  | 功能                                         |
| -------------------------- | -------- | --- | ------------------------------------------ |
| `/`                        | GET      | —   | 已登录跳 `/me`，未登录跳 `/login`                   |
| `/register`                | GET/POST | 邀请码 | 用户注册：昵称、账号、密码、（可选）个人 SendKey；**先经医院 SSO 真实认证，通过才入库**；成功后自动登录并跳 `/me` |
| `/login`                   | GET/POST | —   | 用户登录（工号 + 医院密码 → session；IP 限流，见 8.1）      |
| `/change-password`         | POST     | —   | 更正密码（无需登录，凭医院 SSO 认证新密码后更新入库）          |
| `/logout`                  | POST     | 用户  | 退出登录                                       |
| `/me`                      | GET      | 用户  | 个人页：启用状态、下次执行时间、近 7 天签到记录                 |
| `/me/sendkey`              | POST     | 用户  | 修改个人 Server 酱 SendKey                      |
| `/me/sign-now`             | POST     | 用户  | 手动触发本人当前时段签到（同 `/admin/sign-now` 语义）       |
| `/admin/login`             | GET/POST | —   | 管理员登录（密码 → session）                        |
| `/admin`                   | GET      | 管理员 | 账号总览（含注册邀请码列）+ 近 7 日签到状态 + 未使用邀请码     |
| `/admin/users/{id}/toggle` | POST     | 管理员 | 启用/停用某账号                                   |
| `/admin/users/{id}`        | DELETE   | 管理员 | 删除账号                                       |
| `/admin/invites`           | GET      | 管理员 | 查看剩余邀请码数量                                |
| `/admin/invites/generate`  | POST     | 管理员 | 生成 1 个邀请码，未使用明文常驻管理页可见                  |
| `/admin/invites/{id}/delete` | POST   | 管理员 | 删除未使用的邀请码（已使用的保留审计，不可删）                |
| `/admin/check-one/{id}`    | POST     | 管理员 | 签到状态检测（单账号）：登录医院平台刷新当日真实状态（写 `checked` 日志，徽标直显真实状态，不签到不推送）；管理页前端逐个串行调用、账号间休息 3 秒、每完成一个立即刷新该行 |
| `/admin/settings/sendkey`  | POST     | 管理员 | 页面配置管理员 SendKey（入库覆盖 .env；留空清除）       |
| `/admin/settings/password` | POST     | 管理员 | 修改管理员密码（验原密码，立即生效无需重启）                 |
| `/admin/sign-now/{id}`     | POST     | 管理员 | 手动触发某账号当前时段签到（冒烟测试/补漏用；窗口外会收到服务端失败返回，原样展示） |

### 6.2 页面草图

**登录页**：Logo + 项目名 + 工号/密码两字段 + 登录按钮 + "有邀请码？去注册"链接。

**注册页**：昵称 / 工号 / 密码 / 邀请码 / 可选 SendKey；注册成功即写 session 并跳转 `/me`。

**个人页（/me）**：顶部昵称 + 退出按钮；状态卡片（启用状态、下次执行时间 = 下一个 5:00 或 13:00）；"立即签到"按钮；SendKey 修改表单；近 7 天签到记录表（日期 / 时段 / 结果 / 信息）。**任何页面都不展示密码，SendKey 掩码展示。**

**管理页**：账号总览表（昵称 / 账号掩码 / 注册邀请码 / 启用状态 / 今日 am、pm 结果）+ "签到状态检测"按钮；下方未使用邀请码区（明文常驻、生成一个、删除）。不做花哨样式，Bootstrap 够用。

### 6.3 Session 与登录

- 使用 Starlette `SessionMiddleware`（签名 cookie），密钥来自环境变量 `SESSION_SECRET`
- session 内容二选一、互斥：`{"user_id": N}`（用户）或 `{"admin": true}`（管理员）
- 有效期 7 天滑动续期；`/logout` 与管理员登出均清除 session
- 用户登录校验：工号存在且 `enabled==1` 且密码与 `users.password` 明文一致（Web 登录凭据即医院账号密码）

---

## 7. 调度设计

- **APScheduler（AsyncIOScheduler）** 随 FastAPI 进程启动，时区 `Asia/Shanghai`
- 两个 cron 触发器：`0 5 * * *`、`0 13 * * *` + 0–300s 随机抖动（每天，含周末）
- 重试不用 APScheduler 动态加任务，由 signer 内部循环 + `asyncio.sleep(300)` 实现，逻辑集中
- 容器内显式设置 `TZ=Asia/Shanghai`，避免宿主机时区坑
- 进程重启后任务自动恢复（无持久化任务状态的需求——错过即错过，重试窗口内会自然覆盖）

---

## 8. 安全设计

### 8.1 已采纳措施

| 项                | 方案                          |
| ---------------- | --------------------------- |
| 管理员密码            | 环境变量注入，数据库/配置中只存 SHA-256 哈希 |
| 邀请码              | 单次使用；明文 + SHA-256 双写存储，明文仅管理页可见   |
| 管理页              | session 鉴权，未登录一律跳转          |
| 账号展示             | 管理页账号打掩码（如前 2 后 2 位）        |
| 数据库文件            | 权限 600，容器内非 root 运行         |
| Server 酱 SendKey | 环境变量（管理员）+ 用户可选字段（推送用，不展示）  |
| Web session     | 签名 cookie（SessionMiddleware），secret 由环境变量注入 |
| 登录防爆破           | `/login` 按 IP 限流：1 分钟内失败 5 次锁 10 分钟，进程内存计数 |

### 8.2 已知并明示接受的风险

- **用户密码明文存储**：此风险已与项目负责人确认并决定接受。
- 目标系统为 HTTP 明文协议，凭证与签到请求在链路上不加密——这是医院系统的现状，本项目无法改变。

### 8.3 合规与边界（README 必载）

- 本工具仅自动化"本人按时到岗后的例行点击"，**不支持也不应用于虚构出勤**
- 使用者需自行确认所在单位对自动化签到的管理规定

---

## 9. 通知设计（Server 酱）

> 免费版推送列表只露标题 → **状态前置进标题**，正文从简。

| 事件              | 标题示例 | 推送对象                         |
| --------------- | ---- | ---------------------------- |
| 签到成功            | `✅签到成功｜昵称｜09-12 上午` | 用户本人（填了个人 SendKey 才推；管理员不推）  |
| 签到失败（重试耗尽）      | `❌签到失败｜昵称｜09-12 上午` | 管理员；若用户填了个人 SendKey 则同时推本人   |
| 状态异常需人工（迟到/补签等） | `⚠️需人工处理｜昵称｜09-12 上午` | 同上                           |
| 登录失败（密码错误/账号锁定） | `🔒登录失败｜昵称｜09-12 上午` | 同上，首次失败即推（不等重试耗尽，因此类失败重试无意义） |
| 手动触发签到（后台/个人页）  | `📢手动签到｜昵称｜09-12 下午｜已签过` | 用户本人（无个人 SendKey 时回落管理员），**无论结果如何都推**——兼作推送链路连通性探针 |
| 连续同类失败          | — | 去重：同一时段同一账号只推首次与最终两次         |

`notify(title, msg, sendkey=None)` 单接口封装，Server 酱 API 一个 POST 完事；sendkey 为空自动回落管理员 SendKey。

---

## 10. 冒烟测试方案（本地脚本）

> 独立于主项目的 `scripts/smoke.py`，用真实账号按阶段验证，全部通过后才部署。

| 阶段 | 内容           | 通过标准                                              |
| -- | ------------ | ------------------------------------------------- |
| S0 | RSA 加密单测     | 密文 base64 解码后长度 = 128 字节（RSA-1024），每次结果不同         |
| S1 | 登录链路（窗口外即可测） | 三步链路走通，拿到 SSTokenCookie                           |
| S2 | 拉取考勤列表       | 返回包含本人当日记录，字段完整                                   |
| S3 | 目标行定位逻辑      | 正确输出"今日应签/已签/无需签到"的判定（不实际提交）                      |
| S4 | 真实签到         | 仅可在窗口内验证：部署前挑一个窗口手动跑 `sign-now` 路径，或上线后首个 5:00 观察 |
| S5 | 通知链路         | Server 酱测试消息送达                                    |
| S6 | 注册闭环         | 邀请码注册 → 入库 → 邀请码失效                                |

S1–S3 在窗口外可随时跑，是日常回归手段（医院改版时第一时间暴露）。

---

## 11. 项目结构

```
lazy-clerk/
├── app/
│   ├── main.py              # FastAPI 入口，挂载路由与调度器
│   ├── config.py            # 环境变量读取
│   ├── db.py                # SQLite 初始化与访问
│   ├── models.py            # users / invite_codes / sign_logs
│   ├── core/
│   │   ├── client.py        # HospitalClient：登录链路 + 考勤接口
│   │   ├── crypto.py        # RSA 密码加密
│   │   ├── signer.py        # 签到编排 + 重试 + 状态判定
│   │   ├── scheduler.py     # APScheduler 两个 cron 任务
│   │   └── notify.py        # Server 酱
│   ├── routes/
│   │   ├── register.py      # 注册 + 邀请码校验
│   │   ├── user.py          # /login /logout /me /me/sendkey /me/sign-now
│   │   └── admin.py
│   ├── templates/           # login.html / register.html / me.html / admin.html
│   └── static/
├── scripts/
│   ├── smoke.py             # 冒烟测试
│   └── gen_invites.py       # 预生成 50 个邀请码
├── tests/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── README.md                # 含 8.2/8.3 风险声明
└── LICENSE                  # MIT
```

### 11.1 部署配置（.env.example）

```bash
ADMIN_PASSWORD=            # 管理页密码（首次启动哈希入库，此后仅校验）
SERVERCHAN_SENDKEY=        # 管理员的 Server 酱 SendKey
SESSION_SECRET=            # session cookie 签名密钥（随机长字符串）
TZ=Asia/Shanghai
```

`docker-compose.yml`：单服务，端口映射（如 `127.0.0.1:8787:8000`，前置反代任选），volume 挂载 `./data:/app/data`。

---

## 12. 开发路线图

| 里程碑 | 内容                                              | 依赖       |
| --- | ----------------------------------------------- | -------- |
| M1  | `crypto.py` + `client.py` + `smoke.py` S0–S3 通过 | 无（纯核心链路） |
| M2  | `signer.py` + `notify.py`，窗口内 S4 验证             | M1       |
| M3  | Web 层（注册/登录/个人页/管理页）+ 邀请码 + 数据层                  | M1       |
| M4  | Dockerfile / compose / README / LICENSE，部署上线    | M2、M3    |
| M5  | 观察期：连续 3 天全账号成功后，邀请同事注册                         | M4       |

---

## 13. 风险登记

| 风险            | 等级         | 缓解                                 |
| ------------- | ---------- | ---------------------------------- |
| 医院改服务端验证码     | 中          | 登录失败即告警；届时评估 OCR 或人工介入             |
| 医院改版导致接口/字段变动 | 中          | 冒烟脚本 S1–S3 日常回归；失败告警含原始响应摘要        |
| 密码明文泄露        | **高（已接受）** | 见 8.2；DB 文件 600 权限 + 服务器加固         |
| 服务器宕机/断网错过窗口  | 中          | 7:00/14:00 系统提醒兜底；管理员收到最终失败告警后可手动签 |
| 时钟漂移          | 低          | 容器内 NTP 同步；重试窗口提供 30 分钟容错          |
| 账号被风控/锁定      | 低          | 登录行为与浏览器一致（仅 2 次/天，远低于异常阈值）        |

---

## 14. 个人版

> 背景：2026-09-12 医院 SSO 封禁服务器 IP（排障史见 `server/mihomo/KNOWN_ISSUES.md`），
> 根因"单 IP 代理多账号"命中风控。个人版让每人从自己电脑（天然正常用户 IP）发起签到，根治此问题。

### 14.1 形态与原则

- **PyInstaller 单 exe**（约 38MB），Windows 10/11 双击即用，用户零依赖
- **剔除**：多账号、Web 界面、管理端、邀请码、SQLite、Docker——全部不要
- **保留三核心**：查询（status）、签到（sign）、推送（Server 酱）
- **无数据库**：配置 `config.json`、日志 `sign.log`，均在 exe 同目录
- **复用 `app/core`**：client / crypto / signer / notify 三版单点维护，医院改版只改一处

### 14.2 命令集

| 命令 | 作用 |
| --- | --- |
| `lazy-clerk.exe setup` | 配置向导：先经医院 SSO 真实认证，通过才写 config.json |
| `lazy-clerk.exe sign` | 当前时段签到（复用 signer 重试逻辑，5 分钟±随机间隔打满全场至窗口关闭） |
| `lazy-clerk.exe status` | 查询当日考勤状态（时段/状态/签到时间/科室） |
| `lazy-clerk.exe install` | 注册 Windows 计划任务：时间取 config.json（默认 5:00 / 13:00） |
| `lazy-clerk.exe uninstall` | 删除计划任务 |

### 14.3 关键决策

- **调度外包给 Windows 计划任务**（schtasks），exe 每次跑完即退出，无常驻进程
- **触发时间用户自定义**：家庭正常 IP 无风控压力，无需拟人化随机；开启/修改时校验
  HH:MM 格式且必须落在签到窗口内（默认 5:00 / 13:00），存 config.json
- **不补签**：电脑关机/睡眠错过即错过，医院 7:00/14:00 系统提醒兜底；用户须保持签到时段开机
- **推送**：成功/失败/需人工都推本人 SendKey（标题前置状态）；无 SendKey 则静默
- **凭据**：config.json 明文存本机（用户自担，与服务器版 8.2 同一风险逻辑，但暴露面只剩本机）

### 14.4 与其他形态的关系

- 三种形态平行维护，共享 `app/core`：医院系统改版只改 `app/core/client.py`，三版同时生效
- 公网版（`server/`）：Web 层 + APScheduler + Docker，代理逃生通道已成熟（2026-09-17 定稿，架构决策见 `server/mihomo/KNOWN_ISSUES.md`）
- 个人版（`personal/`）：`app/core` 的改动同时惠及三版

---

# 15. 宿舍版（2026-09-13 新增，dorm/）

### 15.1 定位

一台长期开机的 Windows 电脑服务全宿舍 4–6 人。介于公网版与个人版之间：
不要公网版的风控暴露，也不要每人整夜开电脑。局域网信任环境，无邀请码。

### 15.2 架构：双进程解耦

- **签到执行器**：Windows 计划任务 5:00/13:00 拉起 `lazy-clerk-dorm.exe sign am|pm`，
  进程内随机 sleep 0–300s（即 5:00–5:05 / 13:00–13:05，见 2.6 签到时间窗与系统提醒），跑完即退；
  顺带清理 90 天旧日志。无常驻进程，Web 崩溃不影响签到
- **管理 Web**：仅管理员，127.0.0.1:8787，随用随开（双击 exe 或 start-web.bat），
  首次运行命令行引导播种管理员密码与 SendKey（此后管理页修改）
- **共享 SQLite**：WAL + busy_timeout，签到进程与 Web 进程并发写不互锁

### 15.3 与个人版/公网版的差异

- 相比个人版：多账号 + 浏览器管理页 + SQLite；单 exe 子命令（web/sign/sign-now/install/uninstall）
- 相比公网版：无用户侧（注册页/登录页/me 页/邀请码全砍），无 APScheduler，
  无 Docker；账号管理（增删改、SendKey）全部管理员代录，新增/改密码先过医院 SSO 验证
- 计划任务助手 `app/core/wintasks.py` 为个人版/宿舍版共用（唤醒、补跑、锁屏运行）

### 15.4 三分支一致性约定

**必须一致（改 `app/core` 即三版同步）**：登录链路、考勤接口、签到判定、重试策略、
推送文案与回退、状态码映射。医院系统改版只改 `app/core/client.py`。

**有意不同（勿"对齐"）**：

- 触发时间：公网/宿舍 5:00–5:05、13:00–13:05 随机（拟人化防风控）；个人版用户自定义
  （默认 5:00 / 13:00 准点，校验须落在签到窗口内；家庭正常 IP 无风控压力）
- 推送回退：公网/宿舍有管理员兜底（`notify` 默认解析器走 DB/.env）；
  个人版无数据库，启动时 `set_admin_key_resolver(None)` 关闭兜底
- 手动签到：Web（公网/宿舍）单次尝试走 `signer.sign_user_manual`（HTTP 不能挂起）；
  个人版菜单走 `sign_user_with_retry`（交互等得起）
- 日志清理：公网 APScheduler 每日清理；宿舍签到任务顺带清理；个人版 sign.log 超 512KB 截断
- 管理页徽标渲染共用 `app/routes/common.py` 的 `make_badge`，勿在分支里另抄

### 15.5 目录重构（v0.5，2026-09-13）

公网版专属代码移入 `server/`（main/scheduler/routes/templates/scripts/Dockerfile/
compose/requirements/.env.example + 部署包），`app/` 收敛为三版共享层（core/models/
db/config + routes/common.py + 共享模板 base/admin_login）。第 11 章项目结构已过时，
以根 README 仓库结构为准。静态资源目录 app/static 无引用已删除。
