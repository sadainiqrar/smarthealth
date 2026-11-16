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
