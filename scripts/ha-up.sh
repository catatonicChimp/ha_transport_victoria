#!/bin/bash
export PATH="/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")/.."
podman-compose up -d
exec podman logs -f ptv-ha-dev
