<#
.SYNOPSIS
    Remove the SRP agent scheduled task created by install-service.ps1.

.DESCRIPTION
    Stops and unregisters the scheduled task. Leaves client/config.json,
    buffer.jsonl, and srp-agent.log in place so the host can be re-installed or
    inspected; delete them manually if you want a clean slate.

    Run from an ELEVATED (Administrator) PowerShell.

.PARAMETER TaskName
    Optional scheduled-task name. Defaults to "SRP Agent".
#>
[CmdletBinding()]
param(
    [string] $TaskName = "SRP Agent"
)

$ErrorActionPreference = "Stop"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "No scheduled task '$TaskName' found -- nothing to do."
    return
}

try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {
    Write-Warning "Could not stop '$TaskName' (continuing to unregister): $($_.Exception.Message)"
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'."
Write-Host "(config.json, buffer.jsonl and srp-agent.log were left in place.)"
