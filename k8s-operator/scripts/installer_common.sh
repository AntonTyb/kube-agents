#!/usr/bin/env bash
# ==============================================================================
# Shared definitions for the kube-agents installer front-ends.
# ==============================================================================
# Sourced by install.sh, uninstall.sh, and upgrade.sh — and this is where the
# terraform.tfvars generator lives, so the three front-ends describe the same
# install to the same engine (terraform/examples/full-install).
#
# Contract: the caller defines print_info / print_warning / print_error before
# calling anything here that reports. Functions read the vars.sh variable set
# from the environment (source vars.sh first); none of them prompt.
# ==============================================================================

# ─── Shared Installer Defaults ────────────────────────────────────────────────
# The values every installer front-end must agree on. Each default has exactly
# one home here so the entry points cannot drift apart.
DEFAULT_CLUSTER_NAME="platform-agent-host"
DEFAULT_REGION="us-central1"
DEFAULT_MODEL_PROVIDER="gemini"

# All kube-agents images (k8s-operator, platform-agent, credential-proxy,
# replay-proxy) default to this public registry prefix. Behind-the-firewall
# installs set REGISTRY_PREFIX to pull mirrored images instead.
DEFAULT_REGISTRY_PREFIX="ghcr.io/gke-labs/kube-agents"

# Model provider → the model the install defaults to for that provider.
default_model_for_provider() {
  case "${1:-}" in
    openai) echo "gpt-5.4" ;;
    anthropic) echo "claude-opus-5" ;;
    *) echo "gemini-3.5-flash" ;;
  esac
}

is_valid_model_provider() {
  [[ "${1:-}" =~ ^(gemini|vertex_ai|anthropic|openai)$ ]]
}

# The GCP IAM role bundles the install knows how to grant. Kubernetes RBAC is
# read-only in every one of them; see the site's reference/security-and-iam.
is_valid_permission_set() {
  [[ "${1:-}" =~ ^(read-only|gke-admin|custom)$ ]]
}

# ─── Boolean Parsing ──────────────────────────────────────────────────────────
# Interpret a value as a boolean toggle. Returns 0 (success) for common
# affirmative spellings and 1 otherwise. Matching is case-insensitive and
# surrounding whitespace is ignored, so all of the following are truthy:
#   true, yes, y, 1, on  (in any letter case, e.g. "True", "YES", "On")
# Everything else — including false, no, n, 0, off, and empty/unset — is falsy.
is_truthy() {
  local val="${1:-}"
  val="${val//[[:space:]]/}"
  case "$val" in
    [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Yy] | 1 | [Oo][Nn]) return 0 ;;
    *) return 1 ;;
  esac
}

# Checks if GKE databaseEncryption.state is a valid CMEK-encrypted state.
#   - ENCRYPTED: Standard CMEK database encryption state in GKE
#   - ALL_OBJECTS_ENCRYPTION_ENABLED: GKE 1.35+ Application-layer Secrets Encryption
is_valid_cmek_encryption_state() {
  local state="${1:-}"
  local valid_states=(
    "ENCRYPTED"
    "ALL_OBJECTS_ENCRYPTION_ENABLED"
  )

  for valid in "${valid_states[@]}"; do
    if [ "$state" = "$valid" ]; then
      return 0
    fi
  done
  return 1
}

retry() {
  local max_retries=$1
  local delay=$2
  shift 2
  local count=0

  while [ $count -lt $max_retries ]; do
    count=$((count + 1))
    if "$@"; then
      return 0
    fi
    if [ $count -lt $max_retries ]; then
      echo -e "  ⚠ [Retry $count/$max_retries] Waiting ${delay}s before next attempt..." >&2
      sleep "$delay"
    fi
  done

  return 1
}

