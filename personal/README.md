# lazy-clerk 个人版

实习考勤自动签到 · 个人电脑单机版。零服务器、零数据库，每天 6:58 / 13:58 自动完成签到，结果推送到你的微信。

> 本版本为项目主线。服务器多账号版（`app/` + `docker-compose.yml`）因医院 SSO 封禁服务器 IP 已冻结归档，详见 `docs/lazy-clerk-SSO故障报告与解决方案.md`。

## 使用前提

- Windows 10/11 电脑
- **电脑在签到时段（上午 5:00–8:00、下午 13:00–14:30）需处于开机状态**
  - 睡眠/关机时无法签到；错过时段时医院系统会在 7:00 / 14:00 给你发未签到提醒，作为兜底
- 网络可访问医院平台（`my.gzsums.net`，办公网/家宽均可）

## 安装（3 步）

1. 把 `lazy-clerk.exe` 放到一个固定目录（如 `D:\tools\lazy-clerk\`），**不要放桌面或下载目录后随手清理**
2. 双击运行，或在终端执行首次配置：

   ```
   lazy-clerk.exe setup
   ```

   按提示输入昵称、工号、统一登录平台密码、Server 酱 SendKey（可留空）。
   程序会先向医院平台**真实验证一次账号密码**，通过才会保存（写入同目录 `config.json`）。

3. 注册每日自动签到：

   ```
   lazy-clerk.exe install
   ```

   显示两条"成功"即完成。之后每天 6:58 / 13:58 自动签到，失败自动每 5 分钟重试。

## 获取微信推送（可选但推荐）

1. 微信扫码登录 [sct.ftqq.com](https://sct.ftqq.com/login/)
2. 复制你的 SendKey，重新运行 `lazy-clerk.exe setup` 填入（或直接编辑 `config.json`）
3. 推送规则：签到成功、失败、需人工处理都会推送；标题直接写状态，列表页一眼可读

## 常用命令

| 命令 | 作用 |
|---|---|
| `lazy-clerk.exe setup` | 首次配置 / 修改账号密码（重跑即覆盖） |
| `lazy-clerk.exe status` | 查询今日考勤状态 |
| `lazy-clerk.exe sign` | 立即执行当前时段签到 |
| `lazy-clerk.exe uninstall` | 删除计划任务（停用自动签到） |

日志在 exe 同目录 `sign.log`，配置在 `config.json`（含密码明文，请勿分享该文件）。

## 换电脑 / 卸载

`lazy-clerk.exe uninstall` 后删除整个目录即可，无其他残留。

## 密码改了怎么办

医院平台改密码后，重跑 `lazy-clerk.exe setup` 填入新密码即可（会先验证再保存）。
若收到"🔒登录失败"推送，多半就是密码失效了。

---

## 开发者：打包 exe

```bash
pip install pyinstaller tzdata
pyinstaller --onefile --console --name lazy-clerk --collect-data tzdata personal/main.py
# 产物在 dist/lazy-clerk.exe（约 38MB），连同本 README 分发给同事即可
```
