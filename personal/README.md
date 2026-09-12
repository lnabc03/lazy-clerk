# lazy-clerk 个人版

实习考勤自动签到。每天 6:58 和 13:58 自动完成签到，结果推送到你的微信。

## 你需要准备

- 一台 Windows 电脑，签到时段（上午 5:00–8:00、下午 13:00–14:30）保持开机
- 电脑睡眠或关机时无法签到，错过时段医院会在 7:00 和 14:00 提醒你

## 安装

1. 把 `lazy-clerk.exe` 放进一个固定目录，比如 `D:\tools\lazy-clerk\`
2. 双击打开，第一次使用会引导你配置工号和密码，配置前会先向医院平台验证一次
3. 配置完成后按提示开启自动签到，就全部完成了

## 想要微信推送

1. 微信扫码登录 [sct.ftqq.com](https://sct.ftqq.com/login/)
2. 复制你的 SendKey
3. 重新打开 `lazy-clerk.exe`，选择"更改配置"填入即可

签到成功、失败、需要人工处理时都会推送，标题直接写明结果。

## 日常使用

双击 `lazy-clerk.exe` 就能看到今日考勤状态，并通过菜单操作：

- 立即签到
- 刷新状态
- 更改配置
- 开启或关闭自动签到

日志在同目录的 `sign.log`，配置在 `config.json`。`config.json` 里有你的密码，不要发给别人。

## 换了医院平台密码

重新打开程序，选择"更改配置"填入新密码。收到"🔒登录失败"推送时通常就是密码失效了。

## 卸载

菜单里关闭自动签到，然后删除整个目录即可，没有其他残留。

---

## 开发者：打包 exe

```bash
pip install pyinstaller tzdata
pyinstaller --onefile --console --name lazy-clerk --collect-data tzdata personal/main.py
# 产物在 dist/lazy-clerk.exe（约 38MB），连同本 README 分发给同事即可
```
