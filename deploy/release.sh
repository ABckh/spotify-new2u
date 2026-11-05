#!/usr/bin/env bash
# Server side of a deploy (WHO-176). Runs as the `deploy` user in /srv/new2u;
# scripts/deploy-remote.sh calls it over SSH, step by step:
#
#   release.sh install-files DIR   move the compose files, the Caddyfile and this
#                                  script from DIR (an upload) into place
#   release.sh write-env IMAGE     .env (0600) = stdin + API_IMAGE=IMAGE
#   release.sh backup-config       backup.env (0600) = stdin; empty stdin removes
#                                  it, which turns the nightly backup off (WHO-181)
#   release.sh up                  pull, migrate, start what .env names
#   release.sh verify              api and poller run API_IMAGE and do not restart,
#                                  and no port beyond 80/443 is public
#   release.sh public-ports        the ports that would fail verify (no cd, no .env)
#   release.sh record              API_IMAGE is good: current -> previous, it -> current
#   release.sh rollback            run the image in `current` again (no migration)
#   release.sh status              current, previous and what .env names
#
# By hand on the server, a rollback to a given image (docs/self-hosting.md "Roll back"):
#
#   cd /srv/new2u && deploy/release.sh set-image ghcr.io/OWNER/new2u@sha256:DIGEST \
#       && deploy/release.sh up --no-pull --no-migrate && deploy/release.sh record
#
# `current` and `previous` each hold one image reference, by digest
# (REPO@sha256:..., WHO-177 finding 9): a deploy only takes digests, so a
# rollback runs exactly the bytes that passed. `current` only changes after a
# health check passed, so it is always the last release that worked.
#
# Never prints .env: it holds the secrets.

set -euo pipefail

readonly APP_DIR=/srv/new2u
readonly COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.vps.yml)
readonly FILES=(docker-compose.yml docker-compose.vps.yml deploy/caddy/Caddyfile deploy/release.sh deploy/backup.sh)
# Set by install-files when the Caddyfile changed; `up` then recreates caddy,
# because a bind-mounted file keeps pointing at the old copy.
readonly CADDY_CHANGED="$APP_DIR/.caddy-changed"
# GHCR (or any registry) image with a tag or digest; nothing a shell would read.
readonly IMAGE_RE='^[a-z0-9][a-z0-9._:/-]*(:[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$'

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

check_image() { [[ "$1" =~ $IMAGE_RE ]] || die "not an image reference: $1"; }

# The API_IMAGE line of .env, without reading anything else out loud.
env_image() { sed -n 's/^API_IMAGE=//p' .env | tail -n 1; }

# Replace .env atomically and keep it 0600: a temporary file in the same
# directory, then rename.
replace_env() {
    local tmp
    tmp="$(mktemp "$APP_DIR/.env.XXXXXXXX")"
    chmod 0600 "$tmp"
    cat >"$tmp"
    if [[ -f .env ]] && cmp -s "$tmp" .env; then
        rm -f "$tmp"
        log ".env unchanged"
    else
        mv -f "$tmp" .env
        log ".env written"
    fi
}

install_files() {
    local src="${1:?usage: release.sh install-files DIR}" f
    [[ "$src" == "$APP_DIR"/.upload.* ]] || die "upload directory must be $APP_DIR/.upload.*"
    for f in "${FILES[@]}"; do
        [[ -f "$src/$f" ]] || die "upload is missing $f"
        if [[ -f "$f" ]] && cmp -s "$src/$f" "$f"; then
            continue
        fi
        install -D -m "$([[ "$f" == *.sh ]] && echo 0755 || echo 0644)" "$src/$f" "$f"
        log "$f updated"
        if [[ "$f" == deploy/caddy/Caddyfile ]]; then
            touch "$CADDY_CHANGED"
        fi
    done
    rm -rf "$src"
}

write_env() {
    local image="${1:?usage: release.sh write-env IMAGE}"
    check_image "$image"
    { grep -v '^API_IMAGE=' || true; printf 'API_IMAGE=%s\n' "$image"; } | replace_env
}

# deploy/backup.sh's settings, kept out of .env: compose hands .env to the api
# and poller containers, and they have no use for the bucket key.
backup_config() {
    local body tmp
    body="$(cat)"
    if [[ -z "$body" ]]; then
        if [[ -e backup.env ]]; then
            rm -f backup.env
            log "backup.env removed: nightly backups off"
        else
            log "backups not configured"
        fi
        return
    fi
    tmp="$(mktemp "$APP_DIR/.backup.env.XXXXXXXX")"
    chmod 0600 "$tmp"
    printf '%s\n' "$body" >"$tmp"
    if [[ -f backup.env ]] && cmp -s "$tmp" backup.env; then
        rm -f "$tmp"
        log "backup.env unchanged"
    else
        mv -f "$tmp" backup.env
        log "backup.env written: nightly backups on"
    fi
}

set_image() {
    local image="${1:?usage: release.sh set-image IMAGE}"
    check_image "$image"
    [[ -f .env ]] || die ".env missing: run a full deploy first"
    { grep -v '^API_IMAGE=' .env || true; printf 'API_IMAGE=%s\n' "$image"; } | replace_env
}

