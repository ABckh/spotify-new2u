#!/usr/bin/env bash
# Deploy one image to a bootstrapped server over SSH (WHO-176). The production
# job (.github/workflows/deploy.yml) and the CI test target run this same script;
# only the inputs differ. From the repo root:
#
#   DEPLOY_HOST=203.0.113.10 DEPLOY_USER=deploy DEPLOY_SSH_KEY_FILE=~/.ssh/deploy \
#   DEPLOY_KNOWN_HOSTS="$(cat known_hosts_line)" \
#   API_IMAGE=ghcr.io/owner/new2u@sha256:<digest> API_HOSTNAME=api.example.org \
#   POSTGRES_PASSWORD=... (every secret and input of deploy/env.template) \
#   scripts/deploy-remote.sh
#
# Inputs, from the environment only (docs/self-hosting.md "Deploy credentials"):
#   DEPLOY_HOST, DEPLOY_USER, DEPLOY_KNOWN_HOSTS, DEPLOY_SSH_PORT (default 22)
#   DEPLOY_SSH_KEY       the deploy user's private key itself (CI secret),
#   or DEPLOY_SSH_KEY_FILE  a path to it
#   API_IMAGE            the image to run, by the digest CI's push printed
#                        (REPO@sha256:..., see the image job's summary)
#   CADDY_TLS            optional: unset picks public (a name) or public_ip
#                        (an IP address); internal for test targets only
#   every `secret` and `input` row of deploy/env.template, `default` rows if set
#   BACKUP_S3_ENDPOINT, BACKUP_S3_REGION, BACKUP_S3_BUCKET, BACKUP_S3_ACCESS_KEY_ID,
#   BACKUP_S3_SECRET_ACCESS_KEY, BACKUP_AGE_RECIPIENT
#                        optional, all or none: the nightly backup (WHO-181).
#                        None set: backups off, the deploy passes.
#                        BACKUP_S3_ENDPOINT: https://, or http:// to
#                        127.0.0.1/localhost only.
#   BACKUP_PRUNE         optional, with the six above: on (default) or off.
#                        off: the job deletes nothing, the bucket's lifecycle
#                        rule does, and the key needs no delete right (WHO-182).
#   BACKUP_HEARTBEAT_URL optional secret, with the six above: pinged after
#                        each backup that passed (WHO-182). https://, or
#                        http:// to 127.0.0.1/localhost only.
#   REGISTRY_USER, REGISTRY_TOKEN  optional: `docker login` on the server for
#                        the pull, then logout. The token goes over stdin.
#   SKIP_MIGRATIONS      optional, "true" to not run migrations: going back to
#                        an older commit, whose code does not know the newer ones
#   HEALTH_TIMEOUT_SECONDS  optional, default 180
#   HEALTH_CHECK_RESOLVE    optional, test targets only: curl --resolve HOST:443:IP
#
# Steps: upload the compose files, the Caddyfile, deploy/release.sh and
# deploy/backup.sh; write .env and backup.env (0600) from stdin; pull,
# migrate, up -d; check https://API_HOSTNAME/healthz through Caddy and that
# api and poller stay up; record the release. When any
# step after .env fails, roll back to the last release that passed (`current`
# on the server) and check it again.
#
# Exit status: 0 deployed; 1 deploy failed and the earlier release is back and
# healthy; 2 anything else (bad input, or no healthy release running).
#
# Never `set -x`: the environment holds the secrets. No secret is put on a
# command line or printed; .env and the registry token travel over SSH stdin.

