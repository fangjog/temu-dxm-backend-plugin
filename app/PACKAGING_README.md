# Temu / 店小秘后台自动化运行包

版本：`0.1.0`

这个包用于从店小秘后台已采集产品页开始，按页面从上到下处理指定数量产品。每个产品执行两轮发布，流程结束后输出 Excel。

## 目录说明

- `browser/`：内置浏览器占位目录。当前包没有携带真实 Chrome，可放入便携 Chrome，或使用本机已安装 Chrome。
- `app/`：自动化程序、配置页面、启动脚本和运行依赖。
- `app/data/reports/`：Excel / JSON 结果报告。
- `app/data/logs/`：运行日志。
- `app/data/debug/`：OCR 和字段补全调试信息。

## 安装依赖

在 `app` 目录运行：

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

如果使用本机 Chrome/CDP 或便携 Chrome，Playwright 的 Chromium 可不作为默认浏览器使用，但 Playwright Python 包仍然需要。

## 启动

双击或运行：

```powershell
powershell -ExecutionPolicy Bypass -File run_plugin.ps1
```

或：

```bat
run_plugin.bat
```

启动后浏览器打开或手动访问：

```text
http://127.0.0.1:8765
```

页面输入：

- 店小秘账号
- 店小秘密码
- 编辑数量，默认 `4`
- EasyRouter API Key

API Key 和密码保存时使用 Windows DPAPI 加密，不写入日志，不明文显示。

## 命令行运行

```powershell
python plugin_main.py --count 4 --api-key "your_easyrouter_key"
```

更推荐用网页输入 API Key，避免命令行历史记录保存敏感信息。

## 浏览器说明

默认会尝试：

1. `browser/chrome/chrome.exe`
2. `browser/GoogleChromePortable/App/Chrome-bin/chrome.exe`
3. 系统安装的 Google Chrome

自动化会使用独立用户数据目录：

```text
browser/user_data/Profile 13
```

第一次运行如果需要登录店小秘，请在打开的浏览器中完成登录后再继续运行。

## 更新功能

`update_config.json` 当前结构：

```json
{
  "version": "0.1.0",
  "update_url": "",
  "notes": ""
}
```

如果 `update_url` 为空，页面会提示“当前未配置更新源”。后续可以把新版本包地址写入 `update_url`，再接入自动同步更新。

## 安全说明

本包不包含：

- 真实 `.env`
- 真实 EasyRouter API Key
- 店小秘账号密码
- 浏览器登录态/Profile
- 旧截图、旧日志、旧报告
- `.git`、`__pycache__`、大文件缓存
