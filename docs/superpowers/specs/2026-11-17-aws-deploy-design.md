# AWS deployment — Terraform, k3s, Argo CD, CloudFront — design

**Date:** 2026-11-17 · **Status:** approved in conversation, awaiting written-spec review
**Part of:** deploying SmartHealth for assignment review, project 3 of 3 — (1) demo seed data
(`2026-11-16-demo-seed-data-design.md`, done), (2) Helm charts proven on k3d
(`2026-11-17-helm-charts-k3d-design.md`, done), (3) **this document**.

## 1. Goal

A reviewer opens `https://<id>.cloudfront.net/` from anywhere and finds the whole system
running — the app, the assistant, and the tools (Grafana, Jaeger, Temporal UI, Mailpit,
RabbitMQ, Prometheus, Argo CD) behind one login — with months of demo history. The owner
starts it with one command before a review (once or twice a week, a couple of hours) and it
stops itself; it costs about $11 a month on the Free plan's $100 credits.

## 2. Constraints

- AWS new experience, **Free plan**, `ap-southeast-2` only; free-tier-eligible instance types
  only (`m7i-flex.large` is the largest). Checked 2026-11-17: plan `FREE`, $100 credits,
  only the default VPC exists. MSK, DocumentDB, Managed Grafana unavailable — irrelevant:
  everything runs in k3s.
- CloudFront is global and allowed; VPC origins are supported in `ap-southeast-2`.
- **Nothing is applied in AWS until the owner has reviewed a `terraform plan`.** At the end
  the owner decides whether to keep or destroy the resources.
- The GitHub repo `sadainiqrar/smarthealth` is public; the five GHCR images are made public.

## 3. Decisions (with what was rejected)

