# AWS deployment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SmartHealth runs on one private EC2 k3s server in `ap-southeast-2`, deployed by Argo CD from this repo and reached through CloudFront, started and stopped with scripts and stopping itself after 3 hours.

**Architecture:** Terraform (`deploy/terraform/`) creates a VPC with a NAT instance and a private k3s server, SSM parameters for secrets, an S3 log bucket, and a CloudFront distribution whose origin is a VPC origin on the server. The server's user-data installs k3s, Argo CD (k3s HelmChart) and a boot unit that writes Kubernetes Secrets from SSM and applies the Argo CD Application. The Helm umbrella becomes Argo-buildable by moving `sh-lib` to the umbrella; demo data runs from suspended CronJobs.

**Tech Stack:** Terraform 1.10 (aws, random providers; S3 backend with native lock file), k3s v1.30.6+k3s1, Argo CD (argo-helm chart), CloudFront VPC origins, SSM Parameter Store, bash + Python scripts, pytest meta-tests.

Spec: `docs/superpowers/specs/2026-11-17-aws-deploy-design.md`.

---

## Ground rules the engineer must know

- **Branch** `feat/aws-deploy`. Commit only after the task's checks pass. LF line endings (Python on Windows: `open(..., newline="\n")`).
- **Nothing is created in AWS by any task except Task 10**, and Task 10 is run by the controller with the owner reviewing each `terraform plan`. Tasks 1–9 only write files and run local checks (`terraform fmt`, `terraform validate` with `-backend=false`, `helm template`, pytest, k3d).
- **Tools on this machine:** `helm`, `k3d`, `terraform` (1.10.5) in `C:/Users/Emumba/bin` (on PATH). The AWS CLI v2 is at `~/AppData/Local/Programs/Amazon/AWSCLIV2/aws.exe` and is NOT on Git Bash's PATH — scripts locate it via `scripts/aws_lib.sh` (Task 7). Profile: `smarthealth` (signed in with `aws login`; the Terraform provider may not read that credential cache, so `scripts/tf.sh` exports credentials with `aws configure export-credentials` first).
- **Never** put an account id, a password or a key in a committed file. The backend config (`deploy/terraform/backend.hcl`) is generated and git-ignored.
- **Docs gate:** `docs/doc-map.txt` maps `deploy/**` and `scripts/k3d_*`; Task 1 adds `scripts/aws_*`, `scripts/release.sh`, `scripts/tf.sh`. Until Task 9 writes docs, end sessions with `DOCS_WAIVE="aws deploy in progress; docs in Task 9"` if the docs hook fires.
- Lint: `python -m ruff check app tests services`. Fast suite: `python -m pytest -m "not docker" -q`.

## File structure

| Path | Responsibility |
| --- | --- |
| `deploy/helm/**` | Chart changes: `sh-lib` on the umbrella, demo CronJobs, `values-aws.yaml` |
| `deploy/terraform/bootstrap/main.tf` | The state bucket (local state, applied once) |
| `deploy/terraform/{versions,variables,network,nat,server,iam,ssm,cloudfront,logs,outputs}.tf` | The stack |
| `deploy/terraform/templates/server-user-data.sh.tftpl`, `nat-user-data.sh.tftpl` | First-boot scripts; they install systemd units that run on every boot |
| `deploy/terraform/templates/smarthealth-boot.sh` | The server's every-boot script (Secrets from SSM, Argo CD Application, auto-stop) |
| `scripts/aws_lib.sh` | Shared bash: find `aws`, read Terraform outputs, SSM run-command helper |
| `scripts/tf.sh`, `aws_init.sh`, `aws_secrets.sh`, `aws_start.sh`, `aws_stop.sh`, `aws_extend.sh`, `aws_seed.sh`, `aws_shell.sh`, `release.sh` | Operations |
| `scripts/aws_smoke.py` | HTTP smoke through CloudFront |
| `tests/meta/test_helm_charts.py` | + umbrella-only dependency, CronJobs, `values-aws.yaml` |
| `tests/meta/test_terraform.py` | `fmt`/`validate`, user-data invariants, script syntax |

---

### Task 1: `sh-lib` on the umbrella

**Files:**
- Modify: `deploy/helm/charts/*/Chart.yaml` (21 files), `deploy/helm/smarthealth/Chart.yaml`, `tests/meta/test_helm_charts.py`, `scripts/k3d_up.sh`, `docs/doc-map.txt`

- [ ] **Step 1: Write the failing test**

Append to `tests/meta/test_helm_charts.py`:
```python
def test_only_the_umbrella_declares_dependencies():
    """Argo CD runs `helm dependency build` on the umbrella only (one level), so a component
    chart with its own dependency would not build there (spec A-2)."""
    offenders = []
    for chart in sorted((HELM_DIR / "charts").iterdir()):
        meta = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8"))
        if meta.get("dependencies"):
            offenders.append(chart.name)
    assert not offenders, f"component charts with their own dependencies: {offenders}"
    umbrella = yaml.safe_load((UMBRELLA / "Chart.yaml").read_text(encoding="utf-8"))
    assert "sh-lib" in {d["name"] for d in umbrella["dependencies"]}
```

- [ ] **Step 2: Run it to verify it fails** — `python -m pytest tests/meta/test_helm_charts.py -q -k only_the_umbrella` → FAIL listing 21 charts.

- [ ] **Step 3: Move the dependency**

In every `deploy/helm/charts/<name>/Chart.yaml` delete the whole block:
```yaml
dependencies:
  - name: sh-lib
    version: 0.1.0
    repository: file://../../sh-lib
```
In `deploy/helm/smarthealth/Chart.yaml` add as the FIRST dependency:
```yaml
  - {name: sh-lib, version: 0.1.0, repository: file://../sh-lib}
```
Remove stale packaged copies: delete every `deploy/helm/charts/*/charts/` directory (they are git-ignored build output).

Helm parses the templates of a chart and all its subcharts into one template set, so `{{ include "sh-lib.component" . }}` in a component chart resolves to the umbrella's `sh-lib` dependency.

- [ ] **Step 4: Simplify the harness and k3d_up.sh**

In `tests/meta/test_helm_charts.py` replace `build_dependencies` with:
```python
@lru_cache(maxsize=1)
def build_dependencies() -> None:
    """One `helm dependency build` of the umbrella -- exactly what Argo CD runs."""
    _helm("dependency", "update", UMBRELLA.as_posix())
```
In `scripts/k3d_up.sh` replace the per-chart loop and the umbrella line with:
```bash
helm dependency update deploy/helm/smarthealth >/dev/null
```

- [ ] **Step 5: Map the new scripts in the docs gate** — append to `docs/doc-map.txt`:
```
scripts/aws_*            -> README.md, docs/index.html
scripts/release.sh       -> README.md, docs/index.html
scripts/tf.sh            -> README.md, docs/index.html
```

