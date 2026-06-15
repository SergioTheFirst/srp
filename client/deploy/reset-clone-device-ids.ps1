<#
.SYNOPSIS
    Find SRP agents that share a registry MachineGuid (disk-image clones) and
    reset their device_id so each machine re-derives a unique, clone-safe id.

.DESCRIPTION
    Background: device_id used to be the bare HKLM\SOFTWARE\Microsoft\Cryptography
    MachineGuid. Machines cloned from one disk image (Sysprep /generalize skipped)
    share that GUID, and device_id is the server PRIMARY KEY, so clones overwrote
    each other -- the fleet showed fewer devices than were really connected.

    The fixed agent derives device_id from MachineGuid + hostname + a random
    per-install nonce, so a one-time reset (clear device_id; the agent
    regenerates it on next start) splits the collided clones into separate rows
    -- guaranteed unique even when the clones also share a hostname.

    This script is SURGICAL: it scans the range, groups hosts by MachineGuid, and
    only touches hosts whose GUID is shared by 2+ machines. Unique machines keep
    their id (no fleet churn). Run WITHOUT -Apply first to preview; add -Apply to
    perform the reset.

.PARAMETER Subnet
    First three octets of the /24 to scan. Default "192.168.9".

.PARAMETER Start
    First host octet (default 1).

.PARAMETER End
    Last host octet (default 255).

.PARAMETER TaskName
    Scheduled-task name of the agent. Default "SRP Agent" (matches install-service.ps1).

.PARAMETER Credential
    Admin credential for the target machines. Prompted if omitted.

.PARAMETER Apply
    Actually reset the clones. Without it the script only reports.

.NOTES
    PREREQUISITES
      * Deploy the FIXED agent FIRST. Resetting on the old agent just re-derives
        the bare MachineGuid and the clones collide again.
      * Run from an ELEVATED PowerShell.
      * WinRM/PowerShell remoting must be reachable on the targets (Invoke-Command).
        Workgroup (non-domain) hosts also need the targets in this box's
        TrustedHosts, e.g.:
            Set-Item WSMan:\localhost\Client\TrustedHosts -Value "192.168.9.*" -Force

    AFTER A RESET
      The old shared row keeps its bare-GUID id and stops being updated, so it
      ages out / goes stale on the dashboard; the real machines appear as new,
      correctly-separated devices. This is a one-time event per clone group.

    Windows PowerShell 5.1 compatible (no PS6+ syntax).

.EXAMPLE
    .\reset-clone-device-ids.ps1                      # preview clones on 192.168.9.0/24
    .\reset-clone-device-ids.ps1 -Apply               # reset the detected clones
    .\reset-clone-device-ids.ps1 -Subnet 192.168.9 -Start 10 -End 60 -Apply
#>
[CmdletBinding()]
param(
    [string] $Subnet = "192.168.9",
    [ValidateRange(1, 255)] [int] $Start = 1,
    [ValidateRange(1, 255)] [int] $End = 255,
    [string] $TaskName = "SRP Agent",
    [System.Management.Automation.PSCredential] $Credential,
    [switch] $Apply
)

$ErrorActionPreference = "Stop"

if ($End -lt $Start) { throw "End ($End) must be >= Start ($Start)." }
if (-not $Credential) {
    $Credential = Get-Credential -Message "Admin credentials for $Subnet.$Start-$End"
}

# --- Remote probe: report install state, MachineGuid, current device_id, config path.
$probe = {
    param($TaskName)
    $guid = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Cryptography" -Name MachineGuid).MachineGuid
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        return [pscustomobject]@{ Installed = $false; MachineGuid = $guid }
    }
    $wd = $task.Actions[0].WorkingDirectory
    $cfgPath = Join-Path $wd "client\config.json"
    $devId = $null
    if (Test-Path $cfgPath) {
        $devId = (Get-Content $cfgPath -Raw | ConvertFrom-Json).device_id
    }
    [pscustomobject]@{
        Installed   = $true
        MachineGuid = $guid
        DeviceId    = $devId
        ConfigPath  = $cfgPath
    }
}