up() {
    local pull=1 migrate=1 arg
    for arg in "$@"; do
        case "$arg" in
            --no-pull) pull=0 ;;
            --no-migrate) migrate=0 ;;
            *) die "up: unknown option $arg" ;;
        esac
    done
    log "image $(env_image)"
    if ((pull)); then
        "${COMPOSE[@]}" pull --quiet api poller
    fi
    if ((migrate)); then
        # Forward only. Runs before the new code starts, so every migration
        # must also work with the release before it (docs/self-hosting.md "Migrations").
        log "migrations"
        "${COMPOSE[@]}" run --rm --no-TTY api alembic upgrade head
    fi
    "${COMPOSE[@]}" up -d --remove-orphans
    if [[ -e "$CADDY_CHANGED" ]]; then
        log "Caddyfile changed: recreating caddy"
        "${COMPOSE[@]}" up -d --no-deps --force-recreate caddy
        rm -f "$CADDY_CHANGED"
    fi
}

# Run after the health check: a poller that crashes on start is as bad as an
# API that does. Both must run the image in .env, be running, and not restart
# during VERIFY_SECONDS (an older restart, days ago, does not count).
verify() {
    local image service id pass cfg status restarts public
    local -A first=()
    image="$(env_image)"
    for pass in 1 2; do
        for service in api poller; do
            id="$("${COMPOSE[@]}" ps -q "$service")"
            [[ -n "$id" ]] || die "$service is not running"
            read -r cfg status restarts < <(docker inspect -f '{{.Config.Image}} {{.State.Status}} {{.RestartCount}}' "$id")
            [[ "$cfg" == "$image" ]] || die "$service runs $cfg, not $image"
            [[ "$status" == running ]] || die "$service is $status"
            if ((pass == 1)); then
                first[$service]="$id $restarts"
            elif [[ "${first[$service]}" != "$id $restarts" ]]; then
                die "$service restarted during the check"
            fi
        done
        ((pass == 2)) || sleep "${VERIFY_SECONDS:-10}"
    done
    log "api and poller run $image"
    public="$(public_ports)"
    [[ -z "$public" ]] || die "a port beyond 80/443 is published on a public address: $(tr '\n' ';' <<<"$public")"
    log "nothing published beyond 80/443 on a public address"
}

# Container port mappings on a non-loopback address, other than 80/443, one
# per line (empty: none). The same rule as deploy/harden.sh --check, which
# needs root; this one only needs `docker ps`, so every deploy runs it (WHO-177
# finding 6). `docker ps` prints e.g.
# "0.0.0.0:80->80/tcp, [::]:80->80/tcp, 127.0.0.1:8000->8000/tcp".
public_ports() {
    local containers name ports hostside addr port
    containers="$(docker ps --format '{{.Names}} {{.Ports}}')" || die "docker ps failed, cannot check published ports"
    while read -r name ports; do
        [[ -n "$name" ]] || continue
        while read -r hostside; do
            [[ "$hostside" == *'->'* ]] || continue
            hostside="${hostside%%->*}"
            addr=${hostside%:*}
            port=${hostside##*:}
            case "$addr" in 127.* | '[::1]') continue ;; esac
            case "$port" in 80 | 443) continue ;; esac
            printf '%s publishes %s\n' "$name" "$hostside"
        done < <(tr ',' '\n' <<<"$ports" | sed 's/^ *//')
    done <<<"$containers"
}

record() {
    local image current=""
    image="$(env_image)"
    check_image "$image"
    [[ -f current ]] && current="$(<current)"
    if [[ "$current" == "$image" ]]; then
        log "release unchanged: $image"
        return
    fi
    if [[ -n "$current" ]]; then
        printf '%s\n' "$current" >previous
    fi
    printf '%s\n' "$image" >current
    log "release recorded: current $image, previous ${current:-none}"
}

rollback() {
    local current
    [[ -f current ]] || die "no earlier release to roll back to"
    current="$(<current)"
    log "rolling back to $current"
    set_image "$current"
    # The image is still on the server (it ran until a moment ago). Migrations
    # are forward only, so they are not run back.
    up --no-pull --no-migrate
}

status() {
    printf 'current  %s\n' "$(cat current 2>/dev/null || echo none)"
    printf 'previous %s\n' "$(cat previous 2>/dev/null || echo none)"
    printf '.env     %s\n' "$(env_image)"
}

cmd="${1:-}"
shift || true
if [[ "$cmd" == public-ports ]]; then
    public_ports
    exit 0
fi
cd "$APP_DIR"
umask 077
case "$cmd" in
    install-files) install_files "$@" ;;
    write-env) write_env "$@" ;;
    backup-config) backup_config ;;
    set-image) set_image "$@" ;;
    up) up "$@" ;;
    verify) verify ;;
    record) record ;;
    rollback) rollback ;;
    status) status ;;
    *) die "usage: release.sh install-files|write-env|backup-config|set-image|up|verify|public-ports|record|rollback|status" ;;
esac
