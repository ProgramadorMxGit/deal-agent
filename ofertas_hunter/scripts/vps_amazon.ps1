# vps_amazon.ps1 - Lista las ofertas Amazon que el bot ha encontrado en el VPS.
# Uso: .\scripts\vps_amazon.ps1

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$SshKey  = "$env:USERPROFILE\.ssh\gcp_ofertas_bot"
$SshHost = "agaetranahoy@34.59.242.95"

ssh -o ConnectTimeout=8 -i $SshKey $SshHost "bash /opt/deal-agent/ofertas_hunter/scripts/_vps_amazon_offers.sh"