# --- Remote reset: clear device_id (preserving every other field), restart task.
$reset = {
    param($ConfigPath, $TaskName)
    # Stop so a failed write or failed restart propagates to the caller's catch
    # (otherwise a silently-dead agent would be reported as a successful reset).
    $ErrorActionPreference = "Stop"
    $existing = Get-Content $ConfigPath -Raw | ConvertFrom-Json
    $cfg = [ordered]@{}
    foreach ($p in $existing.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
    $cfg["device_id"] = ""
    $json = $cfg | ConvertTo-Json -Depth 5
    # UTF-8 WITHOUT BOM: a BOM breaks the agent's json.loads() (matches installer).
    [System.IO.File]::WriteAllText($ConfigPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "Scanning $Subnet.$Start-$End for SRP agents (task '$TaskName')..." -ForegroundColor Cyan

$found = New-Object System.Collections.ArrayList
for ($i = $Start; $i -le $End; $i++) {
    $ip = "$Subnet.$i"
    # 5.1-safe liveness check: -Count/-Quiet exist in WinPS 5.1 (-TimeoutSeconds does not).
    if (-not (Test-Connection -ComputerName $ip -Count 1 -Quiet -ErrorAction SilentlyContinue)) {
        continue
    }
    try {
        $info = Invoke-Command -ComputerName $ip -Credential $Credential `
            -ScriptBlock $probe -ArgumentList $TaskName -ErrorAction Stop
    } catch {
        Write-Warning "$ip reachable but probe failed: $($_.Exception.Message)"
        continue
    }
    if (-not $info.Installed) {
        Write-Host ("  {0,-15} no SRP agent task" -f $ip) -ForegroundColor DarkGray
        continue
    }
    [void]$found.Add([pscustomobject]@{
            Ip          = $ip
            MachineGuid = $info.MachineGuid
            DeviceId    = $info.DeviceId
            ConfigPath  = $info.ConfigPath
        })
    Write-Host ("  {0,-15} guid={1} device_id={2}" -f $ip, $info.MachineGuid, $info.DeviceId)
}

if ($found.Count -eq 0) {
    Write-Host "No SRP agents responded in range." -ForegroundColor Yellow
    return
}

# --- Clone groups: a MachineGuid shared by 2+ reachable hosts.
$cloneGroups = $found | Group-Object MachineGuid | Where-Object { $_.Count -gt 1 }

Write-Host ""
if (-not $cloneGroups) {
    Write-Host "No clones found: every reachable agent has a unique MachineGuid. Nothing to reset." -ForegroundColor Green
    return
}

Write-Host ("Found {0} clone group(s):" -f @($cloneGroups).Count) -ForegroundColor Yellow
foreach ($g in $cloneGroups) {
    Write-Host ("  MachineGuid {0} shared by {1} machines:" -f $g.Name, $g.Count) -ForegroundColor Yellow
    foreach ($h in $g.Group) { Write-Host ("      {0}  (device_id={1})" -f $h.Ip, $h.DeviceId) }
}

$targets = $cloneGroups | ForEach-Object { $_.Group }

if (-not $Apply) {
    Write-Host ""
    Write-Host ("PREVIEW ONLY. Re-run with -Apply to reset device_id on these {0} machine(s)." -f @($targets).Count) -ForegroundColor Cyan
    return
}

Write-Host ""
Write-Host ("Applying reset to {0} clone machine(s)..." -f @($targets).Count) -ForegroundColor Cyan
foreach ($t in $targets) {
    try {
        Invoke-Command -ComputerName $t.Ip -Credential $Credential `
            -ScriptBlock $reset -ArgumentList $t.ConfigPath, $TaskName -ErrorAction Stop
        Write-Host ("  {0,-15} reset OK -- agent restarted, will re-derive a unique id" -f $t.Ip) -ForegroundColor Green
    } catch {
        Write-Warning "$($t.Ip) reset FAILED: $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "Done. Watch the dashboard: the previously-merged machines should appear as" -ForegroundColor Cyan
Write-Host "separate devices within a few minutes; the old shared row goes stale and ages out." -ForegroundColor Cyan
