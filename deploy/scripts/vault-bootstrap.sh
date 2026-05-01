#!/usr/bin/env bash
# Phase 8 — vault → GitHub bootstrap (one-time, owner runs on the VPS).
#
# Idempotent: re-running skips already-completed steps. Owner is in the
# loop for steps that touch GitHub (deploy-key paste).
#
# Run as the VPS user that owns ~/.local/share/0xone-assistant/ (uid
# 1000 — `0xone` on the production host).

set -euo pipefail

REPO_ROOT_DEFAULT="/opt/0xone-assistant"
DATA_DIR_DEFAULT="${HOME}/.local/share/0xone-assistant"

REPO_ROOT="${REPO_ROOT:-${REPO_ROOT_DEFAULT}}"
DATA_DIR="${DATA_DIR:-${DATA_DIR_DEFAULT}}"
VAULT_DIR="${VAULT_DIR:-${DATA_DIR}/vault}"
KEY_PATH="${KEY_PATH:-${HOME}/.ssh/vault_deploy}"
KH_PATH="${KH_PATH:-${HOME}/.ssh/known_hosts_vault}"
PINNED_PATH="${PINNED_PATH:-${REPO_ROOT}/deploy/known_hosts_vault.pinned}"
REPO_URL="${REPO_URL:-git@github.com:c0manch3/0xone-vault.git}"

# Secret denylist regex set — MUST match
# VaultSyncSettings.secret_denylist_regex in src/assistant/config.py
# (W2-H4 single-source-of-truth; AC#19 verifies parity).
DENY_RE='(^secrets/|^\.aws/|^\.config/0xone-assistant/|\.env$|\.key$|\.pem$)'

log() { printf '[vault-bootstrap] %s\n' "$*"; }
err() { printf '[vault-bootstrap] ERROR: %s\n' "$*" >&2; }

require_dir() {
    if [[ ! -d "$1" ]]; then
        err "missing directory: $1"
        return 1
    fi
}

step1_keypair() {
    log "step 1: ensure SSH deploy keypair at ${KEY_PATH}"
    mkdir -p "$(dirname "${KEY_PATH}")"
    chmod 700 "$(dirname "${KEY_PATH}")"
    if [[ ! -f "${KEY_PATH}" ]]; then
        log "  generating ed25519 keypair…"
        ssh-keygen -t ed25519 -f "${KEY_PATH}" -N "" \
            -C "0xone-vault deploy key (VPS)"
    else
        log "  keypair already exists; skipping"
    fi
    chmod 600 "${KEY_PATH}"
    chmod 644 "${KEY_PATH}.pub"
}

step2_known_hosts() {
    log "step 2: install pinned known_hosts at ${KH_PATH}"
    if [[ ! -f "${PINNED_PATH}" ]]; then
        err "pinned known_hosts file not found: ${PINNED_PATH}"
        err "  expected at <repo_root>/deploy/known_hosts_vault.pinned"
        return 1
    fi
    cp "${PINNED_PATH}" "${KH_PATH}"
    chmod 600 "${KH_PATH}"
    log "  copied ${PINNED_PATH} → ${KH_PATH}"
}

step3_register_deploy_key() {
    log "step 3: paste deploy key into GitHub"
    log ""
    log "  public key:"
    cat "${KEY_PATH}.pub"
    log ""
    log "  Open https://github.com/c0manch3/0xone-vault/settings/keys/new"
    log "  Tick 'Allow write access' and paste the key above."
    log ""
    read -r -p "  Press Enter once the deploy key is registered… " _
}

