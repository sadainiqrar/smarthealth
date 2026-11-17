#!/usr/bin/env bash
# Build SmartHealth's five images, tagged with the current commit.
#   --local            import them into the k3d cluster "smarthealth" (tag also "dev")
#   --push REGISTRY    push to REGISTRY (e.g. ghcr.io/<owner>); `docker login` first
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="$(git rev-parse --short HEAD)"
declare -A IMAGES=(
  [smarthealth-core]=". -f Dockerfile"
  [smarthealth-knowledge]=". -f services/knowledge/Dockerfile"
  [smarthealth-assistant]=". -f services/assistant/Dockerfile"
  [smarthealth-payments-mock]="services/payments_mock"
  [smarthealth-frontend]="frontend"
)

for name in "${!IMAGES[@]}"; do
  echo ">> building $name:$TAG"
  # shellcheck disable=SC2086
  docker build -t "$name:$TAG" -t "$name:dev" ${IMAGES[$name]}
done

case "${1:-}" in
  --local)
    names=("${!IMAGES[@]}")
    k3d image import -c smarthealth "${names[@]/%/:dev}"
    ;;
  --push)
    registry="${2:?usage: push_images.sh --push REGISTRY}"
    for name in "${!IMAGES[@]}"; do
      docker tag "$name:$TAG" "$registry/$name:$TAG"
      # GHCR can answer `denied` once on a package's first push after the layers uploaded;
      # the retry succeeds (first AWS deploy, docs/ARCHITECTURE.md 8.8).
      for attempt in 1 2 3; do
        docker push "$registry/$name:$TAG" && break
        [ "$attempt" = 3 ] && { echo "push of $name failed 3 times" >&2; exit 1; }
        echo "push of $name failed; retrying ($attempt/3)" >&2; sleep 5
      done
    done
    echo "pushed tag $TAG -- set global.image.registry=$registry global.image.tag=$TAG"
    ;;
  "") echo "built tag $TAG (use --local or --push REGISTRY)";;
  *) echo "unknown option: $1" >&2; exit 2;;
esac
