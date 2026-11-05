#!/usr/bin/env bash
# Run deploy/db-host.sh on the database server over SSH: the server that holds
# PostgreSQL when the app uses an external database (docs/self-hosting.md "External
# database"). The database server has no public address, so the connection
# goes through a jump host (the app server). From the repo root:
#
#   DB_HOST=10.0.1.240 BOOTSTRAP_USER=ubuntu DEPLOY_SSH_KEY_FILE=~/.ssh/id_ed25519 \
#   DB_KNOWN_HOSTS="$(cat known_hosts_db)" \
#   JUMP_HOST=ubuntu@203.0.113.10 JUMP_KNOWN_HOSTS="$(cat known_hosts_app)" \
#   APP_CIDR=10.0.0.0/24 POSTGRES_PASSWORD=... \
#   scripts/bootstrap-db-remote.sh
#
# Inputs, from the environment only:
#   DB_HOST              the database server's private address (also what
#                        PostgreSQL listens on)
#   BOOTSTRAP_USER       a user with sudo there (ubuntu on Oracle)
#   DEPLOY_SSH_KEY_FILE  that user's private key (also used for the jump host)
#   DB_KNOWN_HOSTS       known_hosts line(s) for DB_HOST, pinned
#   JUMP_HOST            optional, user@host of the jump host
#   JUMP_KNOWN_HOSTS     known_hosts line(s) for the jump host, pinned
#   APP_CIDR             the app server's subnet, the only source let in
#   POSTGRES_PASSWORD    the app role's password; sent over SSH stdin only
#   POSTGRES_USER, POSTGRES_DB  optional, default new2u
#
# Copies deploy/ to a temporary directory on the server, runs
# `sudo deploy/db-host.sh` there with the password on stdin, and removes the
# directory again. Exits non-zero when any check FAILs.

set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

for var in DB_HOST BOOTSTRAP_USER DEPLOY_SSH_KEY_FILE DB_KNOWN_HOSTS APP_CIDR POSTGRES_PASSWORD; do
    [[ -n "${!var:-}" ]] || die "$var is not set"
done
[[ "$DB_HOST" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "DB_HOST must be the server's private IPv4 address"
[[ -z "${JUMP_HOST:-}" || -n "${JUMP_KNOWN_HOSTS:-}" ]] || die "JUMP_HOST is set but JUMP_KNOWN_HOSTS is not"
[[ "$APP_CIDR" =~ ^[0-9./]+$ ]] || die "APP_CIDR is not an IPv4 CIDR"
POSTGRES_USER="${POSTGRES_USER:-new2u}"
POSTGRES_DB="${POSTGRES_DB:-new2u}"
[[ "$POSTGRES_USER" =~ ^[a-z_][a-z0-9_]*$ && "$POSTGRES_DB" =~ ^[a-z_][a-z0-9_]*$ ]] \
    || die "POSTGRES_USER and POSTGRES_DB must be plain lower-case identifiers"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$REPO_DIR/deploy/db-host.sh" ]] || die "deploy/db-host.sh not found under $REPO_DIR"

LOCAL_TMP="$(mktemp -d)"
REMOTE_TMP=
cleanup() {
    if [[ -n "$REMOTE_TMP" ]]; then
        ssh_run "rm -rf '$REMOTE_TMP'" || printf 'WARNING: could not remove %s on the server\n' "$REMOTE_TMP" >&2
    fi
    rm -rf "$LOCAL_TMP"
}
trap cleanup EXIT

printf '%s\n' "$DB_KNOWN_HOSTS" >"$LOCAL_TMP/known_hosts"
COMMON_OPTS=(
    -i "$DEPLOY_SSH_KEY_FILE"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o StrictHostKeyChecking=yes
    -o GlobalKnownHostsFile=/dev/null
    -o ConnectTimeout=20
    -o ServerAliveInterval=15
)
SSH_OPTS=("${COMMON_OPTS[@]}" -o UserKnownHostsFile="$LOCAL_TMP/known_hosts")
if [[ -n "${JUMP_HOST:-}" ]]; then
    printf '%s\n' "$JUMP_KNOWN_HOSTS" >"$LOCAL_TMP/known_hosts_jump"
    # The jump connection checks its own pinned key; -W forwards to DB_HOST:22.
    printf -v jump_cmd '%q ' ssh "${COMMON_OPTS[@]}" -o UserKnownHostsFile="$LOCAL_TMP/known_hosts_jump" -W '%h:%p' "$JUMP_HOST"
    SSH_OPTS+=(-o ProxyCommand="$jump_cmd")
fi

# Remote commands are built on this side on purpose. The variables in them
# (REMOTE_TMP, APP_CIDR, DB_HOST, POSTGRES_USER, POSTGRES_DB) are checked
# against fixed patterns first; the password goes over stdin.
# shellcheck disable=SC2029
ssh_run() { ssh "${SSH_OPTS[@]}" "$BOOTSTRAP_USER@$DB_HOST" "$@"; }

REMOTE_TMP="$(ssh_run 'mktemp -d /tmp/new2u-db.XXXXXXXX')"
[[ "$REMOTE_TMP" =~ ^/tmp/new2u-db\.[A-Za-z0-9]+$ ]] || {
    REMOTE_TMP=
    die "unexpected temporary directory name from the server"
}

mkdir -p "$LOCAL_TMP/deploy"
cp "$REPO_DIR/deploy/db-host.sh" "$LOCAL_TMP/deploy/"
cp -R "$REPO_DIR/deploy/journald" "$LOCAL_TMP/deploy/journald"
tar -C "$LOCAL_TMP" -cf - deploy | ssh_run "tar -C '$REMOTE_TMP' -xf -"

printf '%s\n' "$POSTGRES_PASSWORD" | ssh_run \
    "sudo -n env APP_CIDR='$APP_CIDR' DB_LISTEN_IP='$DB_HOST' POSTGRES_USER='$POSTGRES_USER' POSTGRES_DB='$POSTGRES_DB' bash '$REMOTE_TMP/deploy/db-host.sh'"
