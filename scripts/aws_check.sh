#!/usr/bin/env bash
# scripts/aws_check.sh -- check the deployment from INSIDE the server, through Traefik on
# localhost: the same front door CloudFront uses, so it works before CloudFront exists and
# narrows a failure to "behind CloudFront" or "CloudFront itself" afterwards.
# Runs over SSM Run Command; the passwords are read from SSM on the server, never printed.
# scripts/aws_smoke.py is the outside view (through CloudFront, plus caching).
. "$(dirname "$0")/aws_lib.sh"
server="$(tf_output server_instance_id)"
grpc="$(base64 -w0 < "$ROOT/scripts/grpcweb_smoke.py")"

read -r -d '' remote <<'REMOTE' || true
set -u
fail=0
ok()   { echo "ok    $1"; }
bad()  { echo "FAIL  $1"; fail=1; }
want() { if [ "$3" = "$2" ]; then ok "$1"; else bad "$1 (got $3, want $2)"; fi; }
pw() { aws ssm get-parameter --region "$REGION" --name "/smarthealth/$1" --with-decryption \
         --query Parameter.Value --output text; }
k() { /usr/local/bin/k3s kubectl "$@"; }
. /etc/smarthealth/boot.env
B=http://localhost
DEMO="$(pw demo-password)"; TOOLS="$(pw tools-password)"

app="$(k -n argocd get application smarthealth -o jsonpath='{.status.sync.status}/{.status.health.status} {.status.sync.revision}')"
case "$app" in Synced/Healthy*) ok "argo cd: $app";; *) bad "argo cd: $app";; esac
notready="$(k get pods -A --no-headers | awk '$4!="Running" && $4!="Completed"' | wc -l)"
want "every pod Running or Completed" 0 "$notready"
[ "$notready" = 0 ] || k get pods -A --no-headers | awk '$4!="Running" && $4!="Completed"'

want "/api/ready" 200 "$(curl -s -o /dev/null -w '%{http_code}' $B/api/ready)"
code="$(curl -s -o /tmp/login.json -w '%{http_code}' -H 'Content-Type: application/json' \
  -d "{\"email\":\"demo.desk1@medinova.example\",\"password\":\"$DEMO\"}" $B/api/auth/login)"
