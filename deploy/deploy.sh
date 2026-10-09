#!/usr/bin/env bash
# Server-side deploy: pull the new code and image, restart, check health.
#
# Run by GitHub Actions through a restricted SSH key (see README, "Automatic deploys"):
# the key can only start this script, and the requested image tag arrives in
# $SSH_ORIGINAL_COMMAND. Can also be run by hand: deploy/deploy.sh [<commit sha>|latest]
set -euo pipefail

# Everything runs inside main(): bash reads scripts lazily, and `git pull` below may rewrite this file.
main() {
  cd "$(dirname "$0")/.."

  tag="${SSH_ORIGINAL_COMMAND:-${1:-latest}}"
  if ! [[ "$tag" =~ ^([0-9a-f]{7,40}|latest)$ ]]; then
    echo "deploy: refusing tag '$tag' (expected a commit sha or 'latest')" >&2
    exit 2
  fi

  echo "deploy: updating code"
  git pull --ff-only --quiet

  export IMAGE_TAG="$tag"
  echo "deploy: pulling image tag $IMAGE_TAG"
  docker compose --profile https pull --quiet
  docker compose --profile https up -d --remove-orphans

  if grep -qiE '^MCP_ENABLED=(true|1|yes)' .env 2>/dev/null; then
    echo -n "deploy: waiting for /health"
    for _ in $(seq 1 30); do
      if curl -fsS http://127.0.0.1:8765/health >/dev/null 2>&1; then
        echo " ok"
        docker image prune -f >/dev/null
        echo "deploy: done ($IMAGE_TAG)"
        exit 0
      fi
      echo -n "."
      sleep 2
    done
    echo " FAILED"
    docker compose logs --tail 50 ghostwriter >&2
    exit 1
  fi

  sleep 5
  docker compose ps
  docker image prune -f >/dev/null
  echo "deploy: done ($IMAGE_TAG)"
}

main "$@"
exit