set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
log() { printf '==> %s\n' "$*"; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_DIR
readonly APP_DIR=/srv/new2u
readonly TEMPLATE="$REPO_DIR/deploy/env.template"
readonly UPLOAD_FILES=(docker-compose.yml docker-compose.vps.yml deploy/caddy/Caddyfile deploy/release.sh deploy/backup.sh)
# deploy/backup.sh's settings (docs/self-hosting.md "Backups"): all set, or none.
readonly BACKUP_SETTINGS=(BACKUP_S3_ENDPOINT BACKUP_S3_REGION BACKUP_S3_BUCKET
    BACKUP_S3_ACCESS_KEY_ID BACKUP_S3_SECRET_ACCESS_KEY BACKUP_AGE_RECIPIENT)
# Optional on top of those; written only when set (WHO-182).
readonly BACKUP_OPTIONAL=(BACKUP_PRUNE BACKUP_HEARTBEAT_URL)
# The same rules as deploy/backup.sh: https, or plain http to this machine.
readonly BACKUP_ENDPOINT_RE='^(https://[A-Za-z0-9.-]+|http://(127\.0\.0\.1|localhost))(:[0-9]+)?/?$'
readonly BACKUP_HEARTBEAT_RE='^(https://[A-Za-z0-9.-]+|http://(127\.0\.0\.1|localhost))(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$'
# `secret`/`input` rows that may be empty. SPOTIFY_CLIENT_SECRET: empty means a
# public client (PKCE only), see .env.example.
readonly MAY_BE_EMPTY=(SPOTIFY_CLIENT_SECRET)
# By digest only, never a tag (WHO-177 finding 9): the digest the image push
# printed. deploy/release.sh also takes tags, for a manual rollback.
readonly IMAGE_RE='^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$'

for var in DEPLOY_HOST DEPLOY_USER DEPLOY_KNOWN_HOSTS API_IMAGE API_HOSTNAME; do
    [[ -n "${!var:-}" ]] || die "$var is not set"
done
[[ -n "${DEPLOY_SSH_KEY:-}" || -n "${DEPLOY_SSH_KEY_FILE:-}" ]] \
    || die "set DEPLOY_SSH_KEY or DEPLOY_SSH_KEY_FILE"
[[ "$API_IMAGE" =~ $IMAGE_RE ]] || die "API_IMAGE is not an image reference by digest (REPO@sha256:...)"
# API_HOSTNAME: a lower-case DNS name, an IPv4 address, or an IPv6 address in
# brackets (as it goes into a URL and into the Caddyfile).
API_HOSTNAME_KIND=
if [[ "$API_HOSTNAME" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
    IFS=. read -r -a octets <<<"$API_HOSTNAME"
    for octet in "${octets[@]}"; do
        if ! [[ "$octet" =~ ^(0|[1-9][0-9]{0,2})$ ]] || ((octet > 255)); then
            die "API_HOSTNAME is not an IPv4 address"
        fi
    done
    API_HOSTNAME_KIND=ip
elif [[ "$API_HOSTNAME" =~ ^\[[0-9a-f:]+\]$ && "$API_HOSTNAME" == *:*:* ]]; then
    API_HOSTNAME_KIND=ip
elif [[ "$API_HOSTNAME" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ && "$API_HOSTNAME" == *[a-z-]* ]]; then
    API_HOSTNAME_KIND=name
else
    die "API_HOSTNAME is not a host name, an IPv4 address or a bracketed IPv6 address"
fi
# Caddy's certificate (deploy/caddy/Caddyfile). Unset: from the kind of name.
# `public` on an IP would quietly give a certificate from Caddy's internal CA.
if [[ -z "${CADDY_TLS:-}" ]]; then
    if [[ "$API_HOSTNAME_KIND" == ip ]]; then CADDY_TLS=public_ip; else CADDY_TLS=public; fi
fi
case "$CADDY_TLS:$API_HOSTNAME_KIND" in
    public:name | public_ip:ip | internal:*) ;;
    *) die "CADDY_TLS=$CADDY_TLS does not fit API_HOSTNAME (a $API_HOSTNAME_KIND)" ;;
esac
# The bare address for Caddy's default_sni: no brackets around IPv6.
CADDY_DEFAULT_SNI="${API_HOSTNAME#[}"
CADDY_DEFAULT_SNI="${CADDY_DEFAULT_SNI%]}"
export CADDY_TLS CADDY_DEFAULT_SNI
# The database (docs/self-hosting.md "External database"): the postgres container here, or
# with DATABASE_HOST a server of its own, reached over TLS. COMPOSE_PROFILES
# starts the postgres container only in the first case.
if [[ -n "${DATABASE_HOST:-}" ]]; then
    [[ "$DATABASE_HOST" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ || "$DATABASE_HOST" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] \
        || die "DATABASE_HOST is not an IPv4 address or a host name"
    [[ "$DATABASE_HOST" != postgres ]] || die "DATABASE_HOST=postgres is the local container: leave it unset instead"
    # Only require for now: verify-ca/verify-full would need the server's CA in the image.
    [[ -z "${DATABASE_SSLMODE:-}" || "$DATABASE_SSLMODE" == require ]] \
        || die "DATABASE_SSLMODE must be require (or unset)"
    DATABASE_SSLMODE=require
    # SCRAM-SHA-256-PLUS only: the exchange is bound to the certificate libpq
    # saw, so a host in between can neither relay it nor ask for a cleartext
    # password (WHO-188 F1). Works with the DB host's self-signed certificate.
    [[ -z "${DATABASE_CHANNEL_BINDING:-}" || "$DATABASE_CHANNEL_BINDING" == require ]] \
        || die "DATABASE_CHANNEL_BINDING must be require (or unset)"
    DATABASE_CHANNEL_BINDING=require
    COMPOSE_PROFILES=
else
    # The local container has no TLS: require would refuse every connection.
    [[ -z "${DATABASE_SSLMODE:-}" ]] || die "DATABASE_SSLMODE is set, but DATABASE_HOST is not"
    [[ -z "${DATABASE_CHANNEL_BINDING:-}" ]] \
        || die "DATABASE_CHANNEL_BINDING is set, but DATABASE_HOST is not"
    COMPOSE_PROFILES=localdb
fi
export DATABASE_SSLMODE DATABASE_CHANNEL_BINDING COMPOSE_PROFILES
[[ "$DEPLOY_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "DEPLOY_USER is not a user name"
if [[ -n "${REGISTRY_TOKEN:-}" ]]; then
    [[ "${REGISTRY_USER:-}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "REGISTRY_USER missing or not a user name"
fi
readonly REGISTRY="${API_IMAGE%%/*}"
[[ "$REGISTRY" == *.* || "$REGISTRY" == *:* ]] || die "API_IMAGE must name its registry, e.g. ghcr.io/..."

# .env from deploy/env.template and the environment. Values are single-quoted
# (Compose reads them literally, so a `$` in a secret stays a `$`), which
# rules out a quote or a line break inside a value.
render_env() {
    local name source fixed value
    while read -r name source fixed _; do
        [[ -z "$name" || "$name" == \#* ]] && continue
        case "$source" in
            compose) continue ;;
            fixed) value=$fixed ;;
            secret | input)
                # API_IMAGE: deploy/release.sh adds it, so a rollback can swap it alone.
                [[ "$name" == API_IMAGE ]] && continue
                value="${!name:-}"
                if [[ -z "$value" ]] && ! printf '%s\n' "${MAY_BE_EMPTY[@]}" | grep -qx "$name"; then
                    die "$name is not set ($source in deploy/env.template)"
                fi
                ;;
            default)
                value="${!name:-}"
                [[ -n "$value" ]] || continue
                ;;
            *) die "env.template: unknown source $source for $name" ;;
        esac
        [[ "$value" != *"'"* && "$value" != *$'\n'* && "$value" != *$'\r'* ]] \
            || die "$name contains a quote or a line break"
        printf "%s='%s'\n" "$name" "$value"
    done <"$TEMPLATE"
}

# backup.env: every backup setting, or nothing (backups off). Half a set is an
# input mistake and stops the deploy.
render_backup_env() {
    local name value set=() unset=() extra=()
    for name in "${BACKUP_SETTINGS[@]}"; do
        if [[ -n "${!name:-}" ]]; then set+=("$name"); else unset+=("$name"); fi
    done
    for name in "${BACKUP_OPTIONAL[@]}"; do
        if [[ -n "${!name:-}" ]]; then extra+=("$name"); fi
    done
    if ((${#set[@]} == 0)); then
        ((${#extra[@]} == 0)) || die "backup settings: ${extra[*]} set, but none of ${BACKUP_SETTINGS[*]}"
        return 0
    fi
    ((${#unset[@]} == 0)) || die "backup settings: ${unset[*]} not set, but ${set[*]} are (set all or none)"
    [[ "$BACKUP_AGE_RECIPIENT" =~ ^age1[02-9ac-hj-np-z]{58}$ ]] || die "BACKUP_AGE_RECIPIENT is not an age public key (age1...)"
    [[ "$BACKUP_S3_ENDPOINT" =~ $BACKUP_ENDPOINT_RE ]] \
        || die "BACKUP_S3_ENDPOINT is not a URL like https://host (http only to 127.0.0.1 or localhost)"
    [[ -z "${BACKUP_PRUNE:-}" || "$BACKUP_PRUNE" == on || "$BACKUP_PRUNE" == off ]] \
        || die "BACKUP_PRUNE must be on or off"
    # The message leaves the URL out: it is a secret.
    [[ -z "${BACKUP_HEARTBEAT_URL:-}" || "$BACKUP_HEARTBEAT_URL" =~ $BACKUP_HEARTBEAT_RE ]] \
        || die "BACKUP_HEARTBEAT_URL is not a URL like https://host/path (http only to 127.0.0.1 or localhost)"
    # ${extra[@]+...}: an empty array is "unbound" under set -u in macOS bash 3.2.
    for name in "${BACKUP_SETTINGS[@]}" ${extra[@]+"${extra[@]}"}; do
        value="${!name}"
        [[ "$value" != *"'"* && "$value" != *$'\n'* && "$value" != *$'\r'* ]] \
            || die "$name contains a quote or a line break"
        printf "%s='%s'\n" "$name" "$value"
    done
}

# Built before anything touches the server, so a missing input changes nothing.
ENV_BODY="$(render_env)"
BACKUP_ENV_BODY="$(render_backup_env)"

LOCAL_TMP="$(mktemp -d)"
LOGGED_IN=0
# shellcheck disable=SC2329  # called by the EXIT trap
cleanup() {
    if ((LOGGED_IN)); then
        ssh_run "docker logout '$REGISTRY'" >/dev/null || printf 'WARNING: docker logout failed on the server\n' >&2
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

# Remote commands are built on this side on purpose. Every variable in them
# was checked against a fixed pattern above or below.
# shellcheck disable=SC2029
ssh_run() { ssh "${SSH_OPTS[@]}" "$DEPLOY_USER@$DEPLOY_HOST" "$@"; }
release() { ssh_run "cd $APP_DIR && deploy/release.sh $*"; }

registry_logout() {
    if ((LOGGED_IN)); then
        ssh_run "docker logout '$REGISTRY'" >/dev/null
        LOGGED_IN=0
        log "logged out of $REGISTRY on the server"
    fi
}

# https://API_HOSTNAME/healthz through Caddy, retried until the time limit.
healthy() {
    local deadline=$((SECONDS + ${HEALTH_TIMEOUT_SECONDS:-180}))
    local args=(-fsS --max-time 10 -o "$LOCAL_TMP/healthz")
    [[ -n "${HEALTH_CHECK_RESOLVE:-}" ]] && args+=(--resolve "$HEALTH_CHECK_RESOLVE")
    # Caddy's own CA on test targets (CADDY_TLS=internal); verified otherwise.
    [[ "${CADDY_TLS:-public}" == internal ]] && args+=(-k)
    log "health check https://$API_HOSTNAME/healthz (up to ${HEALTH_TIMEOUT_SECONDS:-180}s)"
    while :; do
        if curl "${args[@]}" "https://$API_HOSTNAME/healthz" 2>/dev/null \
            && grep -q '"status":"ok"' "$LOCAL_TMP/healthz"; then
            log "/healthz OK"
            return 0
        fi
        if ((SECONDS >= deadline)); then
            log "/healthz not OK after ${HEALTH_TIMEOUT_SECONDS:-180}s"
            return 1
        fi
        sleep 3
    done
}

log "deploying $API_IMAGE to $DEPLOY_USER@$DEPLOY_HOST"

log "upload files"
UPLOAD="$(ssh_run "mktemp -d $APP_DIR/.upload.XXXXXXXX")"
[[ "$UPLOAD" =~ ^$APP_DIR/\.upload\.[A-Za-z0-9]+$ ]] || die "unexpected upload directory name from the server"
tar -C "$REPO_DIR" -cf - "${UPLOAD_FILES[@]}" | ssh_run "tar -C '$UPLOAD' -xf -"
# The uploaded copy installs the files, including the new release.sh.
ssh_run "cd $APP_DIR && bash '$UPLOAD/deploy/release.sh' install-files '$UPLOAD'"

log "write .env"
printf '%s\n' "$ENV_BODY" | release write-env "'$API_IMAGE'"

if [[ -n "$BACKUP_ENV_BODY" ]]; then
    log "write backup.env (nightly backups on)"
    printf '%s\n' "$BACKUP_ENV_BODY" | release backup-config
else
    log "no backup settings: nightly backups off"
    release backup-config </dev/null
fi

if [[ -n "${REGISTRY_TOKEN:-}" ]]; then
    log "docker login $REGISTRY on the server (token over stdin)"
    printf '%s\n' "$REGISTRY_TOKEN" \
        | ssh_run "docker login '$REGISTRY' -u '$REGISTRY_USER' --password-stdin" >/dev/null
    LOGGED_IN=1
fi

UP_ARGS=()
[[ "${SKIP_MIGRATIONS:-false}" == true ]] && UP_ARGS+=(--no-migrate)

if release up ${UP_ARGS[@]+"${UP_ARGS[@]}"} && registry_logout && healthy && release verify; then
    release record
    release status
    log "deployed $API_IMAGE"
    exit 0
fi

log "deploy of $API_IMAGE failed: rolling back"
registry_logout || true
if release rollback && healthy && release verify; then
    release status
    log "rolled back; the earlier release is running and healthy"
    exit 1
fi
release status || true
die "rollback failed or there was no earlier release: no healthy release running"
