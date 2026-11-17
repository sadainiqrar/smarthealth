#!/usr/bin/env bash
# scripts/release.sh -- build and push the five images to GHCR, record the tag (and the
# CloudFront URL) in values-aws.yaml, and commit. Then fast-forward main and push: Argo CD
# deploys it. Prerequisites: `docker login ghcr.io` with a token that can write packages, and
# helm on PATH -- the chart tests render the release values before anything is committed.
. "$(dirname "$0")/aws_lib.sh"
registry="ghcr.io/sadainiqrar"
tag="$(git -C "$ROOT" rev-parse --short HEAD)"
[ -z "$(git -C "$ROOT" status --porcelain)" ] || { echo "commit your changes first" >&2; exit 1; }
command -v helm >/dev/null 2>&1 || { echo "helm is needed to check the release (the chart tests skip without it)" >&2; exit 1; }
"$ROOT/scripts/push_images.sh" --push "$registry"
url="$(tf_output cloudfront_url 2>/dev/null || true)"
"$PY" "$ROOT/scripts/set_release.py" "$ROOT/deploy/helm/smarthealth/values-aws.yaml" "$tag" "$url"
# Render the AWS values before committing them: an all-digit tag once rendered as 3.244061e+06.
if ! (cd "$ROOT" && "$PY" -m pytest -q -p no:cacheprovider tests/meta/test_release.py tests/meta/test_helm_charts.py); then
  git -C "$ROOT" checkout -- deploy/helm/smarthealth/values-aws.yaml
  echo "the release values do not render; nothing committed (images are pushed)" >&2; exit 1
fi
git -C "$ROOT" add deploy/helm/smarthealth/values-aws.yaml
git -C "$ROOT" commit -m "release: $tag"
echo "committed release $tag -- fast-forward main and push; Argo CD syncs within 3 minutes"