want "demo login" 200 "$code"
ADMIN_TOK="$(curl -s -H 'Content-Type: application/json' -d "{\"email\":\"demo.admin@medinova.example\",\"password\":\"$DEMO\"}" $B/api/auth/login | python3 -c 'import json,sys;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)"
HEALTH="$(curl -s -H "Authorization: Bearer $ADMIN_TOK" "$B/api/monitoring/overview?hours=1")"
want "health page sees every component" "ok" "$(printf '%s' "$HEALTH" | python3 -c '
import json, sys
o = json.load(sys.stdin)
down = [c["name"] for c in o["components"] if c["status"] != "ok"]
print("ok" if o["sources"]["prometheus"] == "ok" and not down else "prometheus=%s down=%s" % (o["sources"]["prometheus"], down))' 2>&1)"
TOK="$(python3 -c 'import json;print(json.load(open("/tmp/login.json")).get("access_token",""))' 2>/dev/null)"
rm -f /tmp/login.json
n="$(curl -s -H "Authorization: Bearer $TOK" "$B/api/appointments?page_size=1" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("total",0))' 2>/dev/null)"
if [ "${n:-0}" -gt 0 ] 2>/dev/null; then ok "appointments listed ($n)"; else bad "appointments listed ($n)"; fi
want "/api/.well-known/jwks.json" 200 "$(curl -s -o /dev/null -w '%{http_code}' $B/api/.well-known/jwks.json)"
want "frontend /" 200 "$(curl -s -o /dev/null -w '%{http_code}' $B/)"

for p in /grafana/ /jaeger/ /temporal/ /mailpit/ /prometheus/graph /argocd/; do
  want "$p without login" 401 "$(curl -s -o /dev/null -w '%{http_code}' $B$p)"
  want "$p with login" 200 "$(curl -s -o /dev/null -w '%{http_code}' -u "reviewer:$TOOLS" $B$p)"
done
# RabbitMQ: guarded by its own login (it and the tools login share the Authorization header).
RABBIT="$(pw rabbitmq-password)"
want "/rabbitmq/ page opens" 200 "$(curl -s -o /dev/null -w '%{http_code}' $B/rabbitmq/)"
want "/rabbitmq/api without login" 401 "$(curl -s -o /dev/null -w '%{http_code}' $B/rabbitmq/api/overview)"
want "/rabbitmq/api with the tools login" 401 "$(curl -s -o /dev/null -w '%{http_code}' -u "reviewer:$TOOLS" $B/rabbitmq/api/overview)"
want "/rabbitmq/api with its own login" 200 "$(curl -s -o /dev/null -w '%{http_code}' -u "smarthealth:$RABBIT" $B/rabbitmq/api/overview)"

down="$(curl -s -u "reviewer:$TOOLS" $B/prometheus/api/v1/targets | python3 -c 'import json,sys;t=json.load(sys.stdin)["data"]["activeTargets"];print(sum(x["health"]!="up" for x in t),len(t))')"
want "prometheus targets down (of total)" "0" "${down%% *}"; echo "      ${down#* } targets scraped"
# A real round trip, the path a reviewer clicks: book (Temporal saga, billing pre-check), wait
# for `confirmed`, cancel (outbox -> Kafka -> consumers -> Celery -> SMTP), see both emails.
# Mailpit keeps mail in memory, so a restart empties it -- count before and after.
mails() { curl -s -u "reviewer:$TOOLS" "$B/mailpit/api/v1/messages?limit=1" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("total",0))'; }
before="$(mails)"
trip="$(DEMO="$DEMO" python3 - 2>&1 <<'PY'
import json, os, random, time, urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime, timedelta, timezone
B = "http://localhost/api"
def call(method, path, body=None, token=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token: h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(B + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"FAIL {method} {path}: {e.code} {e.read()[:300]!r}")
_, login = call("POST", "/auth/login", {"email": "demo.desk1@medinova.example", "password": os.environ["DEMO"]})
tok = login["access_token"]
start = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
_, provs = call("GET", "/providers?limit=50", token=tok)
slots = []
for prov in random.sample(provs["items"], len(provs["items"])):
    slots = call("GET", f"/slots?provider_id={prov['id']}&limit=20&from={urllib.parse.quote(start)}", token=tok)[1]["items"]
    if slots: break
_, pats = call("GET", f"/patients?limit=50&offset={random.randrange(0, 700)}", token=tok)
for slot, pat in zip(random.sample(slots, min(5, len(slots))), random.sample(pats["items"], 5)):
    _, acc = call("POST", "/appointments", {"patient_id": pat["id"], "slot_id": slot["id"],
                  "reason": "automated deployment check"}, tok, {"Idempotency-Key": str(uuid.uuid4())})
    t0 = time.time(); status = acc.get("status")
    while status == "pending" and time.time() - t0 < 120:
        time.sleep(2); status = call("GET", f"/appointments/{acc['appointment_id']}", token=tok)[1]["status"]
    if status == "confirmed":
        took = time.time() - t0
        code, _ = call("POST", f"/appointments/{acc['appointment_id']}/cancel", {"reason": "automated deployment check"}, tok)
        print(f"ok booked and confirmed in {took:.1f}s, cancel answered {code}")
        break
    print(f"try: {status}")
else:
    print("FAIL no booking confirmed")
PY
)"
echo "$trip" | grep -v '^ok' | sed 's/^/      /'
if echo "$trip" | grep -q '^ok'; then ok "booking round trip: ${trip##*ok }"; else bad "booking round trip"; fi
for _ in $(seq 1 30); do [ "$(mails)" -ge $((before + 2)) ] && break; sleep 2; done
want "booking + cancel emails reached mailpit" "$((before + 2))" "$(mails)"

svcs=""
for _ in $(seq 1 12); do  # spans are exported in batches: give them a minute
  svcs="$(curl -s -u "reviewer:$TOOLS" $B/jaeger/api/services | python3 -c 'import json,sys;print(" ".join(sorted(json.load(sys.stdin)["data"] or [])))')"
  case "$svcs" in *part-a-api*part-a-worker*|*part-a-worker*part-a-api*) break;; esac; sleep 5
done
case "$svcs" in *part-a-api*part-a-worker*) ok "jaeger has traces from: $svcs";; *) bad "jaeger lacks part-a-api or part-a-worker traces ($svcs)";; esac

echo "$GRPC" | base64 -d > /tmp/grpcweb_smoke.py
out="$(k -n smarthealth exec -i deploy/assistant -- env PYTHONPATH=/srv/gen python - \
  --edge http://traefik.kube-system.svc.cluster.local --email demo.desk1@medinova.example \
  --password "$DEMO" < /tmp/grpcweb_smoke.py 2>&1)"
rm -f /tmp/grpcweb_smoke.py
if echo "$out" | grep -q '^grpc-status 0'; then ok "assistant over gRPC-Web ($(echo "$out" | grep -c '^[0-9]') events)"; else bad "assistant over gRPC-Web"; echo "$out" | tail -5; fi

disk="$(df --output=pcent / | tail -1 | tr -dc 0-9)"
if [ "$disk" -lt 85 ]; then ok "root disk ${disk}% used"; else bad "root disk ${disk}% used"; fi
avail="$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)"
if [ "$avail" -gt 300 ]; then ok "memory available ${avail} MiB"; else bad "memory available ${avail} MiB"; fi
[ "$fail" = 0 ] && echo "aws check: OK" || echo "aws check: FAILED"
REMOTE

result="$(ssm_run "$server" "GRPC='$grpc'
$remote")"
echo "$result"
case "$result" in *"aws check: OK"*) exit 0;; *) exit 1;; esac
