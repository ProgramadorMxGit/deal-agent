#!/usr/bin/env bash
echo '--- desktops detectados ---'
for de in xfce4 ubuntu-desktop gnome-shell lxde-core lxqt mate-desktop-environment cinnamon-desktop-environment kde-plasma-desktop fluxbox openbox; do
  dpkg -l "$de" 2>/dev/null | grep -q '^ii' && echo "FOUND: $de"
done

echo
echo '--- VNC / RDP instalados ---'
for v in tigervnc-standalone-server tigervnc-common tigervnc-tools vnc4server tightvncserver x11vnc xrdp; do
  dpkg -l "$v" 2>/dev/null | grep -q '^ii' && echo "FOUND: $v"
done

echo
echo '--- procesos vnc/x corriendo ---'
pgrep -af 'vnc|Xvfb|Xorg|xrdp' || echo "(ninguno)"

echo
echo '--- puertos en escucha (5900, 5901, 3389) ---'
ss -tlnp 2>/dev/null | grep -E ':5900|:5901|:5902|:3389' || echo "(ninguno)"

echo
echo '--- usuario y home ---'
whoami
ls -la /home/agaetranahoy/.vnc 2>/dev/null || echo "(no .vnc dir)"
