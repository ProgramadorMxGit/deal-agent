#!/usr/bin/env bash
# Termina sesiones huérfanas que NO sean tty/pts (las gráficas).

echo '=== sesiones antes ==='
loginctl list-sessions --no-legend

echo
# Terminar sesiones que NO tengan tty/pts asignado (son las gráficas)
for sid in $(loginctl list-sessions --no-legend | awk '$5 == "-" {print $1}'); do
    echo "terminating session $sid (no tty/pts → graphical leftover)"
    sudo loginctl terminate-session "$sid" 2>/dev/null || true
done

# Esperar a que se procesen
sleep 3

echo
echo '=== sesiones despues ==='
loginctl list-sessions --no-legend

echo
echo '=== arrancar xrdp limpio ==='
sudo systemctl reset-failed xrdp.service xrdp-sesman.service 2>/dev/null || true
sudo systemctl start xrdp-sesman.service
sleep 2
sudo systemctl start xrdp.service
sleep 2

echo
echo '=== status final ==='
echo "xrdp:        $(systemctl is-active xrdp)"
echo "xrdp-sesman: $(systemctl is-active xrdp-sesman)"
ss -tlnp 2>/dev/null | grep ':3389' || echo 'no listening on 3389'
