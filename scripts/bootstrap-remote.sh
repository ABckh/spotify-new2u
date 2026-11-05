#!/usr/bin/env bash
# Run deploy/bootstrap.sh on a server over SSH (WHO-175). From the repo root,
# on your own machine or a CI runner:
#
#   DEPLOY_HOST=203.0.113.10 BOOTSTRAP_USER=ubuntu \
#   DEPLOY_SSH_KEY_FILE=~/.ssh/id_ed25519 \
#   DEPLOY_KNOWN_HOSTS="$(cat known_hosts_line)" \
#   DEPLOY_PUBKEY="$(cat deploy_key.pub)" \
#   scripts/bootstrap-remote.sh
#
# Inputs, from the environment only:
#   DEPLOY_HOST          server address or name
#   BOOTSTRAP_USER       a user with sudo on the server (ubuntu on Oracle)
#   DEPLOY_SSH_KEY       BOOTSTRAP_USER's private key, the key itself (CI secret),
#   or DEPLOY_SSH_KEY_FILE  a path to it
#   DEPLOY_KNOWN_HOSTS   known_hosts line(s) for DEPLOY_HOST. Pinned: an unknown
#                        or changed host key stops the run
#   DEPLOY_PUBKEY        public key(s) the server's `deploy` user logs in with
#   DEPLOY_SSH_PORT      optional, default 22
#
# Copies deploy/ to a temporary directory on the server, runs
# `sudo deploy/bootstrap.sh` there and removes the directory again.

set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

for var in DEPLOY_HOST BOOTSTRAP_USER DEPLOY_KNOWN_HOSTS DEPLOY_PUBKEY; do
    [[ -n "${!var:-}" ]] || die "$var is not set"
done
[[ -n "${DEPLOY_SSH_KEY:-}" || -n "${DEPLOY_SSH_KEY_FILE:-}" ]] \
    || die "set DEPLOY_SSH_KEY or DEPLOY_SSH_KEY_FILE"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -x "$REPO_DIR/deploy/bootstrap.sh" ]] || die "deploy/bootstrap.sh not found under $REPO_DIR"

# Local scratch for the key and known_hosts; mode 0700, removed on exit.
LOCAL_TMP="$(mktemp -d)"
REMOTE_TMP=
cleanup() {
    if [[ -n "$REMOTE_TMP" ]]; then
        ssh_run "rm -rf '$REMOTE_TMP'" || printf 'WARNING: could not remove %s on the server\n' "$REMOTE_TMP" >&2
    fi
    rm -rf "$LOCAL_TMP"
}
trap cleanup EXIT

if [[ -n "${DEPLOY_SSH_KEY:-}" ]]; then
    KEY_FILE="$LOCAL_TMP/id"
    (umask 077 && printf '%s\n' "$DEPLOY_SSH_KEY" >"$KEY_FILE")
else
    KEY_FILE=$DEPLOY_SSH_KEY_FILE
fi
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" >"$LOCAL_TMP/known_hosts"

SSH_OPTS=(
    -p "${DEPLOY_SSH_PORT:-22}"
    -i "$KEY_FILE"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="$LOCAL_TMP/known_hosts"
    -o GlobalKnownHostsFile=/dev/null
    -o ConnectTimeout=20
    -o ServerAliveInterval=15
)

# Remote commands are built on this side on purpose. The only variable in them
# is REMOTE_TMP, which must match a fixed pattern before it is used.
# shellcheck disable=SC2029
ssh_run() { ssh "${SSH_OPTS[@]}" "$BOOTSTRAP_USER@$DEPLOY_HOST" "$@"; }

REMOTE_TMP="$(ssh_run 'mktemp -d /tmp/new2u-bootstrap.XXXXXXXX')"
[[ "$REMOTE_TMP" =~ ^/tmp/new2u-bootstrap\.[A-Za-z0-9]+$ ]] || {
    REMOTE_TMP=
    die "unexpected temporary directory name from the server"
}

# deploy/ plus the deploy user's public key, as one tar stream.
cp -R "$REPO_DIR/deploy" "$LOCAL_TMP/deploy"
printf '%s\n' "$DEPLOY_PUBKEY" >"$LOCAL_TMP/deploy.pub"
tar -C "$LOCAL_TMP" -cf - deploy deploy.pub | ssh_run "tar -C '$REMOTE_TMP' -xf -"

ssh_run "sudo -n bash '$REMOTE_TMP/deploy/bootstrap.sh' --deploy-pubkey '$REMOTE_TMP/deploy.pub'"
