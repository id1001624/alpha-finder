# Discord Bot 雲端主機部署

這份文件的目標是把 Discord bot 從本機常駐改成 Linux 雲端主機常駐，讓整套系統夜間不再依賴這台 Windows 電腦。

## 建議主機

- Ubuntu 22.04 或 24.04 小型 VPS
- 只要能長時間常駐 outbound 連線到 Discord gateway 即可

## 伺服器目錄

- Repo 放在 `/opt/alpha-finder`
- systemd service 檔在 `deploy/systemd/alpha-finder-discord-bot.service`
- 環境變數檔建議放在 `/etc/alpha-finder/discord-bot.env`

## 部署步驟

1. 安裝 Python 3.12、git、venv
2. clone repo 到 `/opt/alpha-finder`
3. 建立 venv 並安裝依賴
4. 複製 `deploy/systemd/discord-bot.env.example` 到 `/etc/alpha-finder/discord-bot.env`
5. 填入 `DISCORD_BOT_TOKEN`、`TURSO_DATABASE_URL`、`TURSO_AUTH_TOKEN`
6. 建立 log 目錄 `/var/log/alpha-finder`
7. 複製 systemd service 檔到 `/etc/systemd/system/alpha-finder-discord-bot.service`
8. 執行 `sudo systemctl daemon-reload`
9. 執行 `sudo systemctl enable --now alpha-finder-discord-bot`
10. 用 `sudo systemctl status alpha-finder-discord-bot` 確認服務在線

## 完成 cutover 後的本機清理

在確認雲端 bot 已經穩定上線後，再移除本機 Startup 自啟：

```powershell
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\AlphaFinder_Discord_TradeBot.cmd" -Force
```

如果本機還有手動開的 bot 視窗，也一併關掉。

## 什麼時候算真的完成

以下四件都成立，才算 Discord bot 完成上雲端：

1. `systemctl status alpha-finder-discord-bot` 顯示 running
2. Discord 內 `/positions`、`/trades`、`/executions` 可正常回應
3. 本機 Windows 關機後，bot 仍然在線
4. 本機 Startup 檔已刪除且不再需要本機自啟