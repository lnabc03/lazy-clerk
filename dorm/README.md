# lazy-clerk 宿舍版

一台长期开机的 Windows 电脑，服务全宿舍 4–6 人的自动签到。

## 特性

- **签到由 Windows 计划任务驱动**：每天 6:53–6:58、13:53–13:58 之间的随机时刻
  自动签到全部账号（账号间随机间隔 10–40 秒），跑完即退，无常驻进程
- **锁屏不影响签到**；电脑插电时睡眠会自动唤醒执行；错过的任务开机后补跑
- **管理页随用随开**：双击 exe 拉起浏览器管理页（仅本机 127.0.0.1），用完关窗口即可，
  崩溃或关闭都不影响签到
- 账号管理全在管理页：新增/停用/删除、改密码、录 SendKey，新增和改密码都先过医院平台验证
- 每个舍友填自己的 SendKey，签到成功、失败、需人工处理都微信推送本人
- 日志面板：全部账号的签到记录，精确到秒

## 安装（管理员操作）

1. 把 `lazy-clerk-dorm.exe` 放进一个固定目录，比如 `D:\tools\lazy-clerk-dorm\`
2. 双击 exe：首次运行引导设置管理员密码和 SendKey，并询问是否开启自动签到
3. 在管理页顶部确认"自动签到：已开启"；未开启时在终端执行 `lazy-clerk-dorm.exe install`
4. 在管理页逐个添加舍友账号：填昵称、工号、密码，添加前会先向医院平台验证

## 使用

管理页（双击 exe 打开，或终端 `lazy-clerk-dorm.exe web`）：

- 账号总览：今日上下午签到状态徽标
- 签到状态检测：登录医院平台刷新所有账号的当日真实状态
- 单账号"签到"按钮：立即签到（补救用，任何结果都推微信）
- 管理员设置：管理员 SendKey、改管理员密码

终端命令：`web` / `sign am|pm`（带随机延迟，计划任务用）/ `sign-now`（立即签到测试）/
`install` / `uninstall`。

## 注意事项

- 首次运行若被 Windows SmartScreen 或杀毒软件拦截，属 PyInstaller 单文件 exe 的
  常见误报，选择"仍要运行"即可
- 电脑需长期开机；锁屏无碍，插电时睡眠会被任务唤醒，关机则错过
  （医院 7:00 / 14:00 系统提醒兜底）
- 管理页只监听 127.0.0.1，仅本机可访问；校园网 AP 隔离下同宿舍设备也访问不到，
  管理操作都在这台电脑上完成
- **一台电脑只装一种形态**：计划任务与个人版同名，同时安装会被冲突检测拦下
- 舍友密码变更后找管理员在管理页"改密码"更新

## 风险

- `data/lazy-clerk.db` 含全部舍友的医院平台密码明文，谁物理接触这台电脑谁就能读到——
  部署即表示全宿舍知情并接受；不要复制该文件到别处
- 目标系统为 HTTP 明文协议——医院系统现状，本工具无法改变
- 仅自动化"本人按时到岗后的例行点击"，不支持也不应用于虚构出勤

## 卸载

`lazy-clerk-dorm.exe uninstall` 后删除整个目录即可，没有其他残留。

---

## 开发者：打包 exe

```bash
pip install pyinstaller tzdata
pyinstaller --onefile --console --name lazy-clerk-dorm --collect-data tzdata \
    --add-data "app/templates;app/templates" --add-data "dorm/templates;dorm/templates" dorm/main.py
# 产物在 dist/lazy-clerk-dorm.exe
```

源码调试：`dorm/start-web.bat` 拉起管理页，`python dorm/main.py sign-now` 立即签到测试。
