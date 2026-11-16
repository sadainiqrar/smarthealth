#!/usr/bin/env bash
# scripts/release.sh -- build and push the five images to GHCR, record the tag (and the
# CloudFront URL) in values-aws.yaml, and commit. Then fast-forward main and push: Argo CD
# deploys it. Prerequisite: `docker login ghcr.io` with a token that can write packages.
. "$(dirname "$0")/aws_lib.sh"
registry="ghcr.io/sadainiqrar"
tag="$(git -C "$ROOT" rev-parse --short HEAD)"
[ -z "$(git -C "$ROOT" status --porcelain)" ] || { echo "commit your changes first" >&2; exit 1; }
"$ROOT/scripts/push_images.sh" --push "$registry"
url="$(tf_output cloudfront_url 2>/dev/null || true)"
"$PY" "$ROOT/scripts/set_release.py" "$ROOT/deploy/helm/smarthealth/values-aws.yaml" "$tag" "$url"
git -C "$ROOT" add deploy/helm/smarthealth/values-aws.yaml
git -C "$ROOT" commit -m "release: $tag"
echo "committed release $tag -- fast-forward main and push; Argo CD syncs within 3 minutes"
