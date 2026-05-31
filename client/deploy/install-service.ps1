<#
.SYNOPSIS
    Install the SRP agent as a Windows scheduled task that runs at startup as
    LocalSystem (SYSTEM), so the privileged collectors (SMART /
    StorageReliabilityCounter / WMI perf classes) are unblocked.

.DESCRIPTION
    1. Writes the deployment settings (server_url, optional site/token) into
       client/config.json, preserving any existing fields such as device_id.
    2. Validates by running one real pass (`python -m client.agent --once`),
       which reuses the agent's required-server_url check and performs a live
       collect + send. Install aborts if that pass fails.
    3. Registers a SYSTEM, highest-privilege, restart-on-failure scheduled task
       and starts it.

    Run from an ELEVATED (Administrator) PowerShell. You may need:
        powershell -ExecutionPolicy Bypass -File .\install-service.ps1 -ServerUrl http://192.168.1.10:8000

.PARAMETER ServerUrl
    Required. SRP server address. A LAN address is typical
    (e.g. http://192.168.1.10:8000); a public address is a valid explicit
    choice. Behind a TLS reverse-proxy, use the https:// URL.

.PARAMETER SiteCode
    Optional site/org grouping code (W1.1).

.PARAMETER SiteName
    Optional human-readable site name.

.PARAMETER IngestToken
    Optional shared ingest token (must match the server's). NOTE: passing it on
    the command line exposes it to the process list / shell history; prefer
    editing config.json directly on sensitive hosts.

.PARAMETER PythonExe
    Optional absolute path to python.exe. Defaults to the first `python` on PATH.

.PARAMETER TaskName
    Optional scheduled-task name. Defaults to "SRP Agent".
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ServerUrl,
    [string] $SiteCode = "",
    [string] $SiteName = "",
    [string] $IngestToken = "",
    [string] $PythonExe = "",
    [string] $TaskName = "SRP Agent"
)

$ErrorActionPreference = "Stop"

# This script lives in client/deploy, so client/ is one level up and the repo
# root (where `python -m client.agent` resolves) is two levels up.
$deployDir  = $PSScriptRoot
$clientDir  = (Resolve-Path (Join-Path $deployDir "..")).Path
$repoRoot   = (Resolve-Path (Join-Path $deployDir "..\..")).Path
$configPath = Join-Path $clientDir "config.json"
$logPath    = Join-Path $clientDir "srp-agent.log"

# Resolve python.exe: explicit -PythonExe wins, otherwise the first on PATH.
if ($PythonExe) {
    $py = $PythonExe
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "python not found on PATH; pass -PythonExe C:\path\to\python.exe" }
    $py = $cmd.Source
}
if (-not (Test-Path $py)) { throw "python executable not found: $py" }

# Merge config.json: keep existing fields (e.g. device_id), set the deployment ones.
$cfg = [ordered]@{}
if (Test-Path $configPath) {
    $existing = Get-Content $configPath -Raw | ConvertFrom-Json
    foreach ($prop in $existing.PSObject.Properties) { $cfg[$prop.Name] = $prop.Value }
}
$cfg["server_url"] = $ServerUrl
if ($SiteCode)    { $cfg["site_code"]    = $SiteCode }
if ($SiteName)    { $cfg["site_name"]    = $SiteName }
if ($IngestToken) { $cfg["ingest_token"] = $IngestToken }

# Write UTF-8 WITHOUT BOM: Windows PowerShell 5.1 `Set-Content -Encoding utf8`
# emits a BOM, which would make the agent's json.loads() fail.
$json = $cfg | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Wrote $configPath (server_url=$ServerUrl)"

# Validate with one real pass (cwd = repo root so `client.agent` resolves).
# Read server_url from the config we just wrote -- NO --server override -- so this
# tests the exact path the installed task uses (which has no --server). Reuses the
# required-server_url check (SystemExit 2) plus a live collect+send.
Write-Host "Validating configuration with a one-shot run..."
Push-Location $repoRoot
try {
    & $py -m client.agent --once
    $rc = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($rc -ne 0) {
    throw "Validation run failed (exit $rc). Fix server_url / connectivity before installing."
}

# Register the task: at startup, as SYSTEM, highest privileges, auto-restart,
# no execution time limit (the agent loop runs forever).
$argLine   = "-m client.agent --log-file `"$logPath`""
$action    = New-ScheduledTaskAction -Execute $py -Argument $argLine -WorkingDirectory $repoRoot
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Installed scheduled task '$TaskName' -- runs as SYSTEM at startup."
Write-Host "Logs : $logPath"
Write-Host "Check: Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "Then watch this device on the dashboard; SMART/WMI sources should report 'ok'."
