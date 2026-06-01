# vps_tail.ps1 - Sigue en vivo lo que pasa en el VPS desde Windows.
# Refresca cada N segundos: status del bot + ultimos eventos + outbox.
#
# Uso desde Windows (PowerShell, en la carpeta del repo):
#   .\scripts\vps_tail.ps1                 # default 10s
#   .\scripts\vps_tail.ps1 -Interval 5
#
# Ctrl+C para salir.

param(
    [int] $Interval = 10
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$SshKey      = "$env:USERPROFILE\.ssh\gcp_ofertas_bot"
$SshHost     = "agaetranahoy@34.59.242.95"
$RemoteTail  = "/opt/deal-agent/ofertas_hunter/scripts/vps_tail.sh"

while ($true) {
    Clear-Host
    Write-Host ""
    Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') - VPS tail (refresh ${Interval}s, Ctrl+C para salir)" -ForegroundColor Cyan
    ssh -o ConnectTimeout=8 -i $SshKey $SshHost "bash $RemoteTail"
    Start-Sleep -Seconds $Interval
}
