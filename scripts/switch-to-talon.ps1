# Switch to Talon mode: stop the Tobii stack (services + apps) so the
# Eye Tracker 5 is free, then launch Talon elevated.
# Self-elevates (services + killing Tobii processes need admin).

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -Verb RunAs -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $MyInvocation.MyCommand.Path)
    exit
}

try { Get-Process -Name 'Tobii*' -ErrorAction Stop | Stop-Process -Force } catch {}

$services = @('TobiiIS5LEYETRACKER5', 'TobiiGeneric', 'Tobii Service')
foreach ($name in $services) {
    try { Stop-Service -Name $name -Force -ErrorAction Stop } catch {}
}

if (-not (Get-Process -Name talon -ErrorAction SilentlyContinue)) {
    Start-Process 'C:\Program Files\Talon\talon.exe'
}

Write-Host ''
Get-Service -Name 'Tobii*' | Format-Table Name, Status -AutoSize
Write-Host 'Talon mode active. Press Ctrl-Alt-E to re-enable the eye mouse.'
Start-Sleep -Seconds 5
