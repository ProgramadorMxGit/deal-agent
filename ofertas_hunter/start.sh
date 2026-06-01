#!/usr/bin/env bash
# start.sh - Lanzador del bot ofertas_hunter (equivalente Linux de start.ps1)
# Uso: ./start.sh
#
# Modos:
#   [1] Modo IA - Orquestador principal (1 ventana xfce4-terminal o sesion tmux)
#   [2] Modo IA - Subagentes en paralelo (4 ventanas xfce4-terminal o 4 sesiones tmux)
#   [3] Modo autonomo Python (sin IA, 24/7) - via systemd service
#   [4] Estado actual
#   [5] Reinstalar agentes kiro-cli
#   [Q] Salir
#
# Diferencia clave vs Windows: el modo [3] no corre en foreground
# bloqueando esta terminal. Usa systemctl para arranque persistente real.
# Para ver logs en vivo del modo [3], usa la opcion [4] o `vps_logs.sh`.

set -uo pipefail

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$BOT_DIR/.venv/bin/python"
KIRO_CLI="${HOME}/.local/bin/kiro-cli"
SERVICE_NAME="ofertas-hunter"
TERM_EMULATOR=""

cd "$BOT_DIR"

# Limpiar lockfiles huerfanos (idempotente)
rm -f data/mcp_serve.lock data/run.lock 2>/dev/null || true

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
color_cyan='\033[36m'
color_green='\033[32m'
color_magenta='\033[35m'
color_yellow='\033[33m'
color_red='\033[31m'
color_dgray='\033[90m'
color_reset='\033[0m'

# Detecta el hunter Amazon activo leyendo .env
get_amazon_hunter_mode() {
    local env_path="$BOT_DIR/.env"
    if [ ! -f "$env_path" ]; then
        echo "nuevo (default)"
        return 0
    fi
    local val
    val=$(grep -E '^\s*AMAZON_HUNTER_LEGACY\s*=' "$env_path" | tail -1 | cut -d= -f2 | tr -d ' "' | tr '[:upper:]' '[:lower:]')
    case "$val" in
        true|1|yes|on)
            echo "LEGACY (anti-captcha)"
            ;;
        *)
            echo "nuevo (default)"
            ;;
    esac
}

print_header() {
    clear
    echo
    echo -e "  ${color_cyan}=================================================${color_reset}"
    echo -e "       ${color_cyan}OFERTAS HUNTER - Lanzador (VPS)${color_reset}"
    echo -e "  ${color_cyan}=================================================${color_reset}"
    echo
    local hunter_mode
    hunter_mode=$(get_amazon_hunter_mode)
    if [[ "$hunter_mode" == *"LEGACY"* ]]; then
        echo -e "  ${color_dgray}Hunter Amazon activo:${color_reset} ${color_yellow}$hunter_mode${color_reset}"
    else
        echo -e "  ${color_dgray}Hunter Amazon activo:${color_reset} ${color_dgray}$hunter_mode${color_reset}"
    fi
    echo
    echo -e "  ${color_green}[1]${color_reset}  Modo IA - Orquestador principal (1 ventana)"
    echo -e "       ${color_dgray}kiro-cli con Claude Sonnet 4.5 + 17 tools MCP${color_reset}"
    echo
    echo -e "  ${color_magenta}[2]${color_reset}  Modo IA - Subagentes en paralelo (4 ventanas)"
    echo -e "       ${color_dgray}Amazon + ML + QA + Telegram. NO despacha solo; requiere orquestador.${color_reset}"
    echo
    echo -e "  ${color_yellow}[3]${color_reset}  Modo autonomo Python (sin IA, 24/7)"
    echo -e "       ${color_dgray}Servicio systemd '$SERVICE_NAME' - arranque al boot${color_reset}"
    echo
    echo -e "  ${color_cyan}[4]${color_reset}  Estado actual"
    echo -e "       ${color_dgray}Que esta pasando ahora mismo (servicio + outbox + eventos)${color_reset}"
    echo
    echo -e "  ${color_dgray}[5]${color_reset}  Reinstalar agentes kiro-cli"
    echo -e "       ${color_dgray}Recrea los 5 agentes en ~/.kiro/agents/${color_reset}"
    echo
    echo -e "  ${color_magenta}[6]${color_reset}  Togglear Hunter Amazon (nuevo <-> legacy)"
    echo -e "       ${color_dgray}Cambia AMAZON_HUNTER_LEGACY en .env${color_reset}"
    echo
    echo -e "  ${color_dgray}[Q]${color_reset}  Salir"
    echo
    echo -e "  ${color_cyan}=================================================${color_reset}"
    echo
}

