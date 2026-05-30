# Temu DXM Backend Plugin

店小秘后台自动化运行包。当前版本：`0.1.0`。

## 功能

- 从店小秘 Temu 已采集产品页开始。
- 输入编辑数量，按列表从上到下处理。
- 每个源产品发布两轮。
- 运行结束后输出 Excel / JSON。
- 支持 EasyRouter API Key 脱敏输入与本机 DPAPI 保存。
- 支持 GitHub Releases 手动检查更新、立即更新、回退上一版本。

## 本地运行

```powershell
cd app
pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File run_plugin.ps1
```

打开页面：

```text
http://127.0.0.1:8765
```

## 发布下一版

1. 修改 `VERSION`，例如 `0.1.1`，并同步 `app/VERSION`。
2. 运行：
   ```powershell
   python scripts/package_backend_plugin.py
   ```
3. 如果已安装并登录 GitHub CLI：
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/publish_release.ps1
   ```
4. 用户在插件页面点击“检查更新 → 立即更新”。

当前先使用 GitHub Releases 作为更新源。后续客户量上来后，可以把 `latest.json` 里的 `package_url` 换成腾讯云 COS/CDN。
