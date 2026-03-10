# Example only. Do not store secrets in this file.
# Copy to a private script if needed, or call deploy/redeploy_discord_bot.ps1 directly.

pwsh -File .\deploy\redeploy_discord_bot.ps1 `
  -RemoteHost "ubuntu@161.33.150.3" `
  -SshKeyPath "$HOME/.ssh/alpha-finder-bot.key" `
  -SyncEnv