# ─── vars.sh Persistence ──────────────────────────────────────────────────────
# vars.sh is the install's machine-readable record: the admin console, the e2e
# tests, and the Day-2 menu all read it. VARS_FILE must be set by the caller.
save_var() {
  local var_name=$1
  local var_val=$2
  export "${var_name}=${var_val}"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    return 0
  fi

  local old_umask
  old_umask=$(umask)
  umask 077

  if [ -f "$VARS_FILE" ]; then
    chmod 600 "$VARS_FILE" 2>/dev/null || true
    grep -E -v "^[[:space:]]*export[[:space:]]+${var_name}=" "$VARS_FILE" > "$VARS_FILE.tmp" 2>/dev/null || true
    chmod 600 "$VARS_FILE.tmp" 2>/dev/null || true
    mv "$VARS_FILE.tmp" "$VARS_FILE"
  fi
  printf "export %s=%q\n" "$var_name" "$var_val" >> "$VARS_FILE"
  chmod 600 "$VARS_FILE" 2>/dev/null || true

  umask "$old_umask"
}

save_secret_var() {
  local var_name=$1
  local var_val=$2
  export "${var_name}=${var_val}"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    return 0
  fi
  if is_truthy "${PERSIST_SECRETS_ON_DISK:-true}"; then
    save_var "$var_name" "$var_val"
  else
    if [ -f "$VARS_FILE" ]; then
      local old_umask
      old_umask=$(umask)
      umask 077
      chmod 600 "$VARS_FILE" 2>/dev/null || true
      grep -E -v "^[[:space:]]*export[[:space:]]+${var_name}=" "$VARS_FILE" > "$VARS_FILE.tmp" 2>/dev/null || true
      chmod 600 "$VARS_FILE.tmp" 2>/dev/null || true
      mv "$VARS_FILE.tmp" "$VARS_FILE"
      chmod 600 "$VARS_FILE" 2>/dev/null || true
      umask "$old_umask"
    fi
  fi
}

# ─── Locations ────────────────────────────────────────────────────────────────
# Cloud KMS has no zonal locations, so a zonal cluster's REGION (eg.
# "us-central1-c") is not a valid key location. Default to the enclosing region.
derive_kms_location() {
  local loc="${1:-}"
  if [[ "$loc" =~ ^(.+)-[a-z]$ ]]; then
    loc="${BASH_REMATCH[1]}"
  fi
  echo "$loc"
}

# ─── GitHub Account Classification ────────────────────────────────────────────
# Classifies a GitHub account name against the public API, echoing exactly one
# of: organization | user | missing | unknown.
#
# "unknown" is the catch-all for every inconclusive answer — curl absent, the
# network down, rate limiting, an unexpected payload — so a caller can tell
# "GitHub says no" apart from "we could not ask". Never exits and never prints,
# so it is safe to call from an interactive prompt loop; callers decide whether
# an answer is fatal. install.sh uses it to validate before provisioning starts.
github_account_type() {
  local name="${1:-}"
  if [ -z "$name" ] || ! command -v curl &>/dev/null; then
    echo "unknown"
    return 0
  fi

  # Status is appended on its own line so a transport failure (curl non-zero)
  # stays distinguishable from an HTTP error (curl zero, status in the body).
  local response status body
  if ! response=$(curl -sS --max-time 10 -H "Accept: application/vnd.github+json" \
      -w '\n%{http_code}' "https://api.github.com/users/${name}" 2>/dev/null); then
    echo "unknown"
    return 0
  fi
  status="${response##*$'\n'}"
  body="${response%$'\n'*}"

  if [ "$status" = "404" ]; then
    echo "missing"
    return 0
  fi
  if [ "$status" != "200" ]; then
    echo "unknown"
    return 0
  fi

  # Organization is matched first so it wins even if the payload somehow carries
  # both spellings, and both spacings are covered because the API is not
  # guaranteed to keep pretty-printing its JSON.
  case "$body" in
    *'"type": "Organization"'*|*'"type":"Organization"'*) echo "organization" ;;
    *'"type": "User"'*|*'"type":"User"'*) echo "user" ;;
    *) echo "unknown" ;;
  esac
}

