# PowerShell script to execute run_all.py
param (
    [switch]$SkipSsh,
    [switch]$Serve
)

Set-Location -Path $PSScriptRoot

$pythonExe = if (Test-Path ".\venv\Scripts\python.exe") { ".\venv\Scripts\python.exe" } else { "python" }

$argsList = @()
if ($SkipSsh) { $argsList += "--skip-ssh" }
if ($Serve)   { $argsList += "--serve" }

& $pythonExe run_all.py @argsList
