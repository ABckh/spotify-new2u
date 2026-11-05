#!/usr/bin/env bash
# CI only (WHO-176): the deploy half of the throwaway-server job. Runs after
# scripts/ci/bootstrap-target.sh setup and two bootstrap runs, on the same
# runner, which then plays a bootstrapped server. scripts/deploy-remote.sh is
# run between these steps exactly as the production job runs it; only the
# inputs differ.
#
#   scripts/ci/deploy-target.sh images      registry with a password, three images
#   scripts/ci/deploy-target.sh inputs      deploy inputs for the next steps
#   scripts/ci/deploy-target.sh checks      the deployed stack, as part 1 checked it
#   scripts/ci/deploy-target.sh snapshot F  container ids and release files into F
#   scripts/ci/deploy-target.sh same F      ... and fail when they differ from F now
#   scripts/ci/deploy-target.sh release CURRENT PREVIOUS   the release files hold these
#
# Every key, password and secret here is generated on the runner and dies with it.

set -euo pipefail

[[ "${GITHUB_ACTIONS:-}" == true ]] || { echo "CI only, see the header" >&2; exit 1; }

readonly KEYS="$RUNNER_TEMP/keys"
readonly APP_DIR=/srv/new2u
readonly HOSTNAME_UNDER_TEST=api.bootstrap.test
readonly REGISTRY=127.0.0.1:5000
readonly REGISTRY_USER=ci-deploy
readonly COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.vps.yml)
# registry:2.8.3 and httpd:2.4.65-alpine (for htpasswd), by index digest.
readonly REGISTRY_IMAGE=registry@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373
readonly HTTPD_IMAGE=httpd@sha256:07b2fabb7029a0b8aeb2e0fd02651c28fe22c21c5b5a59d6ff5b022791fcd89e

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# A generated value, masked in every later line of the log, for the next steps.
secret_env() {
    echo "::add-mask::$2"
    echo "$1=$2" >>"$GITHUB_ENV"
}

image() { echo "$REGISTRY/new2u:$1-$GITHUB_SHA"; }

# Push REF:TAG and print REF@sha256:... from the digest the push printed
# ("TAG: digest: sha256:... size: N"). Same parsing as ci.yml's image job.
push_digest() {
    local ref=$1 out digest
    out="$(docker push "$ref")"
    digest="$(sed -n 's/^.*: digest: \(sha256:[0-9a-f]\{64\}\) size: [0-9]*$/\1/p' <<<"$out" | tail -n 1)"
    [[ -n "$digest" ]] || die "no digest in the push output of $ref"
    echo "${ref%:*}@$digest"
}

images() {
    local password good1 good2 broken
    password="$(openssl rand -hex 24)"
    echo "::add-mask::$password"
    mkdir -p "$RUNNER_TEMP/registry"
    docker run --rm --entrypoint htpasswd "$HTTPD_IMAGE" -Bbn "$REGISTRY_USER" "$password" \
        >"$RUNNER_TEMP/registry/htpasswd"

    log "registry on $REGISTRY, password required (stands in for GHCR)"
    docker run -d --name ci-registry --restart unless-stopped -p "$REGISTRY:5000" \
        -v "$RUNNER_TEMP/registry/htpasswd:/auth/htpasswd:ro" \
        -e REGISTRY_AUTH=htpasswd -e REGISTRY_AUTH_HTPASSWD_REALM=ci \
        -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd \
        "$REGISTRY_IMAGE" >/dev/null

    good1="$(image good1)"
    good2="$(image good2)"
    broken="$(image broken)"
    log "good1: this commit"
    docker build -q -t "$good1" .
    log "good2: the same code, another image (a second release)"
    printf 'FROM %s\nLABEL ci.release=good2\n' "$good1" | docker build -q -t "$good2" -
    log "broken: migrations work, the API does not start"
    printf 'FROM %s\nUSER root\nRUN echo '\''raise SystemExit("broken on purpose: CI rollback test")'\'' > /srv/src/app/api/main.py\nUSER app\n' \
        "$good1" | docker build -q -t "$broken" -

    for _ in 1 2 3 4 5 6 7 8 9 10; do
        curl -s -o /dev/null "http://$REGISTRY/v2/" && break
        sleep 1
    done
    printf '%s\n' "$password" | docker login "$REGISTRY" -u "$REGISTRY_USER" --password-stdin >/dev/null
    # Deployed by the digest each push printed, as the production image job
    # does (WHO-177 finding 9).
    for name in good1 good2 broken; do
        echo "IMAGE_${name^^}=$(push_digest "$(image "$name")")" >>"$GITHUB_ENV"
    done
    docker logout "$REGISTRY" >/dev/null
    # Only the registry has them now: the deploy must really pull.
    docker image rm -f "$good1" "$good2" "$broken" >/dev/null

    echo "REGISTRY_USER=$REGISTRY_USER" >>"$GITHUB_ENV"
    secret_env REGISTRY_TOKEN "$password"
}