# Detecta si tenemos un emulador grafico disponible (estamos en XFCE/RDP)
detect_terminal() {
    if [ -n "${DISPLAY:-}" ]; then
        if command -v xfce4-terminal >/dev/null 2>&1; then
            TERM_EMULATOR="xfce4-terminal"
            return 0
        fi
        if command -v gnome-terminal >/dev/null 2>&1; then
            TERM_EMULATOR="gnome-terminal"
            return 0
        fi
    fi
    TERM_EMULATOR=""
    return 1
}

# Verifica que kiro-cli este instalado y logueado antes de modos [1]/[2]
check_kiro_ready() {
    if [ ! -x "$KIRO_CLI" ]; then
        echo -e "  ${color_red}ERROR:${color_reset} kiro-cli no esta instalado en $KIRO_CLI"
        echo -e "  Instala con: ${color_dgray}curl -fsSL https://cli.kiro.dev/install | bash${color_reset}"
        return 1
    fi
    # whoami devuelve OK con login activo, error si no
    if ! "$KIRO_CLI" whoami >/dev/null 2>&1; then
        echo -e "  ${color_yellow}AVISO:${color_reset} kiro-cli sin sesion. Ejecuta primero:"
        echo -e "  ${color_dgray}kiro-cli login${color_reset}"
        return 1
    fi
    return 0
}

# Lanza un agente kiro-cli en su propia "ventana" (xfce4-terminal o sesion tmux)
launch_agent() {
    local agent_name="$1"
    local title="$2"
    local prompt="$3"

    local kiro_cmd="cd '$BOT_DIR' && '$KIRO_CLI' --classic chat --agent '$agent_name' --trust-all-tools '$prompt'; echo; read -p '  Sesion terminada. Presiona Enter para cerrar '"

    if [ "$TERM_EMULATOR" = "xfce4-terminal" ]; then
        xfce4-terminal --title="$title" --command="bash -c \"$kiro_cmd\"" &
    elif [ "$TERM_EMULATOR" = "gnome-terminal" ]; then
        gnome-terminal --title="$title" -- bash -c "$kiro_cmd"
    else
        # Fallback tmux: cada agente en su propia sesion
        local tmux_session
        tmux_session="ofertas_$(echo "$agent_name" | tr - _)"
        tmux kill-session -t "$tmux_session" 2>/dev/null || true
        tmux new-session -d -s "$tmux_session" -- bash -c "cd '$BOT_DIR' && '$KIRO_CLI' --classic chat --agent '$agent_name' --trust-all-tools '$prompt'"
        echo -e "  ${color_green}[OK]${color_reset} $title -> tmux attach -t $tmux_session"
    fi
}

# ---------------------------------------------------------------------------
# Acciones de cada opcion
# ---------------------------------------------------------------------------
mode_orquestador() {
    if ! check_kiro_ready; then
        return 1
    fi
    detect_terminal || true
    echo
    echo -e "  ${color_green}>> Iniciando orquestador principal...${color_reset}"
    echo -e "  ${color_dgray}Agente: ofertas-orquestador${color_reset}"
    echo

    local prompt="Inicia tu ciclo continuo ahora mismo. Empieza con get_status y sigue con get_frontier_stats, process_telegram, discover_seeds, hunt_amazon, hunt_mercadolibre, dispatch_outbox, get_outbox + review borderline, get_recent_events. Sin esperas entre iteraciones."

    if [ "$TERM_EMULATOR" = "xfce4-terminal" ] || [ "$TERM_EMULATOR" = "gnome-terminal" ]; then
        launch_agent "ofertas-orquestador" "Orquestador Principal" "$prompt"
        echo -e "  ${color_green}Orquestador lanzado en ventana propia.${color_reset}"
        sleep 2
    else
        # SSH sin display: corre en foreground
        "$KIRO_CLI" --classic chat --agent ofertas-orquestador --trust-all-tools "$prompt"
    fi
}

