# Discord Bot 雲端主機部署

這份文件的目標是把 Discord bot 從本機常駐改成 Linux 雲端主機常駐，讓整套系統夜間不再依賴這台 Windows 電腦。

目前正式主機已切到 Oracle Cloud Ubuntu，systemd service 名稱是 `alpha-finder-discord-bot.service`。

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

## 日常更新 bot

當你修改 bot 相關程式後，不需要重新手動 SSH 一輪。現在本機直接執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\redeploy_discord_bot.ps1
```

這個腳本會：

1. 以目前 repo 的 `HEAD` 重新打包部署內容
2. 上傳到 Oracle VM 的 `/opt/alpha-finder`
3. 視需要重跑 `pip install -r requirements.txt`
4. 重啟 `alpha-finder-discord-bot.service`
5. 回傳 `systemctl status` 與最新 bot log

注意：這裡的 `HEAD` 指的是目前已提交的 git 版本。如果你剛改完 bot 程式但還沒 commit，先 commit 再 redeploy，才會把那些更新送上 Oracle VM。

如果你同時有更新 `DISCORD_BOT_TOKEN`、Turso 連線或其他 bot 環境變數，就改用：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\redeploy_discord_bot.ps1 -SyncEnv
```

若只是純 Python 程式改動、`requirements.txt` 沒變，也可以加上 `-SkipPipInstall` 加快更新。

## 完成 cutover 後的本機清理

在確認雲端 bot 已經穩定上線後，再移除本機 Startup 自啟：

```powershell
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\AlphaFinder_Discord_TradeBot.cmd" -Force
```

如果本機還有手動開的 bot 視窗，也一併關掉。

另外，`setup.bat` 現在預設不會再重建本機 bot 自啟；除非你真的要做緊急備援，否則不需要把它打開。

## 什麼時候算真的完成

以下四件都成立，才算 Discord bot 完成上雲端：

1. `systemctl status alpha-finder-discord-bot` 顯示 running
2. Discord 內 `/positions`、`/trades`、`/executions` 可正常回應
3. 本機 Windows 關機後，bot 仍然在線
4. 本機 Startup 檔已刪除且不再需要本機自啟
