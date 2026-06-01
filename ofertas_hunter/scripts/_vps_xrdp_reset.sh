#!/usr/bin/env bash
# Reset xrdp limpio: para servicio, mata sesiones huérfanas, limpia locks,
# vuelve a levantar el servicio. SSH sobrevive porque no usa el display.

echo '=== procesos xrdp/X antes ==='
pgrep -af 'xrdp|Xorg|xfce' || echo 'none'

echo
echo '=== reset xrdp ==='
sudo systemctl restart xrdp.service xrdp-sesman.service
sleep 1

# Matar sesiones X huérfanas (display :10, :11, :12...)
sudo pkill -9 -f 'Xorg.*:1[0-9]' 2>/dev/null || true
sudo pkill -9 -f 'xfce4-session' 2>/dev/null || true

# Limpiar locks
sudo rm -f /tmp/.X1*-lock /tmp/.X11-unix/X1* 2>/dev/null || true

# Reiniciar otra vez por si acaso
sudo systemctl restart xrdp.service xrdp-sesman.service
sleep 2

echo
echo '=== procesos xrdp/X despues ==='
pgrep -af 'xrdp|Xorg|xfce' || echo 'none'

echo
echo '=== puerto 3389 ==='
ss -tlnp 2>/dev/null | grep ':3389' || sudo ss -tlnp 2>/dev/null | grep ':3389' || echo 'no listening'

echo
echo '=== xrdp service status ==='
systemctl is-active xrdp
systemctl is-active xrdp-sesman
