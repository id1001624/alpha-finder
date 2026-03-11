[CmdletBinding()]
param(
    [string]$RemoteHost = "ubuntu@161.33.150.3",
    [string]$SshKeyPath = "$HOME/.ssh/alpha-finder-bot.key",
    [string]$RemoteAppDir = "/opt/alpha-finder",
    [string]$RemoteServiceName = "alpha-finder-discord-bot.service",
    [switch]$SyncEnv,
    [switch]$SkipPipInstall
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$serviceFile = Join-Path $repoRoot "deploy/systemd/alpha-finder-discord-bot.service"
$tempArchive = Join-Path ([System.IO.Path]::GetTempPath()) "alpha-finder-discord-bot-redeploy.tar"
$tempEnvFile = Join-Path ([System.IO.Path]::GetTempPath()) "alpha-finder-discord-bot.env"

function Require-Command {
    param([string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name"
    }
}

function Invoke-Ssh {
    param([string]$CommandText)

    $normalizedCommand = $CommandText -replace "`r`n", "`n"
    & ssh -i $SshKeyPath $RemoteHost $normalizedCommand
    if ($LASTEXITCODE -ne 0) {
        throw "SSH command failed."
    }
}

Require-Command git
Require-Command ssh
Require-Command scp

if (-not (Test-Path $SshKeyPath)) {
    throw "SSH key not found: $SshKeyPath"
}

if (-not (Test-Path $serviceFile)) {
    throw "Service file not found: $serviceFile"
}

Push-Location $repoRoot
try {
    $gitStatus = & git status --short
    if ($LASTEXITCODE -ne 0) {
        throw "git status failed."
    }
    if (-not [string]::IsNullOrWhiteSpace(($gitStatus | Out-String))) {
        Write-Warning "Working tree has uncommitted changes; redeploy only ships committed git HEAD."
    }

    & git archive --format=tar --output=$tempArchive HEAD
    if ($LASTEXITCODE -ne 0) {
        throw "git archive failed."
    }

    & scp -i $SshKeyPath $tempArchive "${RemoteHost}:/tmp/alpha-finder-deploy.tar"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upload deployment archive."
    }

    & scp -i $SshKeyPath $serviceFile "${RemoteHost}:/tmp/alpha-finder-discord-bot.service"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upload systemd service file."
    }

    if ($SyncEnv) {
        $requiredEnvNames = @(
            "DISCORD_BOT_TOKEN",
            "TURSO_DATABASE_URL",
            "TURSO_AUTH_TOKEN"
        )
        foreach ($name in $requiredEnvNames) {
            $value = [Environment]::GetEnvironmentVariable($name)
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                continue
            }
            throw "Missing required environment variable: $name"
        }

        $envText = @"
DISCORD_BOT_ENABLED=true
DISCORD_BOT_TOKEN=$($env:DISCORD_BOT_TOKEN)
DISCORD_BOT_ALLOWED_CHANNEL_IDS=$($env:DISCORD_BOT_ALLOWED_CHANNEL_IDS)
DISCORD_BOT_SYNC_GUILD_ID=$($env:DISCORD_BOT_SYNC_GUILD_ID)
DISCORD_BOT_PREFIX=$($env:DISCORD_BOT_PREFIX)
TURSO_ENABLED=true
TURSO_DATABASE_URL=$($env:TURSO_DATABASE_URL)
TURSO_AUTH_TOKEN=$($env:TURSO_AUTH_TOKEN)
DISCORD_WEBHOOK_URL=$($env:DISCORD_WEBHOOK_URL)
FINNHUB_API_KEY=$($env:FINNHUB_API_KEY)
TAVILY_API_KEY=$($env:TAVILY_API_KEY)
GEMINI_API_KEY=$($env:GEMINI_API_KEY)
"@
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($tempEnvFile, $envText, $utf8NoBom)

        & scp -i $SshKeyPath $tempEnvFile "${RemoteHost}:/tmp/discord-bot.env"
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to upload remote env file."
        }
    }

    $pipCommand = if ($SkipPipInstall) {
        "echo 'Skip pip install'"
    } else {
        "sudo -u alphafinder $RemoteAppDir/.venv/bin/pip install -r $RemoteAppDir/requirements.txt"
    }

    $envInstallCommand = if ($SyncEnv) {
        "sudo install -o root -g root -m 600 /tmp/discord-bot.env /etc/alpha-finder/discord-bot.env;"
    } else {
        ""
    }

    $remoteCommand = @"
set -e
sudo mkdir -p $RemoteAppDir /etc/alpha-finder /var/log/alpha-finder
sudo chown -R alphafinder:alphafinder $RemoteAppDir /var/log/alpha-finder
sudo rm -rf $RemoteAppDir/*
sudo tar -xf /tmp/alpha-finder-deploy.tar -C $RemoteAppDir
sudo chown -R alphafinder:alphafinder $RemoteAppDir
if [ ! -x $RemoteAppDir/.venv/bin/python ]; then
  sudo -u alphafinder python3 -m venv $RemoteAppDir/.venv
fi
sudo -u alphafinder $RemoteAppDir/.venv/bin/pip install --upgrade pip
$pipCommand
$envInstallCommand
sudo install -o root -g root -m 644 /tmp/alpha-finder-discord-bot.service /etc/systemd/system/$RemoteServiceName
sudo systemctl daemon-reload
sudo systemctl restart $RemoteServiceName
sudo systemctl --no-pager --full status $RemoteServiceName
echo '---LOG---'
sudo tail -n 60 /var/log/alpha-finder/discord-bot.log
"@

    Invoke-Ssh $remoteCommand
}
finally {
    Pop-Location
    if (Test-Path $tempArchive) {
        Remove-Item $tempArchive -Force
    }
    if (Test-Path $tempEnvFile) {
        Remove-Item $tempEnvFile -Force
    }
}
