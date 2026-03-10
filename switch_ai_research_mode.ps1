[CmdletBinding()]
param(
    [ValidateSet("web", "api", "status")]
    [string]$Mode = "status"
)

$ErrorActionPreference = "Stop"

function Get-EffectiveEnv {
    param(
        [string]$Name,
        [string]$Default = ""
    )

    $processValue = [Environment]::GetEnvironmentVariable($Name, "Process")
    if (-not [string]::IsNullOrWhiteSpace($processValue)) {
        return $processValue
    }

    $userValue = [Environment]::GetEnvironmentVariable($Name, "User")
    if (-not [string]::IsNullOrWhiteSpace($userValue)) {
        return $userValue
    }

    return $Default
}

function Show-Status {
    $currentMode = Get-EffectiveEnv -Name "AI_RESEARCH_MODE" -Default "web"
    $currentCatalyst = Get-EffectiveEnv -Name "CATALYST_DETECTOR_ENABLED" -Default "false"

    Write-Output "AI_RESEARCH_MODE=$currentMode"
    Write-Output "CATALYST_DETECTOR_ENABLED=$currentCatalyst"
}

if ($Mode -eq "status") {
    Show-Status
    exit 0
}

$catalystEnabled = if ($Mode -eq "api") { "true" } else { "false" }

$env:AI_RESEARCH_MODE = $Mode
$env:CATALYST_DETECTOR_ENABLED = $catalystEnabled
[Environment]::SetEnvironmentVariable("AI_RESEARCH_MODE", $Mode, "User")
[Environment]::SetEnvironmentVariable("CATALYST_DETECTOR_ENABLED", $catalystEnabled, "User")

Show-Status

if ($Mode -eq "api") {
    $tavilyKey = Get-EffectiveEnv -Name "TAVILY_API_KEY"
    if ([string]::IsNullOrWhiteSpace($tavilyKey)) {
        $tavilyKey = Get-EffectiveEnv -Name "TAVILY_API"
    }

    $geminiKey = Get-EffectiveEnv -Name "GEMINI_API_KEY"
    if ([string]::IsNullOrWhiteSpace($geminiKey)) {
        $geminiKey = Get-EffectiveEnv -Name "GEMINI_API"
    }

    if ([string]::IsNullOrWhiteSpace($tavilyKey) -or [string]::IsNullOrWhiteSpace($geminiKey)) {
        Write-Warning "API mode selected, but Tavily or Gemini key is missing."
    }
}

Write-Output "Next: .\\run_daily.bat"