#!/usr/bin/env bash
# Reset DURO de xrdp: mata absolutamente todo Xorg/xfce/xrdp y limpia.

echo '=== procesos xrdp/X/xfce antes ==='
pgrep -af 'xrdp|Xorg|xfce|xreset|wayland|gnome-session' || echo 'none'

echo
echo '=== usuarios con sesion ==='
who
loginctl list-sessions --no-legend 2>/dev/null || true

echo
echo '=== detener xrdp y matar todo lo gráfico ==='
sudo systemctl stop xrdp.service xrdp-sesman.service

# Mata cualquier Xorg corriendo (todos los displays)
sudo pkill -9 Xorg 2>/dev/null || true
sudo pkill -9 -f 'X11.*-auth' 2>/dev/null || true

# Mata cualquier sesión XFCE / fluxbox / xsession
sudo pkill -9 -f 'xfce4-session' 2>/dev/null || true
sudo pkill -9 -f 'xfce4-panel' 2>/dev/null || true
sudo pkill -9 -f 'xfdesktop' 2>/dev/null || true
sudo pkill -9 -f 'xfwm4' 2>/dev/null || true
sudo pkill -9 -f 'xinit' 2>/dev/null || true
sudo pkill -9 -f 'startxfce4' 2>/dev/null || true
sudo pkill -9 -u agaetranahoy -f 'dbus-daemon' 2>/dev/null || true

# Cierra cualquier loginctl session gráfica del usuario
for sid in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
    sudo loginctl terminate-session "$sid" 2>/dev/null || true
done

sleep 2

# Limpiar locks X
sudo rm -f /tmp/.X*-lock 2>/dev/null || true
sudo rm -f /tmp/.X11-unix/X* 2>/dev/null || true
sudo rm -rf /var/run/xrdp/* 2>/dev/null || true

echo
echo '=== procesos despues de matar (deberia ser none) ==='
pgrep -af 'xrdp|Xorg|xfce' || echo 'none'

echo
echo '=== arrancar xrdp limpio ==='
sudo systemctl start xrdp-sesman.service xrdp.service
sleep 3

echo
echo '=== procesos despues de start ==='
pgrep -af 'xrdp|Xorg|xfce' || echo 'none'

echo
echo '=== verificacion final ==='
ss -tlnp 2>/dev/null | grep ':3389' || echo 'no listening'
systemctl is-active xrdp
systemctl is-active xrdp-sesman
