# scripts/run_amazon_hunter.ps1 — Windows / PowerShell
[CmdletBinding()]
param(
    [string[]]$Seed = @(),
    [int]$Limit = 5,
    [switch]$NoHeadless
)
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $ProjectRoot

$python = if (Test-Path ".\.venv\Scripts\python.exe") {
    ".\.venv\Scripts\python.exe"
} else { "python" }

$cmd = @("-m", "ofertas_hunter", "amazon-hunt", "--limit", "$Limit")
foreach ($s in $Seed) { $cmd += "--seed"; $cmd += $s }
if ($NoHeadless) { $cmd += "--no-headless" }

& $python @cmd