step4_init_repo() {
    log "step 4: initialise vault git repo at ${VAULT_DIR}"
    require_dir "${VAULT_DIR}"
    if [[ -d "${VAULT_DIR}/.git" ]]; then
        log "  already a git repo; skipping init"
    else
        ( cd "${VAULT_DIR}" && git init -b main )
    fi
    ( cd "${VAULT_DIR}" \
        && git config user.name  "0xone-assistant" \
        && git config user.email "0xone-assistant@users.noreply.github.com" \
        && git config core.autocrlf false \
        && git config core.filemode false )
    if ( cd "${VAULT_DIR}" && git remote get-url origin >/dev/null 2>&1 ); then
        log "  remote 'origin' already configured"
    else
        ( cd "${VAULT_DIR}" && git remote add origin "${REPO_URL}" )
        log "  remote 'origin' → ${REPO_URL}"
    fi
    if [[ ! -f "${VAULT_DIR}/.gitignore" ]]; then
        cat >"${VAULT_DIR}/.gitignore" <<'EOF'
# Phase 8 vault sync — secret-leak defence-in-depth.
# MUST stay in sync with VaultSyncSettings.secret_denylist_regex
# (src/assistant/config.py). AC#19 verifies parity.
*.env
*.key
*.pem
secrets/
.aws/
.config/0xone-assistant/
# vault-internal — must never be committed.
.tmp/
*.lock
memory-index.db
memory-index.db-wal
memory-index.db-shm
# editor / OS clutter
*.swp
.DS_Store
*~
EOF
        log "  wrote ${VAULT_DIR}/.gitignore"
    else
        log "  .gitignore already present; not overwriting"
    fi
}

step5_pre_push_check() {
    log "step 5: pre-push secret-leak validation"
    ( cd "${VAULT_DIR}" && git add -A )
    local staged
    staged=$( cd "${VAULT_DIR}" && git diff --cached --name-only )
    if [[ -z "${staged}" ]]; then
        log "  nothing staged; skipping denylist check"
        return 0
    fi
    local matches
    matches=$( printf '%s\n' "${staged}" | grep -E "${DENY_RE}" || true )
    if [[ -n "${matches}" ]]; then
        err "staged files match secret denylist:"
        printf '%s\n' "${matches}" >&2
        err "abort. Remove these paths from the vault dir before re-running."
        return 1
    fi
    log "  OK — no secret-pattern paths staged"
}

step6_initial_push() {
    log "step 6: initial commit + push"
    local has_commit=0
    if ( cd "${VAULT_DIR}" && git rev-parse HEAD >/dev/null 2>&1 ); then
        has_commit=1
    fi
    if [[ "${has_commit}" -eq 0 ]]; then
        ( cd "${VAULT_DIR}" \
            && git commit -m "initial: gitignore + pre-existing notes" )
    else
        if ( cd "${VAULT_DIR}" && ! git diff --cached --quiet ); then
            ( cd "${VAULT_DIR}" \
                && git commit -m "vault bootstrap: stage existing notes" )
        else
            log "  no staged changes; nothing to commit"
        fi
    fi
    GIT_SSH_COMMAND="ssh -i ${KEY_PATH} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${KH_PATH}" \
        git -C "${VAULT_DIR}" push -u origin main
    log "  pushed origin/main"
}

step7_safe_directory() {
    log "step 7: configure git safe.directory for ${VAULT_DIR}"
    git config --global --add safe.directory "${VAULT_DIR}" || true
}

step8_print_env_diff() {
    log "step 8: env-file diff to apply manually"
    log ""
    log "  Add to ~/.config/0xone-assistant/.env:"
    log ""
    log "    VAULT_SYNC_ENABLED=true"
    log "    VAULT_SYNC_REPO_URL=${REPO_URL}"
    log "    VAULT_SYNC_MANUAL_TOOL_ENABLED=true"
    log ""
}

step9_restart_hint() {
    log "step 9: restart the daemon"
    log ""
    log "  cd ${REPO_ROOT}/deploy/docker"
    log "  docker compose restart"
    log ""
    log "  Verify the keys are mounted inside the container:"
    log "    docker exec 0xone-assistant ls -l /home/bot/.ssh/vault_deploy /home/bot/.ssh/known_hosts_vault"
    log ""
    log "  Bootstrap complete. Set VAULT_SYNC_ENABLED=true in env."
}

main() {
    require_dir "${DATA_DIR}"
    require_dir "${REPO_ROOT}"
    mkdir -p "${VAULT_DIR}"
    step1_keypair
    step2_known_hosts
    step3_register_deploy_key
    step4_init_repo
    step5_pre_push_check
    step6_initial_push
    step7_safe_directory
    step8_print_env_diff
    step9_restart_hint
}

main "$@"
