# 本檔只是示例，不含秘密資料。
# 依你的環境複製成私人腳本後再執行，或直接使用 deploy/redeploy_discord_bot.ps1 的參數。

pwsh -File .\deploy\redeploy_discord_bot.ps1 `
  -RemoteHost "ubuntu@161.33.150.3" `
  -SshKeyPath "$HOME/.ssh/alpha-finder-bot.key" `
  -SyncEnv