# The inputs the production job takes from GitHub: plain values as variables,
# generated ones masked like secrets.
inputs() {
    {
        echo "DEPLOY_USER=deploy"
        echo "DEPLOY_SSH_KEY_FILE=$KEYS/deploy"
        echo "API_HOSTNAME=$HOSTNAME_UNDER_TEST"
        echo "CADDY_TLS=internal"
        echo "HEALTH_CHECK_RESOLVE=$HOSTNAME_UNDER_TEST:443:127.0.0.1"
        echo "SPOTIFY_REDIRECT_URI=https://$HOSTNAME_UNDER_TEST/auth/spotify/callback"
        echo "APP_CALLBACK_URL=new2u://signed-in"
    } >>"$GITHUB_ENV"
    secret_env POSTGRES_PASSWORD "$(openssl rand -hex 16)"
    secret_env TOKEN_ENCRYPTION_KEY "$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
    secret_env SESSION_TOKEN_SECRET "$(openssl rand -hex 24)"
    secret_env SPOTIFY_CLIENT_ID "ci-client-$(openssl rand -hex 8)"
    secret_env SPOTIFY_CLIENT_SECRET "$(openssl rand -hex 16)"
    secret_env SPOTIFY_ALLOWED_ACCOUNT_IDS "ci-account-$(openssl rand -hex 4)"
}

