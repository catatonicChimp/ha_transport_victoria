#!/usr/bin/env bash
# dev.sh — manage the local HA dev container for PTV component testing.
#
# Usage:
#   ./scripts/dev.sh up        Start HA (pulls image on first run)
#   ./scripts/dev.sh down      Stop HA
#   ./scripts/dev.sh restart   Restart HA (picks up component file changes)
#   ./scripts/dev.sh logs      Tail live container logs
#   ./scripts/dev.sh reset     Wipe HA state (.storage) and restart fresh
#   ./scripts/dev.sh shell     Open a shell inside the running container

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CMD="${1:-up}"

cd "$PROJECT_ROOT"

case "$CMD" in
  up)
    echo "Starting Home Assistant dev container..."
    echo "UI will be available at http://localhost:8123"
    podman compose up -d
    echo ""
    echo "Run './scripts/dev.sh logs' to follow the log."
    ;;

  down)
    podman compose down
    ;;

  restart)
    echo "Restarting Home Assistant (reloads component files)..."
    podman compose restart homeassistant
    ;;

  logs)
    podman compose logs -f homeassistant
    ;;

  reset)
    echo "Wiping HA runtime state (.storage, logs)..."
    podman compose down 2>/dev/null || true
    rm -rf dev/ha-config/.storage
    rm -f  dev/ha-config/home-assistant.log
    rm -f  dev/ha-config/.HA_VERSION
    rm -f  dev/ha-config/home-assistant_v2.db
    rm -f  dev/ha-config/home-assistant_v2.db-shm
    rm -f  dev/ha-config/home-assistant_v2.db-wal
    echo "Starting fresh..."
    podman compose up -d
    echo ""
    echo "UI: http://localhost:8123 — run './scripts/dev.sh logs' to follow startup."
    ;;

  shell)
    podman exec -it ptv-ha-dev bash
    ;;

  *)
    echo "Usage: $0 {up|down|restart|logs|reset|shell}"
    exit 1
    ;;
esac