- [ ] **Step 6: Verify** — `python -m pytest tests/meta -q` → all pass (every earlier render test now renders through the umbrella's single sh-lib); `helm lint deploy/helm/smarthealth -f deploy/helm/smarthealth/values-local.yaml` → 0 failed.

- [ ] **Step 7: Commit**

```bash
git add deploy/helm tests/meta/test_helm_charts.py scripts/k3d_up.sh docs/doc-map.txt
git commit -m "build(helm): sh-lib is the umbrella's dependency, so Argo CD can build the chart"
```

---

### Task 2: Demo data as suspended CronJobs

**Files:**
- Delete: `deploy/helm/smarthealth/templates/demo-seed-job.yaml`
- Create: `deploy/helm/smarthealth/templates/demo-cronjobs.yaml`
- Modify: `deploy/helm/smarthealth/values.yaml` (`demoSeed` block), `scripts/k3d_smoke.py`, `tests/meta/test_helm_charts.py`

- [ ] **Step 1: Replace the demo Job test**

Delete `test_the_demo_job_is_off_by_default_and_runs_the_cli_when_on` and add:
```python
@needs_helm
def test_demo_data_is_two_suspended_cronjobs():
    docs = render("values-local.yaml")
    assert of_kind(docs, "Job") == []
    crons = {c["metadata"]["name"]: c for c in of_kind(docs, "CronJob")}
    assert set(crons) == {"demo-seed", "demo-refresh"}
    for name, cron in crons.items():
        assert cron["spec"]["suspend"] is True
        assert "argocd.argoproj.io/hook" not in cron["metadata"].get("annotations", {})
        pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        cmd = pod["containers"][0]["command"]
        assert cmd[:4] == ["python", "-m", "app.cli", name]
        assert "--allow-demo-data" in cmd
        assert pod["initContainers"][0]["name"] == "wait-api"
```

- [ ] **Step 2: Run to verify it fails** → FAIL (no CronJob).

- [ ] **Step 3: The template**

`git rm deploy/helm/smarthealth/templates/demo-seed-job.yaml`, then:

```yaml
# deploy/helm/smarthealth/templates/demo-cronjobs.yaml
# Demo data (docs/superpowers/specs/2026-11-16-demo-seed-data-design.md). Two CronJobs that
# never run on a schedule (`suspend: true`): they are templates, started on demand with
#   kubectl -n smarthealth create job --from=cronjob/demo-refresh demo-refresh-$(date +%s)
# No Argo CD hook, so a sync never re-runs them (spec A-2 / section 6).
{{- $g := .Values.global }}
{{- range $mode := list "seed" "refresh" }}
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: demo-{{ $mode }}
spec:
  schedule: "0 0 31 2 *"
  suspend: true
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      backoffLimit: 0
      ttlSecondsAfterFinished: 604800
      template:
        spec:
          restartPolicy: Never
          enableServiceLinks: false
          {{- with $g.imagePullSecrets }}
          imagePullSecrets:
            {{- range . }}
            - name: {{ . }}
            {{- end }}
          {{- end }}
          initContainers:
            - name: wait-api
              image: busybox:1.36
              command: ["sh", "-c", "until nc -z app 8000; do echo waiting for app:8000; sleep 2; done"]
              resources: {requests: {memory: 8Mi, cpu: 5m}, limits: {memory: 16Mi}}
          containers:
            - name: demo
              image: {{ if $g.image.registry }}{{ $g.image.registry }}/{{ end }}smarthealth-core:{{ $g.image.tag }}
              imagePullPolicy: {{ $g.image.pullPolicy }}
              command: ["python", "-m", "app.cli", "demo-{{ $mode }}",
                        "--api-url", "http://app:8000", "--allow-demo-data",
                        "--timezone", {{ $.Values.demoSeed.timezone | quote }}]
              env:
                - {name: SMARTHEALTH_POSTGRES_PASSWORD, valueFrom: {secretKeyRef: {name: {{ $g.secretName }}, key: postgres-password}}}
                - {name: SMARTHEALTH_DEMO_PASSWORD, valueFrom: {secretKeyRef: {name: {{ $g.secretName }}, key: demo-password}}}
                - {name: SMARTHEALTH_ENVIRONMENT, value: docker}
                - {name: SMARTHEALTH_POSTGRES_HOST, value: postgres}
                - {name: SMARTHEALTH_POSTGRES_PORT, value: "5432"}
                - {name: SMARTHEALTH_POSTGRES_USER, value: smarthealth}
                - {name: SMARTHEALTH_POSTGRES_DB, value: smarthealth}
                - {name: SMARTHEALTH_MONGO_HOST, value: mongo}
                - {name: SMARTHEALTH_MONGO_PORT, value: "27017"}
                - {name: SMARTHEALTH_MONGO_DB, value: smarthealth}
                - {name: SMARTHEALTH_DOCUMENTS_PATH, value: /var/smarthealth/documents}
              resources: {requests: {memory: 192Mi, cpu: 50m}, limits: {memory: 384Mi}}
              volumeMounts:
                - {name: documents, mountPath: /var/smarthealth/documents}
          volumes:
            - name: documents
              persistentVolumeClaim: {claimName: core-api-documents}
{{- end }}
```
(`0 0 31 2 *` — 31 February — never fires even if someone unsuspends it.)

In umbrella `values.yaml` replace the `demoSeed` block with:
```yaml
# Demo data: two suspended CronJobs, started on demand (templates/demo-cronjobs.yaml).
demoSeed:
  timezone: Australia/Sydney
```
Update `templates/NOTES.txt`'s demo line to:
`Demo data: kubectl -n {{ .Release.Namespace }} create job --from=cronjob/demo-seed demo-seed-1`

- [ ] **Step 4: The k3d smoke**

Replace `demo_seed()` in `scripts/k3d_smoke.py` with:
```python
def demo_seed() -> None:
    step("demo-seed from its CronJob (takes several minutes)")
    name = f"demo-seed-{int(time.time())}"
    kubectl("create", "job", f"--from=cronjob/demo-seed", name)
    kubectl("wait", "--for=condition=complete", f"job/{name}", "--timeout=40m")
    print(kubectl("logs", f"job/{name}")[-1500:])
```

- [ ] **Step 5: Verify** — `python -m pytest tests/meta -q` → PASS; `python -m ruff check scripts/k3d_smoke.py` → clean.

- [ ] **Step 6: Commit**

```bash
git add -A deploy/helm/smarthealth scripts/k3d_smoke.py tests/meta/test_helm_charts.py
git commit -m "build(helm): demo data from two suspended CronJobs, started on demand"
```

---

### Task 3: `values-aws.yaml`

**Files:**
- Create: `deploy/helm/smarthealth/values-aws.yaml`
- Modify: `tests/meta/test_helm_charts.py`

- [ ] **Step 1: Write the failing test**

```python
@needs_helm
def test_the_aws_values_render_without_secrets_and_from_ghcr():
    docs = render("values-aws.yaml")
    assert of_kind(docs, "Secret") == []          # created on the server from SSM
    aws = yaml.safe_load((UMBRELLA / "values-aws.yaml").read_text(encoding="utf-8"))
    tag = aws["global"]["image"]["tag"]
    assert tag != "dev"
    for d in of_kind(docs, "Deployment"):
        for c in d["spec"]["template"]["spec"]["containers"]:
            if "smarthealth-" in c["image"]:
                assert c["image"].startswith("ghcr.io/sadainiqrar/smarthealth-"), c["image"]
                assert c["image"].endswith(f":{tag}")
    routes = _routes(docs)
    for path in ("/grafana", "/jaeger", "/temporal", "/mailpit", "/rabbitmq", "/prometheus"):
        assert "tools-auth" in routes[path][1], path
    assert aws["global"]["publicUrl"].startswith("https://")
    assert aws["global"]["publicUrl"].endswith(".cloudfront.net")
```

- [ ] **Step 2: Run to verify it fails** → FAIL (file missing).

- [ ] **Step 3: The values file**

```yaml
# deploy/helm/smarthealth/values-aws.yaml
# The AWS server (docs/superpowers/specs/2026-11-17-aws-deploy-design.md), applied by Argo CD
# on top of values.yaml. Nothing secret lives here: the server's boot unit creates
# smarthealth-secrets and smarthealth-tools-auth from SSM Parameter Store.
global:
  # The CloudFront address; written by scripts/release.sh from `terraform output`.
  publicUrl: https://d0000000000000.cloudfront.net
  image:
    registry: ghcr.io/sadainiqrar
    # The release; written by scripts/release.sh (the git SHA the images were built from).
    tag: unreleased
    pullPolicy: IfNotPresent
secrets:
  create: false
```

- [ ] **Step 4: Verify** — `python -m pytest tests/meta/test_helm_charts.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add deploy/helm/smarthealth/values-aws.yaml tests/meta/test_helm_charts.py
git commit -m "build(helm): values-aws.yaml -- GHCR images, CloudFront URL, secrets from the server"
```

---

### Task 4: Terraform bootstrap, providers, network and NAT instance

**Files:**
- Create: `deploy/terraform/bootstrap/main.tf`, `deploy/terraform/versions.tf`, `variables.tf`, `network.tf`, `nat.tf`, `templates/nat-user-data.sh.tftpl`
- Create: `tests/meta/test_terraform.py`
- Modify: `.gitignore`

- [ ] **Step 1: The failing test**

```python
# tests/meta/test_terraform.py
"""Static checks of deploy/terraform (spec 2026-11-17-aws-deploy-design.md section 8).
Nothing here talks to AWS: fmt and validate run with -backend=false. Skipped without
terraform on the path."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.meta

ROOT = Path(__file__).resolve().parents[2]
TF = ROOT / "deploy" / "terraform"
TERRAFORM = shutil.which("terraform")
needs_terraform = pytest.mark.skipif(TERRAFORM is None, reason="terraform is not installed")


def _tf(directory: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([TERRAFORM, f"-chdir={directory.as_posix()}", *args],
                          capture_output=True, text=True, check=False)


@needs_terraform
@pytest.mark.parametrize("directory", [TF, TF / "bootstrap"], ids=["stack", "bootstrap"])
def test_terraform_is_formatted_and_valid(directory):
    fmt = _tf(directory, "fmt", "-check", "-recursive")
    assert fmt.returncode == 0, f"terraform fmt would change:\n{fmt.stdout}"
    init = _tf(directory, "init", "-backend=false", "-input=false")
    assert init.returncode == 0, init.stderr
    valid = _tf(directory, "validate")
    assert valid.returncode == 0, valid.stderr


def test_everything_is_in_the_one_region():
    text = "\n".join(p.read_text(encoding="utf-8") for p in TF.rglob("*.tf"))
    regions = set(__import__("re").findall(r'"((?:us|eu|ap|ca|sa|me|af)-[a-z]+-\d)"', text))
    assert regions <= {"ap-southeast-2"}, regions
```

- [ ] **Step 2: Run to verify it fails** → FAIL (`terraform -chdir` on a missing directory).

- [ ] **Step 3: `.gitignore`** — append:
```
# Terraform: provider caches, local state of the bootstrap, generated backend config
deploy/terraform/**/.terraform/
deploy/terraform/**/*.tfstate
deploy/terraform/**/*.tfstate.*
deploy/terraform/backend.hcl
deploy/terraform/**/.terraform.lock.hcl.bak
```
(`.terraform.lock.hcl` IS committed — it pins provider checksums.)

- [ ] **Step 4: The bootstrap**

```hcl
# deploy/terraform/bootstrap/main.tf
# The S3 bucket that holds the stack's Terraform state (spec A-7). Applied once, with local
# state kept on the laptop (git-ignored); the stack itself then uses this bucket.
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.90" }
  }
}

provider "aws" {
  region = "ap-southeast-2"
  default_tags {
    tags = { Project = "smarthealth", ManagedBy = "terraform", Component = "state" }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "state" {
  bucket = "smarthealth-tfstate-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}
```

- [ ] **Step 5: Providers and variables**

```hcl
# deploy/terraform/versions.tf
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.90" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # bucket comes from backend.hcl (generated by scripts/aws_init.sh; it names the account).
  backend "s3" {
    key          = "smarthealth/terraform.tfstate"
    region       = "ap-southeast-2"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "smarthealth", ManagedBy = "terraform" }
  }
}
```

```hcl
# deploy/terraform/variables.tf
variable "region" {
  description = "The project's one Region (AWS new experience: everything lives here)."
  type        = string
  default     = "ap-southeast-2"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "server_instance_type" {
  description = "Largest free-tier-eligible type: 2 vCPU, 8 GiB."
  type        = string
  default     = "m7i-flex.large"
}

variable "nat_instance_type" {
  type    = string
  default = "t4g.micro"
}

variable "root_volume_gb" {
  type    = number
  default = 40
}

variable "autostop_minutes" {
  description = "The server stops itself this long after boot; the NAT instance 15 minutes later."
  type        = number
  default     = 180
}

variable "k3s_version" {
  description = "As proven on k3d (docs/superpowers/specs/2026-11-17-helm-charts-k3d-design.md)."
  type        = string
  default     = "v1.30.6+k3s1"
}

variable "argocd_chart_version" {
  description = "argo-cd chart from https://argoproj.github.io/argo-helm."
  type        = string
  default     = "7.7.11"
}

variable "repo_url" {
  type    = string
  default = "https://github.com/sadainiqrar/smarthealth.git"
}

variable "repo_revision" {
  type    = string
  default = "main"
}
```

- [ ] **Step 6: Network**

```hcl
# deploy/terraform/network.tf
# One AZ: a public subnet for the NAT instance, a private one for the server (spec section 4).
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  az = data.aws_availability_zones.available.names[0]
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "smarthealth" }
}

# Required by CloudFront VPC origins even though origin traffic never uses it.
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "smarthealth" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 0)
  availability_zone       = local.az
  map_public_ip_on_launch = true
  tags                    = { Name = "smarthealth-public" }
}

resource "aws_subnet" "private" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 1)
  availability_zone = local.az
  tags              = { Name = "smarthealth-private" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "smarthealth-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block           = "0.0.0.0/0"
    network_interface_id = aws_instance.nat.primary_network_interface_id
  }
  tags = { Name = "smarthealth-private" }
}

resource "aws_route_table_association" "private" {
  subnet_id      = aws_subnet.private.id
  route_table_id = aws_route_table.private.id
}
```

- [ ] **Step 7: The NAT instance**

```hcl
# deploy/terraform/nat.tf
# Outbound internet for the private server (spec A-1): a t4g.micro doing NAT, instead of a
# managed NAT Gateway (~$35/month idle). Started and stopped with the server.
data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_security_group" "nat" {
  name        = "smarthealth-nat"
  description = "NAT instance: anything from the private subnet, nothing from the internet"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "traffic to forward, from the private subnet"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [aws_subnet.private.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "nat" {
  ami                                  = data.aws_ssm_parameter.al2023_arm64.value
  instance_type                        = var.nat_instance_type
  subnet_id                            = aws_subnet.public.id
  vpc_security_group_ids               = [aws_security_group.nat.id]
  source_dest_check                    = false
  iam_instance_profile                 = aws_iam_instance_profile.ssm_only.name
  instance_initiated_shutdown_behavior = "stop"
  user_data = templatefile("${path.module}/templates/nat-user-data.sh.tftpl", {
    private_cidr     = aws_subnet.private.cidr_block
    autostop_minutes = var.autostop_minutes + 15
  })

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  lifecycle {
    ignore_changes = [ami]
  }

  tags = { Name = "smarthealth-nat" }
}
```

```bash
#!/bin/bash
# deploy/terraform/templates/nat-user-data.sh.tftpl -- first boot of the NAT instance.
# Installs a unit that, on EVERY boot, turns on forwarding + masquerade and schedules the
# auto-stop (user-data itself runs only once).
set -euo pipefail
cat >/usr/local/sbin/smarthealth-nat <<'EOF'
#!/bin/bash
set -euo pipefail
sysctl -w net.ipv4.ip_forward=1
iface="$(ip route show default | awk '{print $5; exit}')"
nft list table ip nat >/dev/null 2>&1 || nft add table ip nat
nft list chain ip nat postrouting >/dev/null 2>&1 || \
  nft add chain ip nat postrouting '{ type nat hook postrouting priority 100 ; }'
nft flush chain ip nat postrouting
nft add rule ip nat postrouting ip saddr ${private_cidr} oifname "$iface" masquerade
shutdown -h +${autostop_minutes} "smarthealth auto-stop"
EOF
chmod 0755 /usr/local/sbin/smarthealth-nat
cat >/etc/systemd/system/smarthealth-nat.service <<'EOF'
[Unit]
Description=SmartHealth NAT (forwarding, masquerade) and auto-stop
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/smarthealth-nat
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
dnf install -y nftables
systemctl daemon-reload
systemctl enable --now smarthealth-nat.service
```
Note: in a `.tftpl`, `$${x}` is a literal `${x}`; this script uses only `$iface` and `$(…)` (no braces) apart from the two template variables, so no escaping is needed. Keep it that way.

`aws_iam_instance_profile.ssm_only` is defined in Task 5 (`iam.tf`); for `terraform validate` to pass in this task, create `deploy/terraform/iam.tf` now with:
```hcl
# deploy/terraform/iam.tf
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# The NAT instance: Session Manager only.
resource "aws_iam_role" "ssm_only" {
  name               = "smarthealth-nat"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "ssm_only" {
  role       = aws_iam_role.ssm_only.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ssm_only" {
  name = "smarthealth-nat"
  role = aws_iam_role.ssm_only.name
}
```

- [ ] **Step 8: Verify** — `terraform -chdir=deploy/terraform fmt -recursive`, then `python -m pytest tests/meta/test_terraform.py -q` → PASS (init downloads providers into `.terraform/`, git-ignored). Commit the generated `deploy/terraform/.terraform.lock.hcl` and `deploy/terraform/bootstrap/.terraform.lock.hcl`.

- [ ] **Step 9: Commit**

```bash
git add .gitignore deploy/terraform tests/meta/test_terraform.py
git commit -m "build(aws): terraform state bucket, VPC with public and private subnets, NAT instance"
```

---

### Task 5: The server — IAM, SSM parameters, user-data, every-boot unit

**Files:**
- Create: `deploy/terraform/server.tf`, `ssm.tf`, `templates/server-user-data.sh.tftpl`, `templates/smarthealth-boot.sh`
- Modify: `deploy/terraform/iam.tf`, `tests/meta/test_terraform.py`

- [ ] **Step 1: The failing tests**

```python
USER_DATA = TF / "templates" / "server-user-data.sh.tftpl"
BOOT = TF / "templates" / "smarthealth-boot.sh"


def test_the_server_boot_scripts_pin_versions_and_hold_no_secrets():
    user_data = USER_DATA.read_text(encoding="utf-8")
    boot = BOOT.read_text(encoding="utf-8")
    assert "INSTALL_K3S_VERSION=${k3s_version}" in user_data
    assert "targetRevision: ${repo_revision}" in user_data
    assert 'version: "${argocd_chart_version}"' in user_data
    assert "shutdown -h +" in boot
    assert "get-parameters-by-path" in boot
    for text in (user_data, boot):
        lowered = text.lower()
        assert "password=" not in lowered.replace("$", ""), "literal password in a boot script"
        assert "begin private key" not in lowered


def test_bash_scripts_parse():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    scripts = [BOOT, *sorted((ROOT / "scripts").glob("*.sh"))]
    for script in scripts:
        result = subprocess.run([bash, "-n", script.as_posix()], capture_output=True,
                                text=True, check=False)
        assert result.returncode == 0, f"{script.name}: {result.stderr}"
```

- [ ] **Step 2: Run to verify they fail** → FAIL (files missing).

- [ ] **Step 3: SSM parameters**

```hcl
# deploy/terraform/ssm.tf
# Generated secrets (spec section 4). The JWT signing key and the Groq key are NOT here:
# scripts/aws_secrets.sh puts them, so they never enter Terraform state.
locals {
  generated = toset([
    "postgres-password", "knowledge-db-password", "assistant-db-password",
    "rabbitmq-password", "grafana-admin-password", "demo-password", "tools-password",
    "argocd-admin-password",
  ])
}

resource "random_password" "secret" {
  for_each = local.generated
  length   = 24
  special  = false
}

resource "aws_ssm_parameter" "secret" {
  for_each = local.generated
  name     = "/smarthealth/${each.key}"
  type     = "SecureString"
  value    = random_password.secret[each.key].result
}
```

- [ ] **Step 4: The server's IAM** — append to `deploy/terraform/iam.tf`:

```hcl
data "aws_caller_identity" "current" {}

# The server: Session Manager, plus reading its own parameters (spec section 4).
resource "aws_iam_role" "server" {
  name               = "smarthealth-server"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "server_ssm" {
  role       = aws_iam_role.server.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "server_parameters" {
  statement {
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [
      "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/smarthealth",
      "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/smarthealth/*",
    ]
  }
}

resource "aws_iam_role_policy" "server_parameters" {
  name   = "read-smarthealth-parameters"
  role   = aws_iam_role.server.id
  policy = data.aws_iam_policy_document.server_parameters.json
}

resource "aws_iam_instance_profile" "server" {
  name = "smarthealth-server"
  role = aws_iam_role.server.name
}
```
(SecureString parameters use the AWS-managed `aws/ssm` key, whose key policy already lets principals in the account decrypt through SSM — no extra KMS statement is needed.)

- [ ] **Step 5: The every-boot script**

```bash
#!/bin/bash
# deploy/terraform/templates/smarthealth-boot.sh -- runs on EVERY boot of the server
# (smarthealth-boot.service, after k3s). Copied verbatim by user-data; reads its settings
# from /etc/smarthealth/boot.env. Idempotent.
#  1. schedule the auto-stop;  2. wait for the API server;
#  3. write the Secrets from SSM;  4. set Argo CD's admin password;
#  5. apply the Argo CD Application and the /argocd ingress once Argo CD's CRDs exist.
set -euo pipefail
# shellcheck disable=SC1091
. /etc/smarthealth/boot.env   # REGION, AUTOSTOP_MINUTES
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl() { /usr/local/bin/k3s kubectl "$@"; }

shutdown -h "+$AUTOSTOP_MINUTES" "smarthealth auto-stop" || true

until kubectl get --raw=/readyz >/dev/null 2>&1; do sleep 3; done

params=""
until params="$(aws ssm get-parameters-by-path --region "$REGION" --path /smarthealth \
                  --with-decryption --output json 2>/dev/null)"; do
  echo "waiting for SSM..."; sleep 10
done
value() {  # value <name> -> the parameter's value, or "" when it does not exist
  printf '%s' "$params" | python3 -c '
import json, sys
name = "/smarthealth/" + sys.argv[1]
for p in json.load(sys.stdin)["Parameters"]:
    if p["Name"] == name:
        print(p["Value"], end="")' "$1"
}

for ns in smarthealth argocd; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done

kubectl -n smarthealth create secret generic smarthealth-secrets \
  --from-literal=postgres-password="$(value postgres-password)" \
  --from-literal=knowledge-db-password="$(value knowledge-db-password)" \
  --from-literal=assistant-db-password="$(value assistant-db-password)" \
  --from-literal=rabbitmq-password="$(value rabbitmq-password)" \
  --from-literal=grafana-admin-password="$(value grafana-admin-password)" \
  --from-literal=demo-password="$(value demo-password)" \
  --from-literal=jwt-private-key="$(value jwt-private-key)" \
  --from-literal=llm-api-key="$(value llm-api-key)" \
  --dry-run=client -o yaml | kubectl apply -f -

users="$(htpasswd -nbB reviewer "$(value tools-password)" | head -n1)"
for ns in smarthealth argocd; do
  kubectl -n "$ns" create secret generic smarthealth-tools-auth \
    --from-literal=users="$users" --dry-run=client -o yaml | kubectl apply -f -
done

until kubectl get crd applications.argoproj.io >/dev/null 2>&1; do
  echo "waiting for Argo CD..."; sleep 5
done
until kubectl -n argocd get secret argocd-secret >/dev/null 2>&1; do sleep 5; done
hash="$(htpasswd -nbBC 10 "" "$(value argocd-admin-password)" | tr -d ':\n')"
kubectl -n argocd patch secret argocd-secret --type merge -p \
  "{\"stringData\":{\"admin.password\":\"$hash\",\"admin.passwordMtime\":\"$(date -u +%FT%TZ)\"}}"

kubectl apply -f /etc/smarthealth/argocd-extra.yaml
echo "smarthealth boot: done"
```

- [ ] **Step 6: The user-data template**

```bash
#!/bin/bash
# deploy/terraform/templates/server-user-data.sh.tftpl -- FIRST boot of the k3s server.
# Installs k3s, Argo CD (as a k3s HelmChart), and smarthealth-boot.service, which does the
# every-boot work (deploy/terraform/templates/smarthealth-boot.sh).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -y
apt-get install -y apache2-utils python3
snap install aws-cli --classic

mkdir -p /etc/smarthealth /var/lib/rancher/k3s/server/manifests
cat >/etc/smarthealth/boot.env <<'EOF'
REGION=${region}
AUTOSTOP_MINUTES=${autostop_minutes}
EOF

# Argo CD, applied by k3s itself (no helm on the server). Served under /argocd over HTTP:
# TLS ends at CloudFront. Anonymous users are read-only; they have already passed the
# tool login at the ingress.
cat >/var/lib/rancher/k3s/server/manifests/argocd.yaml <<'EOF'
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: argocd
  namespace: kube-system
spec:
  repo: https://argoproj.github.io/argo-helm
  chart: argo-cd
  version: "${argocd_chart_version}"
  targetNamespace: argocd
  createNamespace: true
  valuesContent: |-
    dex: {enabled: false}
    notifications: {enabled: false}
    applicationSet: {enabled: false}
    configs:
      params:
        server.insecure: true
        server.basehref: /argocd
        server.rootpath: /argocd
      cm:
        users.anonymous.enabled: "true"
        timeout.reconciliation: 180s
      rbac:
        policy.default: role:readonly
    controller:
      resources: {requests: {memory: 192Mi, cpu: 50m}, limits: {memory: 384Mi}}
    repoServer:
      resources: {requests: {memory: 128Mi, cpu: 50m}, limits: {memory: 320Mi}}
    server:
      resources: {requests: {memory: 64Mi, cpu: 20m}, limits: {memory: 160Mi}}
    redis:
      resources: {requests: {memory: 32Mi, cpu: 10m}, limits: {memory: 96Mi}}
EOF

# Applied by smarthealth-boot once Argo CD's CRDs exist.
cat >/etc/smarthealth/argocd-extra.yaml <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: smarthealth
  namespace: argocd
spec:
  project: default
  source:
    repoURL: ${repo_url}
    targetRevision: ${repo_revision}
    path: deploy/helm/smarthealth
    helm:
      valueFiles: [values.yaml, values-aws.yaml]
  destination:
    server: https://kubernetes.default.svc
    namespace: smarthealth
  syncPolicy:
    automated: {prune: true, selfHeal: true}
    syncOptions: [CreateNamespace=true]
---
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: tools-auth
  namespace: argocd
spec:
  basicAuth:
    secret: smarthealth-tools-auth
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: argocd
  namespace: argocd
  annotations:
    traefik.ingress.kubernetes.io/router.middlewares: argocd-tools-auth@kubernetescrd
spec:
  ingressClassName: traefik
  rules:
    - http:
        paths:
          - path: /argocd
            pathType: Prefix
            backend:
              service:
                name: argocd-server
                port:
                  number: 80
EOF

cat >/usr/local/sbin/smarthealth-boot <<'BOOT'
${boot_script}
BOOT
chmod 0755 /usr/local/sbin/smarthealth-boot

cat >/etc/systemd/system/smarthealth-boot.service <<'EOF'
[Unit]
Description=SmartHealth: auto-stop, Secrets from SSM, Argo CD Application
After=k3s.service network-online.target
Wants=network-online.target
Requires=k3s.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/smarthealth-boot
RemainAfterExit=yes
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=${k3s_version} sh -s - server \
  --write-kubeconfig-mode 0640
systemctl daemon-reload
systemctl enable --now smarthealth-boot.service
```

Template escaping: inside this `.tftpl` every literal `${…}` must be written `$${…}`. The heredocs above use no braces except the template variables (`region`, `autostop_minutes`, `argocd_chart_version`, `repo_url`, `repo_revision`, `k3s_version`, `boot_script`). `boot_script` is the boot script's content, injected by `server.tf`; that script uses `"$1"`, `"$ns"`, `$(…)` but `${…}` nowhere — keep it so, and the meta-test's `bash -n` covers it.

- [ ] **Step 7: The server**

```hcl
# deploy/terraform/server.tf
data "aws_ssm_parameter" "ubuntu_2404" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "server" {
  name        = "smarthealth-server"
  description = "k3s server: HTTP from CloudFront only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Traefik, from CloudFront (VPC origin)"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "server" {
  ami                                  = data.aws_ssm_parameter.ubuntu_2404.value
  instance_type                        = var.server_instance_type
  subnet_id                            = aws_subnet.private.id
  vpc_security_group_ids               = [aws_security_group.server.id]
  iam_instance_profile                 = aws_iam_instance_profile.server.name
  instance_initiated_shutdown_behavior = "stop"
  user_data = templatefile("${path.module}/templates/server-user-data.sh.tftpl", {
    region               = var.region
    autostop_minutes     = var.autostop_minutes
    k3s_version          = var.k3s_version
    argocd_chart_version = var.argocd_chart_version
    repo_url             = var.repo_url
    repo_revision        = var.repo_revision
    boot_script          = file("${path.module}/templates/smarthealth-boot.sh")
  })

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = false # all data (local-path volumes) lives here
  }

  # The NAT route must exist before first boot downloads k3s.
  depends_on = [aws_route_table_association.private, aws_instance.nat]

  lifecycle {
    ignore_changes = [ami, user_data]
  }

  tags = { Name = "smarthealth-server" }
}
```
(`ignore_changes = [user_data]`: user-data only matters at first boot; editing the template must not replace a server holding data. To apply a changed template, taint the instance deliberately.)

- [ ] **Step 8: Verify** — `terraform -chdir=deploy/terraform fmt -recursive`; `python -m pytest tests/meta/test_terraform.py -q` → PASS.

- [ ] **Step 9: Commit**

```bash
git add deploy/terraform tests/meta/test_terraform.py
git commit -m "build(aws): private k3s server -- user-data installs k3s and Argo CD; every boot writes Secrets from SSM and schedules the auto-stop"
```

---

### Task 6: CloudFront and its logs

**Files:**
- Create: `deploy/terraform/cloudfront.tf`, `logs.tf`, `outputs.tf`
- Modify: `tests/meta/test_terraform.py`

- [ ] **Step 1: The failing test**

```python
def test_cloudfront_reaches_the_server_privately_and_never_caches_the_api():
    text = (TF / "cloudfront.tf").read_text(encoding="utf-8")
    assert 'resource "aws_cloudfront_vpc_origin"' in text
    assert "vpc_origin_config" in text
    assert 'viewer_protocol_policy = "redirect-to-https"' in text
    assert "Managed-CachingDisabled" in text
    assert 'path_pattern     = "/assets/*"' in text
    assert "logging_config" in text
```

- [ ] **Step 2: Run to verify it fails** → FAIL.

- [ ] **Step 3: Logs bucket**

```hcl
# deploy/terraform/logs.tf
# CloudFront standard logs (kept 14 days). Legacy standard logging writes with ACLs, so the
# bucket allows ACLs for the log-delivery grant CloudFront adds itself.
resource "aws_s3_bucket" "logs" {
  bucket_prefix = "smarthealth-cf-logs-"
  force_destroy = true
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule { object_ownership = "BucketOwnerPreferred" }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration { days = 14 }
  }
}
```

- [ ] **Step 4: CloudFront**

```hcl
# deploy/terraform/cloudfront.tf
# The one public entry (spec section 4): HTTPS to browsers, a VPC origin to the private
# server over AWS's network, no caching except hashed static assets.
data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

data "aws_cloudfront_response_headers_policy" "security" {
  name = "Managed-SecurityHeadersPolicy"
}

resource "aws_cloudfront_vpc_origin" "server" {
  vpc_origin_endpoint_config {
    name                   = "smarthealth-server"
    arn                    = aws_instance.server.arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"
    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
}

resource "aws_cloudfront_distribution" "main" {
  enabled         = true
  comment         = "SmartHealth"
  price_class     = "PriceClass_All"
  is_ipv6_enabled = true
  http_version    = "http2and3"

  origin {
    origin_id   = "server"
    domain_name = aws_instance.server.private_dns
    vpc_origin_config {
      vpc_origin_id            = aws_cloudfront_vpc_origin.server.id
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id           = "server"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  ordered_cache_behavior {
    path_pattern               = "/assets/*"
    target_origin_id           = "server"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  logging_config {
    bucket          = aws_s3_bucket.logs.bucket_domain_name
    prefix          = "cloudfront/"
    include_cookies = false
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  depends_on = [aws_s3_bucket_ownership_controls.logs]
}
```

- [ ] **Step 5: Outputs**

```hcl
# deploy/terraform/outputs.tf
output "cloudfront_url" {
  value = "https://${aws_cloudfront_distribution.main.domain_name}"
}

output "server_instance_id" {
  value = aws_instance.server.id
}

output "nat_instance_id" {
  value = aws_instance.nat.id
}

output "tools_password" {
  description = "Login for /grafana, /jaeger, /temporal, /mailpit, /rabbitmq, /prometheus, /argocd (user: reviewer)."
  value       = random_password.secret["tools-password"].result
  sensitive   = true
}

output "demo_password" {
  description = "Password of every demo login (README, Demo data)."
  value       = random_password.secret["demo-password"].result
  sensitive   = true
}

output "argocd_admin_password" {
  value     = random_password.secret["argocd-admin-password"].result
  sensitive = true
}
```

- [ ] **Step 6: Verify** — fmt, then `python -m pytest tests/meta/test_terraform.py -q` → PASS. If `terraform validate` rejects an attribute of `aws_cloudfront_vpc_origin` or `vpc_origin_config` (provider schema differences), read `terraform providers schema -json` for the exact names, fix, and report.

- [ ] **Step 7: Commit**

```bash
git add deploy/terraform tests/meta/test_terraform.py
git commit -m "build(aws): CloudFront with a VPC origin to the private server; logs to S3; outputs"
```

---

### Task 7: Operations scripts

**Files:**
- Create: `scripts/aws_lib.sh`, `scripts/tf.sh`, `scripts/aws_init.sh`, `scripts/aws_secrets.sh`, `scripts/aws_start.sh`, `scripts/aws_stop.sh`, `scripts/aws_extend.sh`, `scripts/aws_seed.sh`, `scripts/aws_shell.sh`, `scripts/release.sh`, `scripts/aws_smoke.py`

- [ ] **Step 1: The shared library**

```bash
#!/usr/bin/env bash
# scripts/aws_lib.sh -- sourced by the scripts/aws_*.sh helpers.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AWS_PROFILE="${AWS_PROFILE:-smarthealth}"
export AWS_REGION="${AWS_REGION:-ap-southeast-2}"
export PYTHONIOENCODING=utf-8   # the CLI fails on a non-UTF-8 Windows console otherwise

if command -v aws >/dev/null 2>&1; then
  AWS=aws
elif [ -x "$HOME/AppData/Local/Programs/Amazon/AWSCLIV2/aws.exe" ]; then
  AWS="$HOME/AppData/Local/Programs/Amazon/AWSCLIV2/aws.exe"
else
  echo "AWS CLI v2 not found (install it or put it on PATH)" >&2; exit 1
fi

# The stack state is in S3, so outputs need the exported credentials: go through tf.sh.
tf_output() { "$ROOT/scripts/tf.sh" output -raw "$1"; }

# ssm_run <instance-id> <shell command> -> runs it as root on the instance, prints stdout.
ssm_run() {
  local id
  id="$("$AWS" ssm send-command --instance-ids "$1" --document-name AWS-RunShellScript \
        --parameters "commands=[\"$2\"]" --query Command.CommandId --output text)"
  "$AWS" ssm wait command-executed --command-id "$id" --instance-id "$1" || true
  "$AWS" ssm get-command-invocation --command-id "$id" --instance-id "$1" \
    --query StandardOutputContent --output text
}
```

- [ ] **Step 2: Terraform wrapper and init**

```bash
#!/usr/bin/env bash
# scripts/tf.sh -- terraform with credentials from the `smarthealth` profile.
# `aws login` credentials live in a cache the Terraform provider may not read, so export
# them as environment variables first. Usage: scripts/tf.sh [bootstrap] <terraform args>
. "$(dirname "$0")/aws_lib.sh"
eval "$("$AWS" configure export-credentials --format env)"
dir="$ROOT/deploy/terraform"
if [ "${1:-}" = bootstrap ]; then dir="$dir/bootstrap"; shift; fi
exec terraform -chdir="$dir" "$@"
```

```bash
#!/usr/bin/env bash
# scripts/aws_init.sh -- write the git-ignored backend config and `terraform init` the stack.
# Run after `scripts/tf.sh bootstrap apply` has created the state bucket.
. "$(dirname "$0")/aws_lib.sh"
bucket="$(terraform -chdir="$ROOT/deploy/terraform/bootstrap" output -raw state_bucket)"
printf 'bucket = "%s"\n' "$bucket" > "$ROOT/deploy/terraform/backend.hcl"
"$ROOT/scripts/tf.sh" init -backend-config=backend.hcl -input=false
```

- [ ] **Step 3: Secrets the stack does not generate**

```bash
#!/usr/bin/env bash
# scripts/aws_secrets.sh -- put the two secrets Terraform must not hold (spec section 4):
# a fresh JWT signing key, and the Groq key from the git-ignored .env (if present).
# Re-run to rotate; then reboot the server (or wait for the next start).
. "$(dirname "$0")/aws_lib.sh"
key="$(cd "$ROOT" && python -m app.cli generate-signing-key)"
"$AWS" ssm put-parameter --name /smarthealth/jwt-private-key --type SecureString \
  --value "$key" --overwrite >/dev/null
echo "jwt-private-key: set"
llm="$(grep -E '^ASSISTANT_LLM_API_KEY=' "$ROOT/.env" 2>/dev/null | head -n1 | cut -d= -f2- || true)"
if [ -n "$llm" ]; then
  "$AWS" ssm put-parameter --name /smarthealth/llm-api-key --type SecureString \
    --value "$llm" --overwrite >/dev/null
  echo "llm-api-key: set from .env"
else
  echo "llm-api-key: not in .env -- the assistant runs offline"
fi
```

- [ ] **Step 4: Start, stop, extend, seed, shell**

```bash
#!/usr/bin/env bash
# scripts/aws_start.sh [--refresh] -- start the NAT instance and the server, wait until the
# app answers through CloudFront, optionally refresh the demo data.
. "$(dirname "$0")/aws_lib.sh"
nat="$(tf_output nat_instance_id)"; server="$(tf_output server_instance_id)"
url="$(tf_output cloudfront_url)"
"$AWS" ec2 start-instances --instance-ids "$nat" >/dev/null
"$AWS" ec2 wait instance-running --instance-ids "$nat"
"$AWS" ec2 start-instances --instance-ids "$server" >/dev/null
"$AWS" ec2 wait instance-running --instance-ids "$server"
echo "instances running; waiting for $url/api/ready (up to 15 min)..."
for _ in $(seq 1 90); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "$url/api/ready")" = 200 ]; then
    if [ "${1:-}" = --refresh ]; then
      ssm_run "$server" "k3s kubectl -n smarthealth create job --from=cronjob/demo-refresh demo-refresh-\$(date +%s)"
      echo "demo refresh started (watch: scripts/aws_shell.sh, kubectl -n smarthealth get jobs)"
    fi
    echo "SmartHealth is up: $url/  (tools login: reviewer / scripts/tf.sh output -raw tools_password)"
    echo "It stops itself in about 3 hours; scripts/aws_extend.sh adds 3 hours."
    exit 0
  fi
  sleep 10
done
echo "not ready after 15 minutes; last boot log:" >&2
ssm_run "$server" "journalctl -u smarthealth-boot -u k3s --no-pager -n 60" >&2
exit 1
```

```bash
#!/usr/bin/env bash
# scripts/aws_stop.sh -- stop the server, then the NAT instance. Data stays on the disk.
. "$(dirname "$0")/aws_lib.sh"
"$AWS" ec2 stop-instances --instance-ids "$(tf_output server_instance_id)" >/dev/null
"$AWS" ec2 stop-instances --instance-ids "$(tf_output nat_instance_id)" >/dev/null
echo "stopping (disks are kept; about \$4.60 a month while stopped)"
```

```bash
#!/usr/bin/env bash
# scripts/aws_extend.sh -- push both auto-stops 3 hours (+15 min for NAT) from now.
. "$(dirname "$0")/aws_lib.sh"
ssm_run "$(tf_output server_instance_id)" "shutdown -c; shutdown -h +180 'smarthealth auto-stop'"
ssm_run "$(tf_output nat_instance_id)" "shutdown -c; shutdown -h +195 'smarthealth auto-stop'"
echo "auto-stop moved to 3 hours from now"
```

```bash
#!/usr/bin/env bash
# scripts/aws_seed.sh -- first time only: seed the demo data once Argo CD has synced.
. "$(dirname "$0")/aws_lib.sh"
ssm_run "$(tf_output server_instance_id)" \
  "k3s kubectl -n smarthealth create job --from=cronjob/demo-seed demo-seed-\$(date +%s)"
echo "demo seed started; it takes several minutes (scripts/aws_shell.sh to watch)"
```

```bash
#!/usr/bin/env bash
# scripts/aws_shell.sh -- a root shell on the server through Session Manager
# (needs the Session Manager plugin for the AWS CLI). kubectl there: `k3s kubectl`.
. "$(dirname "$0")/aws_lib.sh"
exec "$AWS" ssm start-session --target "$(tf_output server_instance_id)"
```

- [ ] **Step 5: Release**

```bash
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
python - "$ROOT/deploy/helm/smarthealth/values-aws.yaml" "$tag" "$url" <<'PY'
import re, sys
path, tag, url = sys.argv[1:4]
text = open(path, encoding="utf-8").read()
text = re.sub(r"(?m)^(    tag: ).*$", r"\g<1>" + tag, text)
if url:
    text = re.sub(r"(?m)^(  publicUrl: ).*$", r"\g<1>" + url, text)
open(path, "w", encoding="utf-8", newline="\n").write(text)
PY
git -C "$ROOT" add deploy/helm/smarthealth/values-aws.yaml
git -C "$ROOT" commit -m "release: $tag"
echo "committed release $tag -- fast-forward main and push; Argo CD syncs within 3 minutes"
```

- [ ] **Step 6: The AWS smoke**

```python
# scripts/aws_smoke.py
"""Smoke the AWS deployment through CloudFront, HTTP only (spec section 7).

Reads the URL and passwords from `scripts/tf.sh output`. Checks: the API is ready, a demo
login works, gRPC-Web streams, every tool path and /argocd answer 401 without the login and
200 with it, hashed assets are cached and the API is not.
"""

from __future__ import annotations

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
    if "miss" not in r.headers.get("x-cache", "").lower() and \
            "refreshhit" not in r.headers.get("x-cache", "").lower():
        print(f"   note: x-cache={r.headers.get('x-cache')}")

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
    asset = next((part.split('"')[0] for part in index.split('src="')[1:]
                  if part.startswith("/assets/")), None)
    if asset:
        client.get(f"{base}{asset}")
        cache = client.get(f"{base}{asset}").headers.get("x-cache", "")
        if "hit" not in cache.lower():
            raise SystemExit(f"{asset} not cached (x-cache={cache})")
    api_cache = client.get(f"{base}/api/health").headers.get("x-cache", "")
    if "hit" in api_cache.lower():
        raise SystemExit(f"/api/health was served from cache ({api_cache})")

    print(f"aws smoke: OK -- {base}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: Verify** — `chmod +x scripts/*.sh`; `git update-index --chmod=+x scripts/aws_*.sh scripts/tf.sh scripts/release.sh`; `python -m pytest tests/meta/test_terraform.py -q` (bash -n over every script) → PASS; `python -m ruff check scripts/aws_smoke.py` → clean. `bash scripts/tf.sh bootstrap version` must print the Terraform version (proves credential export works) — it touches no AWS resource.

- [ ] **Step 8: Commit**

```bash
git add scripts tests/meta/test_terraform.py
git commit -m "build(aws): start/stop/extend/seed/shell/release scripts and an HTTP smoke through CloudFront"
```

---

### Task 8: Re-prove the charts on k3d

**Files:** (none expected; fixes go where the failure is)

- [ ] **Step 1:** The k3d cluster `smarthealth` may still be running from the Helm work. Run `bash scripts/k3d_up.sh` (idempotent: it rebuilds images, `helm dependency update` on the umbrella only, `helm upgrade`).
- [ ] **Step 2:** Helm will delete the old demo Job resources (none exist) and add the two CronJobs. Run `python scripts/k3d_smoke.py` (full, including the demo seed through `kubectl create job --from=cronjob/demo-seed`). Expected: `k3d smoke: OK`.
- [ ] **Step 3:** If anything fails, fix the chart, re-run, and commit as `fix(helm): …`. If the cluster no longer exists, `k3d_up.sh` creates it (stop the compose stacks first).

---

### Task 9: Documentation

**Files:** `README.md`, `docs/index.html`, `docs/ARCHITECTURE.md`, `docs/PRD.md`, `CLAUDE.md`

- [ ] **Step 1: README** — a "Deploying to AWS" section after "Kubernetes (k3d)": what it is (diagram from spec §4 in text form), prerequisites (AWS CLI v2 with the `smarthealth` profile, Session Manager plugin, terraform 1.10, docker login to ghcr.io), first deploy (spec §7's five steps with the exact commands: `scripts/tf.sh bootstrap init`, `scripts/tf.sh bootstrap apply`, `scripts/aws_init.sh`, `scripts/tf.sh plan -out=tfplan`, `scripts/tf.sh apply tfplan`, `scripts/aws_secrets.sh`, make the five GHCR packages public in GitHub → Packages → Package settings, `scripts/release.sh`, push `main`, `scripts/aws_start.sh`, `scripts/aws_seed.sh`, `python scripts/aws_smoke.py`), the review routine (`aws_start.sh --refresh` … `aws_stop.sh`), cost table, teardown (`scripts/tf.sh destroy`, then the bootstrap; note the server's root volume is kept on termination and must be deleted by hand), where the passwords come from (`scripts/tf.sh output -raw tools_password` / `demo_password` / `argocd_admin_password`).
- [ ] **Step 2: Site** — in the Deployment section of `docs/index.html` add an "On AWS" subsection: a Mermaid flowchart (browser → CloudFront → VPC origin → k3s server (Traefik → pods); server → NAT instance → internet (GHCR, GitHub, Groq); SSM → server; Argo CD ← GitHub), the scripts table, cost. No `;` in labels.
- [ ] **Step 3: ARCHITECTURE** — "8. Deployment on AWS": network, why a VPC origin + NAT instance, the boot sequence, GitOps flow, secrets, auto-stop, what survives a stop.
- [ ] **Step 4: PRD** — §8 decisions A-1…A-7 as the next free D- ids (read the highest; D-41 was last as of 2026-11-17), each with its rejected alternative; §9 a new risk: no backups — the server's root volume holds all data; a snapshot schedule (Data Lifecycle Manager) is the fix if the demo must survive a lost volume. Disposition: accepted for a demo.
- [ ] **Step 5: CLAUDE.md** — commands rows: `scripts/tf.sh …`, `scripts/aws_start.sh [--refresh]` / `aws_stop.sh` / `aws_extend.sh`, `scripts/release.sh`, `python scripts/aws_smoke.py`; one line in the status paragraph: "Deployment: k3d locally, AWS via Terraform + Argo CD (spec 2026-11-17-aws-deploy-design.md)". Preserve CLAUDE.md's line endings.
- [ ] **Step 6: Verify** — `python scripts/gen_docs_data.py --check`; `python -m pytest -m "not docker" -q`; `python -m ruff check app tests services`.
- [ ] **Step 7: Commit** — `git commit -m "docs: deploying to AWS -- network, boot, GitOps, scripts, cost, decisions"`.

---

### Task 10: First deploy (controller + owner; touches AWS)

Run by the controller session, not a subagent. **Every `apply` waits for the owner's explicit approval of the shown plan.**

- [ ] **Step 1: State bucket** — `bash scripts/tf.sh bootstrap init`; `bash scripts/tf.sh bootstrap plan -out=bootstrap.tfplan`; show the owner the plan (4 resources); on approval `bash scripts/tf.sh bootstrap apply bootstrap.tfplan`.
- [ ] **Step 2: Stack plan** — `bash scripts/aws_init.sh`; `bash scripts/tf.sh plan -out=tfplan`; walk the owner through every resource (VPC, subnets, IGW, routes, NAT instance + SG + role, server + SG + role + policy, 8 SSM parameters, log bucket and its settings, VPC origin, distribution). On approval `bash scripts/tf.sh apply tfplan`. Note: the instances start running at creation — the server's first boot installs k3s and Argo CD (~10 min).
- [ ] **Step 3: Secrets and release** — `bash scripts/aws_secrets.sh`; ask the owner to `docker login ghcr.io` and confirm; merge the branch to `main` per the owner's choice; `bash scripts/release.sh` on `main`; ask the owner to make the five GHCR packages public; push `main` (owner approval).
- [ ] **Step 4: Bring up** — `bash scripts/aws_start.sh` (instances may already be running); then reboot the server once if it booted before `aws_secrets.sh` ran (`ssm_run … reboot` or stop/start) so the JWT and Groq keys are in its Secret; wait for Argo CD to show Synced/Healthy (`/argocd`); `bash scripts/aws_seed.sh`; `python scripts/aws_smoke.py` → `aws smoke: OK`.
- [ ] **Step 5: Hand-over** — show the URLs and where the passwords come from; ask the owner (AWS rule) whether to keep the resources (and stop them now: `scripts/aws_stop.sh`) or destroy them.
