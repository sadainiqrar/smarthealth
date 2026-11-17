# scripts/k3d_smoke.py
"""Prove the k3d install works end to end through the ingress (spec section 8).

Fails on the first problem. Assumes `scripts/k3d_up.sh` has run and kubectl's context is
k3d-smarthealth. Creates a front-desk login with the CLI inside core-api, then checks the
API, gRPC-Web, the tool logins, runs the demo seed Job and prints memory per pod.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

import httpx

NS = "smarthealth"
TOOLS = ["/grafana/", "/jaeger/", "/temporal/", "/mailpit/", "/prometheus/graph"]
# RabbitMQ is guarded by its own login, not the tools login (the two share one header).
RABBITMQ_API = "/rabbitmq/api/overview"


def kubectl(*args: str, check: bool = True) -> str:
    result = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True,
                            check=False)
    if check and result.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def step(name: str) -> None:
    print(f"-- {name}", flush=True)


def pods_ready() -> None:
    step("every pod Ready, nothing restarting")
    pods = json.loads(kubectl("get", "pods", "-o", "json"))["items"]
    bad = []
    for pod in pods:
        if pod["status"].get("phase") == "Succeeded":
            continue
        statuses = pod["status"].get("containerStatuses", [])
        if not statuses or not all(s["ready"] for s in statuses):
            bad.append(pod["metadata"]["name"])
    if bad:
        raise SystemExit(f"not ready: {bad}")
    before = {p["metadata"]["name"]: sum(s["restartCount"] for s in
                                         p["status"].get("containerStatuses", []))
              for p in pods}
    time.sleep(60)
    after = json.loads(kubectl("get", "pods", "-o", "json"))["items"]
    restarting = [p["metadata"]["name"] for p in after
                  if sum(s["restartCount"] for s in p["status"].get("containerStatuses", []))
                  > before.get(p["metadata"]["name"], 0)]
    if restarting:
        raise SystemExit(f"restarting: {restarting}")


def api(base: str, email: str, password: str) -> None:
    step("API through the ingress")
    # Right after an upgrade a dependency may still be reconnecting: give it a minute.
    deadline = time.monotonic() + 60
    while (r := httpx.get(f"{base}/api/ready", timeout=10)).status_code != 200:
        if time.monotonic() > deadline:
            raise SystemExit(f"/api/ready: {r.status_code} {r.text[:500]}")
        time.sleep(3)
    docs = httpx.get(f"{base}/api/docs", timeout=10)
    if "/api/openapi.json" not in docs.text:
        raise SystemExit("Swagger does not point at /api/openapi.json (UVICORN_ROOT_PATH)")
    kubectl("exec", "deploy/core-api", "--", "python", "-m", "app.cli", "create-user",
            "--email", email, "--password", password, "--role", "front_desk", check=False)
    r = httpx.post(f"{base}/api/auth/login", json={"email": email, "password": password},
                   timeout=10)
    r.raise_for_status()


def grpc_web(base: str, email: str, password: str) -> None:
    step("gRPC-Web (assistant) through the ingress")
    result = subprocess.run([sys.executable, "scripts/grpcweb_smoke.py", "--edge", base,
                             "--email", email, "--password", password],
                            capture_output=True, text=True, check=False)
    print(result.stdout[-800:])
    if result.returncode != 0:
        raise SystemExit(f"grpc-web smoke failed:\n{result.stderr[-1500:]}")


def tools(base: str, user: str, password: str) -> None:
    step("tool UIs: 401 without the login, 200 with it")
    for path in TOOLS:
        anonymous = httpx.get(f"{base}{path}", timeout=10)
        if anonymous.status_code != 401:
            raise SystemExit(f"{path}: expected 401 without login, got {anonymous.status_code}")
        authed = httpx.get(f"{base}{path}", auth=(user, password), timeout=10,
                           follow_redirects=True)
        if authed.status_code != 200:
            raise SystemExit(f"{path}: expected 200 with login, got {authed.status_code}")
    rabbit = {
        "page, no login": httpx.get(f"{base}/rabbitmq/", timeout=10).status_code == 200,
        "api, no login": httpx.get(f"{base}{RABBITMQ_API}", timeout=10).status_code == 401,
        "api, tools login": httpx.get(f"{base}{RABBITMQ_API}", auth=(user, password),
                                      timeout=10).status_code == 401,
        "api, its own login": httpx.get(f"{base}{RABBITMQ_API}",
                                        auth=("smarthealth", "smarthealth"),  # dev default
                                        timeout=10).status_code == 200,
    }
    if not all(rabbit.values()):
        raise SystemExit(f"rabbitmq: {rabbit}")


def demo_seed() -> None:
    step("demo-seed from its CronJob (takes several minutes)")
    name = f"demo-seed-{int(time.time())}"
    kubectl("create", "job", "--from=cronjob/demo-seed", name)
    kubectl("wait", "--for=condition=complete", f"job/{name}", "--timeout=40m")
    print(kubectl("logs", f"job/{name}")[-1500:])


def memory() -> None:
    step("memory per pod (kubectl top)")
    print(kubectl("top", "pods", "--sort-by=memory", check=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://localhost:8080")
    parser.add_argument("--tools-user", default="reviewer")
    parser.add_argument("--tools-password", default="smarthealth")
    parser.add_argument("--skip-demo", action="store_true")
    args = parser.parse_args()
    email, password = "smoke.desk@medinova.example", "smoke-password-123"
    pods_ready()
    api(args.base, email, password)
    grpc_web(args.base, email, password)
    tools(args.base, args.tools_user, args.tools_password)
    if not args.skip_demo:
        demo_seed()
    memory()
    print("k3d smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
