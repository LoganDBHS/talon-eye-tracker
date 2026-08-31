# Switch to Tobii mode: quit Talon, start the Tobii services so
# Tobii Experience / game head-tracking can use the Eye Tracker 5.
# Talon holds the tracker exclusively, so it must die first.
# Self-elevates (Talon runs elevated; services need admin too).

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -Verb RunAs -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $MyInvocation.MyCommand.Path)
    exit
}

Stop-Process -Name talon -Force -ErrorAction SilentlyContinue

$services = @('Tobii Service', 'TobiiGeneric', 'TobiiIS5LEYETRACKER5')
foreach ($name in $services) {
    try { Start-Service -Name $name -ErrorAction Stop } catch { Write-Host ("FAILED to start {0}: {1}" -f $name, $_) }
}

Write-Host ''
Get-Service -Name 'Tobii*' | Format-Table Name, Status -AutoSize
Write-Host 'Tobii mode active. Open Tobii Experience normally.'
Write-Host 'NOTE: a reboot auto-starts Talon again (it will steal the tracker).'
Start-Sleep -Seconds 5
