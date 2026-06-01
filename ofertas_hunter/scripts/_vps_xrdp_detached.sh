#!/usr/bin/env bash
# Arranca el reset de xrdp en BACKGROUND y desligado del SSH actual.
# Usa setsid + nohup para que el reset sobreviva el cierre de mi SSH.

cat > /tmp/_xrdp_actual_reset.sh <<'INNER'
#!/usr/bin/env bash
exec >> /tmp/_xrdp_reset.log 2>&1
echo "=== reset start: $(date) ==="

# Termina sesiones huérfanas (sin tty/pts)
for sid in $(loginctl list-sessions --no-legend | awk '$5 == "-" {print $1}'); do
    echo "terminating $sid"
    loginctl terminate-session "$sid" 2>/dev/null || true
done

sleep 5

# Mata cualquier proceso de display/xfce que haya quedado
pkill -9 -u agaetranahoy -f 'Xorg|xfce4|xinit|startxfce4|xfdesktop|xfwm4|xfce4-panel|xfce4-session' 2>/dev/null || true

sleep 2

# Limpiar locks
rm -f /tmp/.X*-lock 2>/dev/null
rm -rf /var/run/xrdp/* 2>/dev/null

# Reiniciar xrdp limpio
systemctl reset-failed xrdp.service xrdp-sesman.service 2>/dev/null || true
systemctl restart xrdp-sesman.service
sleep 2
systemctl restart xrdp.service
sleep 3

echo "=== status final: $(date) ==="
systemctl is-active xrdp
systemctl is-active xrdp-sesman
loginctl list-sessions --no-legend
ss -tlnp | grep ':3389' || echo 'no listening'
echo "=== reset done ==="
INNER

chmod +x /tmp/_xrdp_actual_reset.sh

# Lanzarlo desligado del SSH actual via systemd-run (para que sobreviva la
# muerte de la sesión).
sudo systemd-run --no-block --collect --unit=xrdp-reset-once.service \
    /tmp/_xrdp_actual_reset.sh

echo "reset launched as systemd transient unit; check /tmp/_xrdp_reset.log"