# Minty resolves App installations with GET /orgs/{org}/installation and has no
# fallback to the /users/{user}/installation endpoint that serves personal
# accounts, so a user-owned GitOps repo can never mint a token. Left unchecked
# that surfaces far downstream, as an HTTP 500 from a Minty that deployed and
# passed its readiness probes, so catch it while GITHUB_ORG is still being set.
#
# This exits, so it is the wrong entry point for anything that can still
# re-prompt: install.sh calls github_account_type directly and settles the value
# before provisioning starts. An inconclusive lookup is never fatal — an
# unreachable api.github.com must not block a provision that is otherwise fine.
check_github_org_is_organization() {
  local org="${1:-}"
  [ -z "$org" ] && return 0

  if is_truthy "${SKIP_GITHUB_ORG_CHECK:-false}"; then
    print_warning "SKIP_GITHUB_ORG_CHECK=true is set; not verifying that '${org}' is an organization."
    return 0
  fi

  case "$(github_account_type "$org")" in
    organization) return 0 ;;
    user)
      print_error "GITHUB_ORG='${org}' is a GitHub user account, not an organization."
      print_error "The GitHub Token Minter looks installations up at /orgs/${org}/installation,"
      print_error "which does not exist for personal accounts, so every token request would"
      print_error "fail with a 404 after deployment."
      print_error "Move the GitOps repository to an organization (a free one is enough) and set"
      print_error "GITHUB_ORG in ${VARS_FILE:-scripts/vars.sh} to it, or re-run with"
      print_error "SKIP_GITHUB_ORG_CHECK=true to bypass this check."
      print_error "See the chart's githubMinter values and terraform/modules/github-minter."
      exit 1
      ;;
    missing)
      print_error "GITHUB_ORG='${org}' does not exist on GitHub."
      print_error "Check the spelling. The Token Minter resolves installations at"
      print_error "/orgs/${org}/installation, so a name that does not exist fails every"
      print_error "token request after deployment."
      print_error "Edit GITHUB_ORG in ${VARS_FILE:-scripts/vars.sh}, or re-run with"
      print_error "SKIP_GITHUB_ORG_CHECK=true to bypass this check."
      print_error "(GitHub Enterprise Server is not supported: this check, and the Minter,"
      print_error "both talk to api.github.com.)"
      exit 1
      ;;
    *)
      print_warning "Could not determine whether '${org}' is an organization; continuing."
      return 0
      ;;
  esac
}

# ─── Terraform State Location ─────────────────────────────────────────────────
# The bucket and prefix are derivable from the install coordinates alone, so a
# fresh clone (uninstall.sh, upgrade.sh) can find the state without any file
# from the original install. Keep in step with lifecycle.sh's ensure_backend.
tf_state_bucket() {
  local bucket="${KUBE_AGENTS_STATE_BUCKET:-auto}"
  [ "$bucket" = "auto" ] && bucket="${PROJECT_ID}-kube-agents-tfstate"
  echo "$bucket"
}

tf_state_prefix() {
  echo "${KUBE_AGENTS_STATE_PREFIX:-kube-agents/${CLUSTER_NAME}}"
}

