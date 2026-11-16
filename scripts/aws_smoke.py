"""Smoke the AWS deployment through CloudFront, HTTP only (spec section 7).

Reads the URL and passwords from `scripts/tf.sh output`. Checks: the API is ready, a demo
login works, gRPC-Web streams, every tool path and /argocd answer 401 without the login and
200 with it, hashed assets are cached and the API is not.
"""

from __future__ import annotations

import re
import subprocess
import sys

import httpx

TOOLS = ["/grafana/", "/jaeger/", "/temporal/", "/mailpit/", "/rabbitmq/",
         "/prometheus/graph", "/argocd/"]


def output(name: str) -> str:
    return subprocess.run(["bash", "scripts/tf.sh", "output", "-raw", name],
                          capture_output=True, text=True, check=True).stdout.strip()


def step(name: str) -> None:
    print(f"-- {name}", flush=True)


def main() -> int:
    base = output("cloudfront_url")
    tools_pw = output("tools_password")
    demo_pw = output("demo_password")
    client = httpx.Client(timeout=30, follow_redirects=False)

    step("API ready through CloudFront")
    r = client.get(f"{base}/api/ready")
    if r.status_code != 200:
        raise SystemExit(f"/api/ready answered {r.status_code}")

    step("demo login")
    r = client.post(f"{base}/api/auth/login",
                    json={"email": "demo.desk1@medinova.example", "password": demo_pw})
    r.raise_for_status()

    step("gRPC-Web (assistant) through CloudFront")
    result = subprocess.run([sys.executable, "scripts/grpcweb_smoke.py", "--edge", base,
                             "--email", "demo.desk1@medinova.example", "--password", demo_pw],
                            capture_output=True, text=True, check=False)
    print(result.stdout[-600:])
    if result.returncode != 0:
        raise SystemExit(f"grpc-web smoke failed:\n{result.stderr[-1200:]}")

    step("tools: 401 without the login, 200 with it")
    for path in TOOLS:
        if (code := client.get(f"{base}{path}").status_code) != 401:
            raise SystemExit(f"{path}: expected 401 without login, got {code}")
        authed = client.get(f"{base}{path}", auth=("reviewer", tools_pw),
                            follow_redirects=True)
        if authed.status_code != 200:
            raise SystemExit(f"{path}: expected 200 with login, got {authed.status_code}")

    step("caching: assets cached, API not")
    index = client.get(f"{base}/").text
    # Vite emits <script type="module" crossorigin src="/assets/index-<hash>.js">.
    found = re.search(r"""(?:src|href)=["'](/assets/[^"']+)["']""", index)
    if found:
        asset = found.group(1)
        client.get(f"{base}{asset}")
        cache = client.get(f"{base}{asset}").headers.get("x-cache", "")
        if "hit" not in cache.lower():
            raise SystemExit(f"{asset} not cached (x-cache={cache})")
    else:
        print("   note: no /assets/ reference in the index page; asset caching not checked")
    api_cache = client.get(f"{base}/api/health").headers.get("x-cache", "")
    if "hit" in api_cache.lower():
        raise SystemExit(f"/api/health was served from cache ({api_cache})")

    print(f"aws smoke: OK -- {base}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