| # | Decision | Rejected |
| --- | --- | --- |
| A-1 | **Private server behind a CloudFront VPC origin**; outbound internet through a **NAT instance** (`t4g.micro`) started and stopped with the server | Public server locked to the CloudFront prefix list (the CloudFront→server hop would cross the internet in clear text: passwords, tokens, the tool login). Public server with HTTPS (needs a domain and a permanent Elastic IP). Managed NAT Gateway (~$35/month even idle) |
| A-2 | **`sh-lib` is a dependency of the umbrella only**; component charts use its templates (Helm named templates are global across a chart and its subcharts). Argo CD reads the chart straight from git | OCI charts on GHCR (two artifacts per release, chart versioning, git no longer the whole truth). An Argo CD config-management plugin (fragile, custom sidecar) |
| A-3 | **Public GHCR images**, no pull secret | Private images with a GHCR token in SSM (another secret to rotate) |
| A-4 | **Argo CD UI at `/argocd`**, behind the tool login, anonymous = read-only | Port-forward only (reviewers could not see GitOps) |
| A-5 | **Auto-stop on the instance** (systemd timer, `shutdown -h`, instance-initiated shutdown = stop) | EventBridge Scheduler + Lambda (more resources; a Lambda only to stop one instance) |
| A-6 | **No AWS WAF** | WAF with managed rules (~$6+/month minimum; CloudFront's built-in Shield Standard covers a demo) |
| A-7 | **Terraform state in S3** (encrypted, versioned, native lock file) created by a tiny `bootstrap/` | Local state (lost with the laptop; holds generated secrets) |

## 4. AWS resources (`deploy/terraform/`)

```
browser ──HTTPS──► CloudFront (d….cloudfront.net)
                       │ VPC origin — AWS private network, HTTP :80
VPC 10.20.0.0/16, one AZ
  public subnet:  NAT instance t4g.micro (Amazon Linux 2023, iptables MASQUERADE,
                  source/dest check off, public IPv4) → internet gateway
  private subnet: k3s server m7i-flex.large, Ubuntu 24.04, no public IP,
                  40 GB gp3 encrypted root (delete_on_termination = false)
                  SG: TCP 80 from the CloudFront origin-facing managed prefix list only
SSM Parameter Store /smarthealth/* (SecureString) · S3: CloudFront logs (14-day lifecycle)
```

- **Provider** `hashicorp/aws` pinned, region `ap-southeast-2`; `hashicorp/random` for
  passwords. CloudFront and its VPC origin are created through the same provider (global
  service).
- **Server:** IMDSv2 required; no key pair, no SSH; IAM instance profile with
  `AmazonSSMManagedInstanceCore` plus `ssm:GetParameter(s)`/`GetParametersByPath` on
  `/smarthealth/*` and `kms:Decrypt` via the SSM default key;
  `instance_initiated_shutdown_behavior = "stop"`. Same for the NAT instance (SSM only).
- **Routing:** private subnet default route → the NAT instance's ENI.
- **CloudFront:** origin = VPC origin for the server (HTTP 80, origin read timeout 60 s,
  keep-alive 5 s); viewer protocol `redirect-to-https`; allowed methods all (POST for the
  API and gRPC-Web); default behavior cache policy `Managed-CachingDisabled` with origin
  request policy `Managed-AllViewer` (forwards `Authorization` for the tool login, and the
  bearer tokens); `/assets/*` with `Managed-CachingOptimized`; response headers policy
  `Managed-SecurityHeadersPolicy`; standard logging to the S3 log bucket; price class 100
  is NOT used (reviewers may be anywhere) — `PriceClass_All`. Managed policy ids are looked
  up by name (`data "aws_cloudfront_cache_policy"`), never hard-coded.
- **Secrets:** `random_password` for each Postgres password, RabbitMQ, Grafana admin, the
  tool login, the demo logins and the Argo CD admin; the JWT signing key is generated by
  `python -m app.cli generate-signing-key` once and put by `scripts/aws_secrets.sh`, as is the
  Groq key from the git-ignored `.env` — neither enters Terraform state. Terraform outputs
  the tool and demo passwords (sensitive) for the owner.
- **Tags:** `Project=smarthealth`, `ManagedBy=terraform` on everything.

**Cost (approximate, per month):** server + NAT at ~10 h/week ≈ $6; 48 GB EBS ≈ $4.60 (also
while stopped); public IPv4 while running ≈ $0.20; CloudFront, S3, SSM within free tiers.
**≈ $11/month → ~9 months of credits.**

## 5. Server bootstrap

`user-data` (a Terraform `templatefile`, kept in the repo) on first boot:
1. installs k3s **`v1.30.6+k3s1`** (as proven on k3d), keeping Traefik and local-path;
2. writes to `/var/lib/rancher/k3s/server/manifests/` (k3s applies these itself):
   - a `HelmChart` for Argo CD (chart version pinned): `server.basehref`/`rootpath`
     `/argocd`, `server.insecure: true` (TLS ends at CloudFront), anonymous access with the
     read-only role, admin password from SSM (bcrypt, set by the secrets unit);
   - the Argo CD `Application`: repo `https://github.com/sadainiqrar/smarthealth`, revision
     `main`, path `deploy/helm/smarthealth`, value files `values.yaml` + `values-aws.yaml`,
     namespace `smarthealth` (created), automated sync with prune and self-heal;
   - an `Ingress` + Traefik basic-auth `Middleware` for `/argocd` in namespace `argocd`;
3. installs `smarthealth-secrets.service` (oneshot, every boot, before workloads need it,
   retrying until SSM answers): reads `/smarthealth/*` and applies `smarthealth-secrets`
   (namespace `smarthealth`), `smarthealth-tools-auth` (namespaces `smarthealth` and
   `argocd`) and Argo CD's admin password;
4. installs `smarthealth-autostop.timer` → `shutdown -h now` 3 h after boot.
The NAT instance's user-data enables forwarding + MASQUERADE and the same timer at 3 h 15 m.

Every boot: k3s starts, Secrets are re-applied, Argo CD syncs whatever git says, the
auto-stop starts counting. Pods whose Secret is missing wait instead of starting with empty
passwords. Rotating a password: change SSM, reboot.

## 6. GitOps and chart changes

Release: `scripts/release.sh` → `push_images.sh --push ghcr.io/sadainiqrar` (tag = git SHA)
→ writes `global.image.tag` into `values-aws.yaml` → commits `release: <sha>`. The owner
fast-forwards `main` and pushes; Argo CD (polling every 3 min) syncs when the server runs.

Chart changes (each re-proven on k3d):
1. **`sh-lib` moves to the umbrella** (A-2): the 21 component charts drop their `sh-lib`
   dependency; the umbrella declares it. The test harness's `build_dependencies()` becomes
   one `helm dependency build` of the umbrella — what Argo CD does. A meta-test fails if a
   component chart declares a dependency.
2. **`values-aws.yaml`**: `global.publicUrl` (the CloudFront URL, from the Terraform
   output), `global.image.registry: ghcr.io/sadainiqrar`, `global.image.tag`,
   `secrets.create: false`. Meta-test: rendering with it yields no Secret, every own image
   from GHCR with a non-`dev` tag, every tool route guarded.
3. **Demo data as two suspended CronJobs** `demo-seed` and `demo-refresh` (replacing the
   Job and its Argo hook): never scheduled, started on demand with
   `kubectl create job --from=cronjob/demo-refresh <name>`.
4. `k3d_smoke.py` triggers the demo through the CronJob.

## 7. Operations (laptop, AWS profile `smarthealth`)

| Script | Does |
| --- | --- |
| `scripts/aws_secrets.sh` | Puts the Groq key (from `.env`) and a generated JWT signing key into SSM — once, or to rotate |
| `scripts/aws_start.sh [--refresh]` | Starts NAT then server; waits for `https://<cf>/api/ready` = 200 (15 min, else prints the boot log via SSM); `--refresh` runs demo-refresh; prints URLs and the stop time |
| `scripts/aws_stop.sh` | Stops server then NAT |
| `scripts/aws_extend.sh` | Restarts both auto-stop timers (3 h from now) via SSM |
| `scripts/aws_seed.sh` | First time: demo-seed once Argo CD has synced |
| `scripts/aws_shell.sh` | SSM Session Manager shell on the server |
| `scripts/release.sh` | §6 |
| `scripts/aws_smoke.py` | HTTP checks through CloudFront: ready, demo login, gRPC-Web stream, tools and `/argocd` 401/200, `/assets/*` cached and `/api/*` not |

**First deploy:** (1) `terraform apply` in `bootstrap/` (state bucket; plan reviewed);
(2) `terraform plan` for the stack — **owner reviews every resource**, then apply;
(3) CloudFront URL into `values-aws.yaml`, `release.sh`, owner pushes `main`;
(4) `aws_secrets.sh`, `aws_start.sh`, wait for the first sync, `aws_seed.sh`, `aws_smoke.py`;
(5) owner decides keep or `terraform destroy`.

## 8. Testing

No AWS needed: `terraform fmt -check` and `terraform validate` (skipped without
`terraform`); meta-tests on the rendered user-data (k3s pinned, auto-stop present, no secret
values in it); the chart meta-tests of §6; `bash -n` on every script. Against AWS:
`aws_smoke.py`.

## 9. Documentation

README "Deploying to AWS" (prerequisites, first deploy, start/stop routine, cost, teardown);
site: an AWS architecture diagram in the Deployment section; ARCHITECTURE: an AWS section;
PRD §8: A-1…A-7 as the next free D- ids; PRD §9: no backups (risk); CLAUDE.md: the scripts.

## 10. Out of scope

A custom domain and HTTPS to the origin; WAF; backups/snapshots (recorded as a risk);
more than one server; CI.
