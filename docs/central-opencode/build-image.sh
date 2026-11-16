#!/usr/bin/env bash
# Build and push the central opencode image to Harbor (run on a machine with
# Docker and a Harbor login; the nervcenter deploy machine works).
set -euo pipefail
cd "$(dirname "$0")"
IMAGE="${IMAGE:-registry.celestium.life/nervecenter/opencode:2.3.0-1}"
docker buildx build --platform linux/amd64 -t "$IMAGE" --push .
docker buildx imagetools inspect "$IMAGE" | sed -n '1,4p'