mode_subagents() {
    if ! check_kiro_ready; then
        return 1
    fi
    detect_terminal || true
    echo
    echo -e "  ${color_magenta}>> Lanzando 4 subagentes en paralelo...${color_reset}"
    echo

    if [ -z "$TERM_EMULATOR" ]; then
        echo -e "  ${color_yellow}Sin DISPLAY: usando sesiones tmux (attach con tmux attach -t <sesion>).${color_reset}"
        echo
    fi

    local generic_prompt="Inicia tu ciclo continuo. Sin esperas entre iteraciones a menos que scheduler=hibernating."

    launch_agent "ofertas-amazon"   "Amazon Hunter"     "$generic_prompt"
    sleep 2
    launch_agent "ofertas-ml"       "ML Hunter"         "$generic_prompt"
    sleep 2
    launch_agent "ofertas-qa"       "QA Reviewer"       "$generic_prompt"
    sleep 2
    launch_agent "ofertas-telegram" "Telegram Listener" "$generic_prompt"

    echo
    echo -e "  ${color_green}4 subagentes lanzados.${color_reset}"
    echo -e "  ${color_dgray}Estado actual: SIN dispatcher. Todavia no sale nada a WhatsApp.${color_reset}"
    echo
    read -rp "  Quieres lanzar tambien el orquestador para DISPATCH + supervision? [s/N] " resp
    if [[ "$resp" =~ ^[sSyY] ]]; then
        sleep 2
        local orq_prompt="Tu rol AHORA: dispatch_outbox cada vez que pase el cooldown + supervision via get_status/get_recent_events. NO hagas hunt: los subagentes Amazon, ML, QA, Telegram ya estan corriendo. Solo despachas y vigilas."
        launch_agent "ofertas-orquestador" "Orquestador Principal" "$orq_prompt"
    else
        echo -e "  ${color_yellow}Quedas sin dispatch activo.${color_reset}"
    fi
    echo
    read -rp "  Presiona Enter para volver al menu " _
}

mode_python_daemon() {
    echo
    echo -e "  ${color_yellow}Modo daemon Python${color_reset}"
    echo
    echo -e "  Tienes dos formas de correr este modo:"
    echo
    echo -e "    ${color_green}[f]${color_reset} Foreground (igual que Windows)"
        echo -e "       ${color_dgray}orquestador_ia.py con hunters + dispatcher Python directo.${color_reset}"
    echo -e "       ${color_dgray}Se DETIENE si cierras esta ventana / RDP. Ctrl+C para salir.${color_reset}"
    echo
    echo -e "    ${color_cyan}[b]${color_reset} Background con systemd (recomendado para 24/7)"
    echo -e "       ${color_dgray}Sobrevive cierre de RDP, reboot, desconexión.${color_reset}"
    echo -e "       ${color_dgray}Se gestiona con systemctl + journalctl (logs sin Rich).${color_reset}"
    echo
    echo -e "    ${color_dgray}[Enter]${color_reset} Volver al menu"
    echo
    read -rp "  Elige [f/b/Enter]: " sub_mode
    case "${sub_mode,,}" in
        f)
            mode_python_foreground
            ;;
        b)
            mode_python_systemd
            ;;
        *)
            return 0
            ;;
    esac
}