checks() {
    log ".env: deploy-owned, 0600"
    [[ "$(sudo stat -c '%U:%G %a' "$APP_DIR/.env")" == "deploy:deploy 600" ]] || die "$APP_DIR/.env owner/mode"
    sudo grep -qx "APP_ENV='production'" "$APP_DIR/.env" || die "APP_ENV is not production"

    log "no registry login left on the server"
    if sudo test -f ~deploy/.docker/config.json && sudo grep -q "$REGISTRY" ~deploy/.docker/config.json; then
        die "the deploy user is still logged in to $REGISTRY"
    fi

    log "http redirects to https"
    [[ "$(curl -s -o /dev/null -w '%{http_code}' --resolve "$HOSTNAME_UNDER_TEST:80:127.0.0.1" \
        "http://$HOSTNAME_UNDER_TEST/healthz")" == 308 ]] || die "http does not redirect to https"

    log "only Caddy publishes; uvicorn believes Caddy only"
    [[ "$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' new2u-caddy-1)" == 10.201.0.2 ]] \
        || die "caddy is not on 10.201.0.2"
    [[ "$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' new2u-api-1 | grep '^FORWARDED_ALLOW_IPS=')" \
        == FORWARDED_ALLOW_IPS=10.201.0.2 ]] || die "api FORWARDED_ALLOW_IPS"
    for c in new2u-postgres-1 new2u-api-1 new2u-poller-1; do
        [[ "$(docker inspect -f '{{json .NetworkSettings.Ports}}' "$c")" != *HostPort* ]] || die "$c publishes a port"
    done
    sudo deploy/harden.sh --check

    log "logs go to journald"
    for c in new2u-postgres-1 new2u-api-1 new2u-poller-1 new2u-caddy-1; do
        [[ "$(docker inspect -f '{{.HostConfig.LogConfig.Type}}' "$c")" == journald ]] || die "$c logging"
    done

    log "HSTS on /healthz (WHO-177 finding 4)"
    local headers
    headers="$(curl -fsS -k -o /dev/null -D - --resolve "$HOSTNAME_UNDER_TEST:443:127.0.0.1" \
        "https://$HOSTNAME_UNDER_TEST/healthz")"
    grep -qix 'strict-transport-security: max-age=31536000'$'\r' <<<"$headers" \
        || die "no Strict-Transport-Security: max-age=31536000 on /healthz"

    log "a client that sends no SNI (every client of an IP address) gets a certificate"
    # https://127.0.0.1 sends no SNI; Caddy answers with default_sni's
    # certificate. Without default_sni the handshake fails (curl prints 000).
    [[ "$(curl -s -k -o /dev/null -w '%{http_code}' -H "Host: $HOSTNAME_UNDER_TEST" https://127.0.0.1/healthz)" == 200 ]] \
        || die "no TLS for a client without SNI"

    log "Caddy's journal keeps no request URI when the API is down (WHO-177 finding 3)"
    local probe matches
    probe="probe-$(openssl rand -hex 6)"
    deploy_ssh "cd $APP_DIR && ${COMPOSE[*]} stop api" >/dev/null
    # 502 from Caddy: reverse_proxy fails, which is when its error log fires.
    curl -s -k -o /dev/null --resolve "$HOSTNAME_UNDER_TEST:443:127.0.0.1" \
        "https://$HOSTNAME_UNDER_TEST/healthz?probe=$probe" || true
    deploy_ssh "cd $APP_DIR && ${COMPOSE[*]} start api" >/dev/null
    sleep 2
    matches="$(sudo journalctl -q --no-pager CONTAINER_NAME=new2u-caddy-1 | grep -c "$probe" || true)"
    [[ "$matches" == 0 ]] || die "Caddy logged the probe URI $matches time(s)"
    wait_healthy

    log "release.sh verify fails when a port beyond 80/443 is public (WHO-177 finding 6)"
    local out
    docker run -d --name ci-public-port -p 8099:5000 "$REGISTRY_IMAGE" >/dev/null
    if out="$(deploy_ssh "cd $APP_DIR && VERIFY_SECONDS=1 deploy/release.sh verify" 2>&1)"; then
        docker rm -f ci-public-port >/dev/null
        die "release.sh verify passed with a container on 0.0.0.0:8099"
    fi
    docker rm -f ci-public-port >/dev/null
    grep -q 'ci-public-port publishes 0.0.0.0:8099' <<<"$out" || die "verify failed for another reason: $out"
    deploy_ssh "cd $APP_DIR && VERIFY_SECONDS=1 deploy/release.sh verify"

    log "migrations ran"
    docker exec new2u-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select version_num from alembic_version"' \
        | grep -Eq '^[0-9a-z_]+$' || die "alembic_version is empty"
}

deploy_ssh() {
    ssh -i "$KEYS/deploy" -o IdentitiesOnly=yes -o BatchMode=yes \
        -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$KEYS/known_hosts" \
        deploy@127.0.0.1 "$@"
}

# /healthz through Caddy again, after the API was stopped and started.
wait_healthy() {
    local _
    for _ in $(seq 1 60); do
        curl -fsS -k -o /dev/null --resolve "$HOSTNAME_UNDER_TEST:443:127.0.0.1" \
            "https://$HOSTNAME_UNDER_TEST/healthz" 2>/dev/null && return 0
        sleep 2
    done
    die "/healthz not OK 120s after starting api again"
}

state() {
    local c
    for c in new2u-postgres-1 new2u-api-1 new2u-poller-1 new2u-caddy-1; do
        docker inspect -f '{{.Name}} {{.Id}} {{.Config.Image}} {{.RestartCount}}' "$c"
    done
    sudo cat "$APP_DIR/current" "$APP_DIR/previous" 2>/dev/null || true
    sudo sha256sum "$APP_DIR/.env" "$APP_DIR/docker-compose.yml" "$APP_DIR/docker-compose.vps.yml" \
        "$APP_DIR/deploy/caddy/Caddyfile" "$APP_DIR/deploy/release.sh"
}

snapshot() { state >"${1:?file}"; }

same() {
    local now="$RUNNER_TEMP/state-now"
    state >"$now"
    diff -u "${1:?file}" "$now" || die "the second deploy changed something"
    log "nothing changed: same containers, same files, same release"
}

release() {
    local want_current="${1:?current}" want_previous="${2:-none}" current previous
    current="$(sudo cat "$APP_DIR/current")"
    previous="$(sudo cat "$APP_DIR/previous" 2>/dev/null || echo none)"
    [[ "$current" == "$want_current" ]] || die "current is $current, expected $want_current"
    [[ "$previous" == "$want_previous" ]] || die "previous is $previous, expected $want_previous"
    [[ "$(docker inspect -f '{{.Config.Image}}' new2u-api-1)" == "$want_current" ]] || die "api does not run $want_current"
    [[ "$(docker inspect -f '{{.Config.Image}}' new2u-poller-1)" == "$want_current" ]] || die "poller does not run $want_current"
    curl -fsS -k --resolve "$HOSTNAME_UNDER_TEST:443:127.0.0.1" "https://$HOSTNAME_UNDER_TEST/healthz"
    echo
    log "current $current, previous $previous, /healthz OK"
}

cmd="${1:-}"
shift || true
case "$cmd" in
    images | inputs | checks) "$cmd" ;;
    snapshot | same | release) "$cmd" "$@" ;;
    *) die "usage: $0 images|inputs|checks|snapshot F|same F|release CURRENT [PREVIOUS]" ;;
esac
