#!/usr/bin/env bash
# Permite múltiples sesiones xrdp simultáneas y limpia el lingering.
set -e

# 1. Desactivar lingering del usuario (que era lo que mantenía sesiones huérfanas)
sudo loginctl disable-linger agaetranahoy 2>/dev/null || true

# 2. Backup config xrdp si no hay
[ -f /etc/xrdp/xrdp.ini.original ] || sudo cp /etc/xrdp/xrdp.ini /etc/xrdp/xrdp.ini.original

# 3. Verificar que xrdp.ini tenga MaxSessions alto (default es 50, suele bastar)
echo "=== MaxSessions actual ==="
grep -E '^MaxSessions' /etc/xrdp/xrdp.ini || echo "(usando default)"

# 4. Asegurar que xrdp permita re-conexión (no kill_disconnected)
echo "=== KillDisconnected ==="
grep -E '^KillDisconnected' /etc/xrdp/xrdp.ini || echo "(usando default false)"

# 5. Lanzar reset completo en transient unit
cat > /tmp/_xrdp_finalreset.sh <<'INNER'
#!/usr/bin/env bash
exec >> /tmp/_xrdp_finalreset.log 2>&1
echo "=== FINAL RESET START $(date) ==="

# Terminar TODAS las sesiones gráficas (las que no tienen tty/pts)
for sid in $(loginctl list-sessions --no-legend | awk '$5 == "-" {print $1}'); do
    echo "kill session $sid"
    loginctl terminate-session "$sid" 2>/dev/null || true
done

# Para mas seguridad, terminar también el "user manager" del UID
# que mantiene las sesiones lingering vivas
systemctl stop user@1001.service 2>/dev/null || true

sleep 4

# Mata Xorg/xfce huérfanos
pkill -9 -u agaetranahoy -f 'Xorg|xfce4-session|xfce4-panel|xfwm4|xfdesktop|startxfce4|xinit' 2>/dev/null || true

sleep 2

# Limpia locks y temp
rm -f /tmp/.X*-lock /tmp/.X11-unix/X1* 2>/dev/null
rm -rf /var/run/xrdp/* 2>/dev/null
rm -rf /run/user/1001/* 2>/dev/null

# Restart xrdp
systemctl reset-failed xrdp.service xrdp-sesman.service 2>/dev/null || true
systemctl restart xrdp-sesman.service
sleep 2
systemctl restart xrdp.service
sleep 3

echo
echo "=== sessions despues ==="
loginctl list-sessions --no-legend
echo
echo "=== xrdp activo ==="
systemctl is-active xrdp
systemctl is-active xrdp-sesman
ss -tlnp 2>/dev/null | grep ':3389'
echo "=== FIN $(date) ==="
INNER

chmod +x /tmp/_xrdp_finalreset.sh
sudo systemd-run --no-block --collect --unit=xrdp-final-reset.service /tmp/_xrdp_finalreset.sh

echo
echo "Lanzado. Espera 15s y revisa: cat /tmp/_xrdp_finalreset.log"