mode_python_foreground() {
    if [ ! -x "$PYTHON" ]; then
        echo -e "  ${color_red}ERROR:${color_reset} no se encuentra venv en $PYTHON"
        read -rp "  Presiona Enter para volver " _
        return 1
    fi
    if ! sudo systemctl is-active "$SERVICE_NAME" >/dev/null 2>&1; then
        : # OK, daemon systemd no esta corriendo
    else
        echo -e "  ${color_red}AVISO:${color_reset} el daemon systemd '$SERVICE_NAME' esta corriendo."
        echo -e "  Lanzar foreground duplicaria los hunters/dispatcher (riesgo de"
        echo -e "  doble publicacion + cooldown roto)."
        read -rp "  Continuar de todos modos? [s/N] " confirm
        [[ "$confirm" =~ ^[sSyY] ]] || return 0
    fi

    echo
    echo -e "  ${color_yellow}>> Arrancando daemon Python (Amazon + ML + Telegram + dispatch)...${color_reset}"
    echo -e "  ${color_dgray}Ctrl+C para detener.${color_reset}"
    echo
    "$PYTHON" "$BOT_DIR/scripts/orquestador_ia.py"
}

mode_python_systemd() {
    echo
    if ! sudo systemctl status "$SERVICE_NAME" --no-pager >/dev/null 2>&1; then
        if [ ! -f "/etc/systemd/system/${SERVICE_NAME}.service" ] && \
           [ ! -f "/lib/systemd/system/${SERVICE_NAME}.service" ]; then
            echo -e "  ${color_red}ERROR:${color_reset} servicio systemd '$SERVICE_NAME' no instalado."
            echo -e "  Crealo con el script de deploy del VPS."
            echo
            read -rp "  Presiona Enter para volver " _
            return 1
        fi
    fi

    echo -e "  ${color_yellow}Modo daemon Python (systemd)${color_reset}"
    echo
    echo -e "  ${color_dgray}A diferencia de Windows, el daemon NO corre en esta terminal."
    echo -e "  Lo gestiona systemd: arranque al boot, restart automatico,"
    echo -e "  logs centralizados en journalctl.${color_reset}"
    echo
    echo -e "  Estado actual:"
    sudo systemctl status "$SERVICE_NAME" --no-pager -l | head -15 || true
    echo
    echo -e "  Acciones:"
    echo -e "    ${color_green}[a]${color_reset} Activar y arrancar (enable + start)"
    echo -e "    ${color_yellow}[r]${color_reset} Reiniciar"
    echo -e "    ${color_red}[s]${color_reset} Detener (sin desactivar arranque al boot)"
    echo -e "    ${color_red}[d]${color_reset} Desactivar definitivamente (disable + stop)"
    echo -e "    ${color_cyan}[l]${color_reset} Ver logs en vivo (Ctrl+C para salir)"
    echo -e "    ${color_dgray}[Enter]${color_reset} Volver al menu"
    echo
    read -rp "  Elige: " action
    case "${action,,}" in
        a)
            echo
            echo -e "  ${color_yellow}AVISO:${color_reset} esto activa el daemon systemd 24/7. Si tambien"
            echo -e "  tienes el orquestador IA corriendo en otra ventana, vas a tener"
            echo -e "  ${color_red}DOS bots${color_reset} cazando + despachando en paralelo (doble publicacion,"
            echo -e "  cooldown roto, mas captchas)."
            read -rp "  Confirmas activar? [s/N] " confirm
            if [[ "$confirm" =~ ^[sSyY] ]]; then
                sudo systemctl enable --now "$SERVICE_NAME"
                sleep 2
                sudo systemctl status "$SERVICE_NAME" --no-pager -l | head -10
            else
                echo -e "  ${color_dgray}Cancelado.${color_reset}"
            fi
            ;;
        r)
            read -rp "  Reiniciar el servicio? [s/N] " confirm
            if [[ "$confirm" =~ ^[sSyY] ]]; then
                sudo systemctl restart "$SERVICE_NAME"
                sleep 2
                sudo systemctl status "$SERVICE_NAME" --no-pager -l | head -10
            else
                echo -e "  ${color_dgray}Cancelado.${color_reset}"
            fi
            ;;
        s)
            read -rp "  Detener el servicio? [s/N] " confirm
            if [[ "$confirm" =~ ^[sSyY] ]]; then
                sudo systemctl stop "$SERVICE_NAME"
            else
                echo -e "  ${color_dgray}Cancelado.${color_reset}"
            fi
            ;;
        d)
            read -rp "  Desactivar definitivamente (no arrancara al boot)? [s/N] " confirm
            if [[ "$confirm" =~ ^[sSyY] ]]; then
                sudo systemctl disable --now "$SERVICE_NAME"
            else
                echo -e "  ${color_dgray}Cancelado.${color_reset}"
            fi
            ;;
        l)
            sudo journalctl -u "$SERVICE_NAME" -f --no-pager
            ;;
        *)
            return 0
            ;;
    esac
    echo
    read -rp "  Presiona Enter para volver " _
}

