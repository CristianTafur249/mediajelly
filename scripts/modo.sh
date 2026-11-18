#!/usr/bin/env bash
# Modo trabajo / modo libre: cede CPU y RAM al escritorio en horario laboral, sin reiniciar el stack.
#
#   trabajo  mediajelly-cron y jellyfin bajan a 1.5 CPUs con prioridad mínima, y se detienen los
#            servicios que solo sirven para buscar o pedir contenido (flaresolverr, jackett, bazarr, seerr).
#            Transmission, Sonarr, Radarr, Prowlarr, Jellyfin, Samba y los bots siguen funcionando.
#   libre    restaura los límites del docker-compose.yaml y arranca lo detenido.
#   auto     elige según la hora: trabajo de lunes a viernes entre 07:30 y 18:30; libre el resto.
#   estado   muestra el modo y el consumo actual.
#   --instalar / --desinstalar   timers de systemd de usuario que ejecutan "auto" (07:30, 18:30 y al iniciar sesión).
#
# Uso: scripts/modo.sh [trabajo|libre|auto|estado|--instalar|--desinstalar]
set -euo pipefail
cd "$(dirname "$0")/.."

DETENIBLES=(flaresolverr jackett bazarr seerr)
LIMITADOS=(mediajelly-cron jellyfin)
CPUS_TRABAJO="1.5"
INICIO="07:30"
FIN="18:30"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
ESTADO="${XDG_STATE_HOME:-$HOME/.local/state}/mediajelly-modo"

cid() { docker compose ps -q "$1" 2>/dev/null; }

# CPUs declaradas en el compose para un servicio (lo que se restaura en modo libre).
cpus_compose() {
    docker compose config --format json | python3 -I -c '
import json, sys
print(json.load(sys.stdin)["services"][sys.argv[1]].get("cpus") or 0)' "$1"
}

modo_auto() {
    local dia hora
    dia=$(date +%u)
    hora=$(date +%H:%M)
    if [ "$dia" -le 5 ] && [[ ! "$hora" < "$INICIO" ]] && [[ "$hora" < "$FIN" ]]; then
        echo trabajo
    else
        echo libre
    fi
}

trabajo() {
    for s in "${LIMITADOS[@]}"; do
        c=$(cid "$s")
        [ -n "$c" ] && docker update --cpus "$CPUS_TRABAJO" --cpu-shares 128 "$c" >/dev/null
    done
    docker compose stop "${DETENIBLES[@]}" >/dev/null 2>&1 || true
    echo trabajo >"$ESTADO"
    echo "Modo trabajo: ${LIMITADOS[*]} a $CPUS_TRABAJO CPUs; detenidos: ${DETENIBLES[*]}"
}

libre() {
    for s in "${LIMITADOS[@]}"; do
        c=$(cid "$s")
        [ -n "$c" ] && docker update --cpus "$(cpus_compose "$s")" --cpu-shares 256 "$c" >/dev/null
    done
    docker compose start "${DETENIBLES[@]}" >/dev/null 2>&1 || true
    echo libre >"$ESTADO"
    echo "Modo libre: límites del compose restaurados; arrancados: ${DETENIBLES[*]}"
}

estado() {
    echo "Modo: $(cat "$ESTADO" 2>/dev/null || echo 'desconocido') (según la hora correspondería: $(modo_auto))"
    docker stats --no-stream --format '  {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}'
}

instalar() {
    mkdir -p "$UNIT_DIR"
    cat >"$UNIT_DIR/mediajelly-modo.service" <<EOF
[Unit]
Description=mediaJelly: modo trabajo o libre según la hora
After=docker.service

[Service]
Type=oneshot
ExecStart="$PWD/scripts/modo.sh" auto
EOF
    cat >"$UNIT_DIR/mediajelly-modo.timer" <<EOF
[Unit]
Description=mediaJelly: cambia de modo a las $INICIO y $FIN (L-V) y al iniciar sesión

[Timer]
OnCalendar=Mon..Fri $INICIO
OnCalendar=Mon..Fri $FIN
OnStartupSec=2min
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now mediajelly-modo.timer
    systemctl --user list-timers mediajelly-modo.timer --no-pager
}

desinstalar() {
    systemctl --user disable --now mediajelly-modo.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/mediajelly-modo.service" "$UNIT_DIR/mediajelly-modo.timer"
    systemctl --user daemon-reload
    libre
}

mkdir -p "$(dirname "$ESTADO")"
case "${1:-estado}" in
    trabajo) trabajo ;;
    libre) libre ;;
    auto) if [ "$(modo_auto)" = trabajo ]; then trabajo; else libre; fi ;;
    estado) estado ;;
    --instalar) instalar ;;
    --desinstalar) desinstalar ;;
    *) echo "Uso: $0 [trabajo|libre|auto|estado|--instalar|--desinstalar]" >&2; exit 2 ;;
esac