# ─── terraform.tfvars Generation ──────────────────────────────────────────────
# HCL string literal with backslashes and double quotes escaped.
hcl_str() {
  local s="${1//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

hcl_bool() {
  if is_truthy "${1:-}"; then printf 'true'; else printf 'false'; fi
}

# Comma- or space-separated string → HCL list of strings, dropping empty
# items. Both separators, because --custom-roles documents "space- or
# comma-separated" and the retired provision_04 honoured both.
hcl_csv_list() {
  local csv="${1:-}" out="[" first=true item
  local IFS=$', \t\n'
  for item in $csv; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [ -n "$item" ] || continue
    $first || out+=", "
    out+="$(hcl_str "$item")"
    first=false
  done
  printf '%s]' "$out"
}

# Whether this install's Terraform state already MANAGES the cluster. Read
# straight from the state object in GCS — cheaper and earlier than an init, and
# it works from a fresh clone. Any read failure means "not ours".
#
# Parsed, not grepped, for two reasons. An existing-cluster install records a
# data-mode "google_container_cluster" entry in the same state, and matching on
# the type alone would flip such an install's create_cluster back to true on
# every re-run — planning a second cluster over the real one. And a
# `gcloud | grep -q` pipeline under pipefail can report a cluster that IS in
# state as absent when grep exits before gcloud finishes writing (the same trap
# lifecycle.sh documents for its own state reads).
tf_state_has_cluster() {
  local state
  state=$(gcloud storage cat "gs://$(tf_state_bucket)/$(tf_state_prefix)/default.tfstate" 2>/dev/null) || return 1
  printf '%s' "$state" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(1)
managed = any(
    r.get("type") == "google_container_cluster" and r.get("mode") == "managed"
    for r in doc.get("resources", [])
)
sys.exit(0 if managed else 1)
'
}

# Writes the terraform.tfvars the full-install composition consumes, from the
# vars.sh variable set in the environment (source vars.sh first). This is what
# replaced the fourteen provisioning scripts as the engine's input: the same
# generator runs from install.sh, upgrade.sh, and uninstall.sh, so the three
# front-ends can never describe different installs.
#
# create_cluster comes from a liveness probe, not from the interview: the
# script pipeline was check-then-create, so "use an existing cluster" against a
# name that does not exist still created it. A cluster that exists but is
# already in OUR state stays create_cluster = true — flipping it off would
# remove the resource from configuration and plan the cluster's destruction
# (lifecycle.sh guards this too).
write_tfvars_from_state() {
  local dest="$1"
  local image_tag="${2:-${IMAGE_TAG:-latest}}"

  # vars.sh does not always carry the credentials: PERSIST_SECRETS_ON_DISK=false
  # keeps them out of it, on script-era installs as well as new ones. Their
  # home is the live Secret, so recover any missing key from it the way the
  # retired provision_07 did — best-effort, since on a fresh install there is
  # no cluster to ask yet and the keys are still in the environment.
  local secret_key secret_val
  if command -v kubectl >/dev/null 2>&1; then
    for secret_key in API_SERVER_KEY GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
      [ -z "${!secret_key:-}" ] || continue
      secret_val=$(kubectl get secret platform-agent-secrets -n "${NAMESPACE:-kubeagents-system}" \
        -o jsonpath="{.data.${secret_key}}" 2>/dev/null | base64 --decode 2>/dev/null) || secret_val=""
      if [ -n "$secret_val" ]; then
        export "${secret_key}=${secret_val}"
        print_info "Recovered ${secret_key} from the live 'platform-agent-secrets' Secret (vars.sh does not persist it)."
      fi
    done
  fi

  # The composition requires api_server_key non-empty, so fail here with the
  # recovery path spelled out rather than aborting the caller on an opaque
  # unbound-variable error under set -u.
  if [ -z "${API_SERVER_KEY:-}" ]; then
    print_error "API_SERVER_KEY is not set, vars.sh does not carry it (PERSIST_SECRETS_ON_DISK=false keeps it out), and it could not be recovered from the live Secret."
    print_info "Recover it and re-run: export API_SERVER_KEY=\"\$(kubectl get secret platform-agent-secrets -n kubeagents-system -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)\""
    return 1
  fi

  # cluster_mode follows the LIVE cluster when there is one. Hardcoding
  # "standard" here planned the destruction of every existing Autopilot
  # install the moment a front door regenerated tfvars against it — the
  # autopilot resource's count went to 0, so uninstall's targeted
  # deletion-protection apply and upgrade's full apply both became cluster
  # replacements. A fresh create keeps "standard", the script-parity shape.
  local create_cluster="true" cluster_mode="standard" autopilot_enabled=""
  if autopilot_enabled=$(gcloud container clusters describe "${CLUSTER_NAME}" \
      --location "${REGION}" --project "${PROJECT_ID}" \
      --format="value(autopilot.enabled)" 2>/dev/null); then
    if [ "$autopilot_enabled" = "True" ]; then
      cluster_mode="autopilot"
      print_info "Cluster '${CLUSTER_NAME}' is an Autopilot cluster; generating cluster_mode = \"autopilot\"."
    fi
    if tf_state_has_cluster; then
      print_info "Cluster '${CLUSTER_NAME}' exists and is managed by this install's Terraform state."
    else
      create_cluster="false"
      print_info "Cluster '${CLUSTER_NAME}' already exists and is not in Terraform state; installing onto it (create_cluster = false)."
    fi
  fi

  # The generator's create/adopt decision, exported for the callers that need
  # it after the apply: install.sh sets the managed-OTel scope only on a
  # cluster this install created, the way provision_01 set it on create only.
  TFVARS_CREATE_CLUSTER="$create_cluster"
  export TFVARS_CREATE_CLUSTER

  # A pre-existing cert-manager makes the composition's own cert-manager
  # release fail on the existing CRDs, so probe for one on the existing-cluster
  # path. Best-effort: an unreachable cluster leaves the default in place.
  # SKIP_CERT_MANAGER=true is the explicit opt-out the retired path had — for
  # the cluster whose cert-manager comes from somewhere else, or the air-gapped
  # runner that cannot fetch the Jetstack chart from charts.jetstack.io.
  local enable_cert_manager="true"
  if is_truthy "${SKIP_CERT_MANAGER:-false}"; then
    enable_cert_manager="false"
    print_info "SKIP_CERT_MANAGER=true: the composition will not install cert-manager. The operator webhooks need one serving before the apply."
  elif [ "$create_cluster" = "false" ] && command -v kubectl >/dev/null 2>&1; then
    if gcloud container clusters get-credentials "${CLUSTER_NAME}" --location "${REGION}" \
        --project "${PROJECT_ID}" >/dev/null 2>&1 &&
      kubectl get deployment cert-manager -n cert-manager >/dev/null 2>&1; then
      enable_cert_manager="false"
      print_info "cert-manager already runs on '${CLUSTER_NAME}'; the composition will not install its own."
    fi
  fi

  # Only a mirrored install sets image_registry; the default prefix means
  # "the public registries", which the composition spells as empty.
  local image_registry=""
  if [ -n "${REGISTRY_PREFIX:-}" ] && [ "${REGISTRY_PREFIX%/}" != "$DEFAULT_REGISTRY_PREFIX" ]; then
    image_registry="${REGISTRY_PREFIX%/}"
  fi

  local enable_github_minter="false"
  if [ -n "${GITHUB_ORG:-}" ] && [ -n "${GITHUB_REPO:-}" ] && [ -n "${GITHUB_APP_ID:-}" ]; then
    enable_github_minter="true"
  fi

  local old_umask
  old_umask="$(umask)"
  umask 077
  {
    echo "# Generated by the kube-agents installer from vars.sh — regenerated on every"
    echo "# run. Change settings through install.sh (or its --menu) rather than here."
    echo "project_id   = $(hcl_str "${PROJECT_ID}")"
    echo "cluster_name = $(hcl_str "${CLUSTER_NAME}")"
    echo "location     = $(hcl_str "${REGION}")"
    echo ""
    echo "# The shape the retired provisioning scripts built: a Standard cluster with"
    echo "# the DNS endpoint open and no deletion protection. An existing cluster"
    echo "# keeps its own mode — see the probe above."
    echo "cluster_mode               = $(hcl_str "${cluster_mode}")"
    echo "create_cluster             = ${create_cluster}"
    echo "allow_external_dns_traffic = true"
    echo "deletion_protection        = false"
    echo "enable_gvisor_node_pool    = $(hcl_bool "${ENABLE_GVISOR:-false}")"
    echo "gvisor_pool_name           = $(hcl_str "${GVISOR_POOL_NAME:-gvisor-pool}")"
    echo "enable_cert_manager        = ${enable_cert_manager}"
    echo ""
    echo "image_tag                  = $(hcl_str "${image_tag}")"
    echo "image_registry             = $(hcl_str "${image_registry}")"
    echo "third_party_image_registry = $(hcl_str "${THIRD_PARTY_REGISTRY_PREFIX:-}")"
    echo ""
    echo "model_provider     = $(hcl_str "${MODEL_PROVIDER:-$DEFAULT_MODEL_PROVIDER}")"
    echo "model_default_name = $(hcl_str "${MODEL_DEFAULT_NAME:-}")"
    echo "vertex_project_id  = $(hcl_str "${VERTEX_PROJECT_ID:-}")"
    echo "vertex_location    = $(hcl_str "${VERTEX_LOCATION:-}")"
    echo ""
    echo "api_server_key    = $(hcl_str "${API_SERVER_KEY:-}")"
    echo "gemini_api_key    = $(hcl_str "${GEMINI_API_KEY:-}")"
    echo "openai_api_key    = $(hcl_str "${OPENAI_API_KEY:-}")"
    echo "anthropic_api_key = $(hcl_str "${ANTHROPIC_API_KEY:-}")"
    echo ""
    echo "permission_set = $(hcl_str "${PLATFORM_AGENT_PERMISSION_SET:-read-only}")"
    if [ "${PLATFORM_AGENT_PERMISSION_SET:-}" = "custom" ]; then
      echo "project_roles  = $(hcl_csv_list "${PLATFORM_AGENT_CUSTOM_ROLES:-}")"
    fi
    echo ""
    echo "enable_google_chat        = $(hcl_bool "${GOOGLE_CHAT_ENABLED:-false}")"
    echo "chat_topic_name           = $(hcl_str "${CHAT_TOPIC_NAME:-platform-agent-chat-events}")"
    echo "chat_subscription_name    = $(hcl_str "${CHAT_SUB_NAME:-platform-agent-chat-events-sub}")"
    echo "google_chat_allowed_users = $(hcl_csv_list "${ALLOWED_USERS:-}")"
    echo "google_chat_mode          = $(hcl_str "${GOOGLE_CHAT_MODE:-default}")"
    echo ""
    echo "enable_slack            = $(hcl_bool "${SLACK_ENABLED:-false}")"
    echo "slack_bot_token         = $(hcl_str "${SLACK_BOT_TOKEN:-}")"
    echo "slack_app_token         = $(hcl_str "${SLACK_APP_TOKEN:-}")"
    echo "slack_allowed_users     = $(hcl_csv_list "${SLACK_ALLOWED_USERS:-}")"
    echo "slack_home_channel      = $(hcl_str "${SLACK_HOME_CHANNEL:-}")"
    echo "slack_home_channel_name = $(hcl_str "${SLACK_HOME_CHANNEL_NAME:-}")"
    echo ""
    if [ -n "${GITHUB_ORG:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
      echo "github_repo = $(hcl_str "${GITHUB_ORG}/${GITHUB_REPO}")"
    fi
    echo "enable_github_minter = ${enable_github_minter}"
    echo "github_app_id        = $(hcl_str "${GITHUB_APP_ID:-}")"
    if [ -n "${KMS_KEYRING:-}" ]; then
      echo "github_minter_kms_keyring = $(hcl_str "${KMS_KEYRING}")"
    fi
    if [ -n "${KMS_KEY:-}" ]; then
      echo "github_minter_kms_key = $(hcl_str "${KMS_KEY}")"
    fi
    echo ""
    echo "enable_gke_backup_plan = $(hcl_bool "${ENABLE_GKE_BACKUP_PLAN:-false}")"
    echo ""
    echo "# The CRD defaults dashboardEnabled to true; the installer has always"
    echo "# defaulted it to false and asks. Memory settings mirror --memory."
    echo "hermes_dashboard_enabled = $(hcl_bool "${HERMES_DASHBOARD_ENABLED:-false}")"
    echo "memory_enabled           = $(hcl_bool "${MEMORY_ENABLED:-false}")"
    echo "memory_provider          = $(hcl_str "${MEMORY_PROVIDER:-multiuser_memory}")"
    echo "user_profile_enabled     = $(hcl_bool "${USER_PROFILE_ENABLED:-false}")"
  } > "${dest}.tmp"
  chmod 600 "${dest}.tmp"
  mv -f -- "${dest}.tmp" "$dest"
  umask "$old_umask"
}
