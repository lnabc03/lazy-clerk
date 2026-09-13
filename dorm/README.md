# lazy-clerk 宿舍版

一台长期开机的 Windows 电脑，服务全宿舍 4–6 人的自动签到。

- **签到由 Windows 计划任务驱动**：每天 6:53–6:58、13:53–13:58 之间的随机时刻自动签到全部账号，跑完即退，不依赖任何常驻程序。锁屏不影响；电脑插电时睡眠会自动唤醒。
- **管理页随用随开**：管理员双击 exe 打开浏览器管理页，用完直接关窗口，不影响签到。

## 部署（管理员操作）

1. 把 `lazy-clerk-dorm.exe` 放进一个固定目录，比如 `D:\tools\lazy-clerk-dorm\`
2. 双击 exe：首次运行引导设置管理员密码和 SendKey，随后自动打开管理页
3. 命令行执行一次 `lazy-clerk-dorm.exe install`，注册每天的自动签到任务
4. 在管理页逐个添加舍友账号：填昵称、工号、密码，添加前会先向医院平台验证

## 管理页功能

- 账号总览：今日上下午签到状态徽标
- 新增 / 停用 / 删除账号，改密码、录 SendKey（都先过医院平台验证）
- 签到状态检测：刷新所有账号的当日真实状态
- 单个账号立即签到（补救用，结果必推微信）
- 最近日志：全部账号的签到记录，精确到秒
- 管理员设置：管理员 SendKey、改管理员密码

## 数据与安全

- 全部数据在 exe 同目录的 `data/lazy-clerk.db`，含舍友密码明文，不要发给别人
- 管理页只监听 127.0.0.1，仅本机可访问
- 卸载：`lazy-clerk-dorm.exe uninstall` 后删除整个目录即可

## 开发者：打包 exe

```bash
pip install pyinstaller tzdata
pyinstaller --onefile --console --name lazy-clerk-dorm --collect-data tzdata \
    --add-data "app/templates;app/templates" --add-data "app/static;app/static" \
    --add-data "dorm/templates;dorm/templates" dorm/main.py
# 产物在 dist/lazy-clerk-dorm.exe
```

源码调试：`dorm/start-web.bat` 拉起管理页，`python dorm/main.py sign-now` 立即签到测试。