mode_status() {
    echo
    if [ -x "$BOT_DIR/scripts/vps_status.sh" ]; then
        "$BOT_DIR/scripts/vps_status.sh"
    else
        "$PYTHON" -m ofertas_hunter status
    fi
    echo
    read -rp "  Presiona Enter para volver " _
}

mode_reinstall_agents() {
    echo
    echo -e "  ${color_dgray}>> Reinstalando agentes kiro-cli...${color_reset}"
    echo
    if [ -x "$BOT_DIR/scripts/install_kiro_agents.sh" ]; then
        "$BOT_DIR/scripts/install_kiro_agents.sh"
    else
        "$PYTHON" "$BOT_DIR/scripts/install_kiro_agents.py"
    fi
    echo
    read -rp "  Presiona Enter para volver " _
}

mode_toggle_amazon_hunter() {
    echo
    echo -e "  ${color_magenta}>> Toggle Hunter Amazon (nuevo <-> legacy)${color_reset}"
    echo
    local current
    current=$(get_amazon_hunter_mode)
    echo -e "  Modo actual: ${color_cyan}$current${color_reset}"
    echo
    echo -e "  ${color_dgray}- Nuevo:   AmazonHunterAgent + Playwright persistente.${color_reset}"
    echo -e "  ${color_dgray}- Legacy:  LegacyAmazonHunterAgent (browser efimero,${color_reset}"
    echo -e "  ${color_dgray}           receta anti-captcha del scraper original).${color_reset}"
    echo
    read -rp "  Cambiar a otro hunter? [s/N] " resp
    if [[ ! "$resp" =~ ^[sSyY] ]]; then
        echo -e "  ${color_dgray}Cancelado.${color_reset}"
        echo
        read -rp "  Presiona Enter para volver " _
        return 0
    fi
    local env_path="$BOT_DIR/.env"
    [ -f "$env_path" ] || touch "$env_path"
    # Remover líneas previas del flag
    grep -v -E '^\s*AMAZON_HUNTER_LEGACY\s*=' "$env_path" > "${env_path}.tmp" || true
    mv "${env_path}.tmp" "$env_path"
    if [[ "$current" == *"LEGACY"* ]]; then
        echo "AMAZON_HUNTER_LEGACY=false" >> "$env_path"
        echo -e "  ${color_green}OK:${color_reset} hunter Amazon ahora es 'nuevo'."
    else
        echo "AMAZON_HUNTER_LEGACY=true" >> "$env_path"
        echo -e "  ${color_green}OK:${color_reset} hunter Amazon ahora es 'LEGACY'."
    fi
    echo -e "  ${color_dgray}Reinicia el orquestador (1, 2 o 3) para que tome efecto.${color_reset}"
    echo
    read -rp "  Presiona Enter para volver " _
}

# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------
while true; do
    print_header
    read -rp "  Elige [1/2/3/4/5/6/Q]: " opcion
    case "${opcion^^}" in
        1) mode_orquestador ;;
        2) mode_subagents ;;
        3) mode_python_daemon ;;
        4) mode_status ;;
        5) mode_reinstall_agents ;;
        6) mode_toggle_amazon_hunter ;;
        Q)
            echo
            echo -e "  ${color_dgray}Hasta luego.${color_reset}"
            exit 0
            ;;
        *)
            echo
            echo -e "  ${color_red}Opcion no valida.${color_reset}"
            sleep 1
            ;;
    esac
done
