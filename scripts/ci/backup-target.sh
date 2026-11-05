#!/usr/bin/env bash
# CI only (WHO-181): the backup half of the throwaway-server job. Runs after
# scripts/ci/deploy-target.sh, on the same runner, which by then is a
# bootstrapped and deployed server. MinIO stands in for the S3 bucket.
#
#   scripts/ci/backup-target.sh bucket     MinIO, a bucket, a key that cannot read, an age key pair,
#                                          a heartbeat listener
#   scripts/ci/backup-target.sh row        write a test row into the app database
#   scripts/ci/backup-target.sh run        start new2u-backup.service, as the timer does
#   scripts/ci/backup-target.sh restore    newest dump: download, decrypt, restore into an empty database
#   scripts/ci/backup-target.sh seed-old   put dumps 10, 8 and 6 days old (by name) and another file in the bucket
#   scripts/ci/backup-target.sh retention  after a run: only the first two of those are gone
#   scripts/ci/backup-target.sh fail       a run with the bucket down fails, visibly, and sends no heartbeat
#   scripts/ci/backup-target.sh no-delete  take delete off the key; put a 10-day-old dump in the bucket
#   scripts/ci/backup-target.sh kept       after a BACKUP_PRUNE=off run: the 10-day-old dump is still there
#   scripts/ci/backup-target.sh off        after a deploy without backup settings
#
# Every key and password here is generated on the runner and dies with it.

set -euo pipefail

[[ "${GITHUB_ACTIONS:-}" == true ]] || { echo "CI only, see the header" >&2; exit 1; }

readonly KEYS="$RUNNER_TEMP/keys"
readonly WORK="$RUNNER_TEMP/backup"
readonly APP_DIR=/srv/new2u
readonly BUCKET=new2u-backups-ci
readonly ENDPOINT=http://127.0.0.1:9000
# MinIO's own images are no longer published; Chainguard's builds of MinIO and
# its client, pinned by index digest. rclone: the image deploy/backup.sh uses.
readonly MINIO_IMAGE=cgr.dev/chainguard/minio@sha256:268d68a2a3b6fc65f905a61f84d7eab6345ce6bff14f551e2b028ec31e6a1341
readonly MC_IMAGE=cgr.dev/chainguard/minio-client@sha256:59f1924e417b6fcfdc0aca321e3bdebe8a56fd0efa7da80037d5eb65a203d687
RCLONE_IMAGE="$(sed -n 's/^readonly RCLONE_IMAGE=//p' deploy/backup.sh)"
readonly RCLONE_IMAGE
readonly PROBE_TZ=Pacific/Chatham
readonly HEARTBEAT_PORT=9099
readonly HEARTBEAT_LOG="$RUNNER_TEMP/heartbeat.log"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

secret_env() {
    echo "::add-mask::$2"
    echo "$1=$2" >>"$GITHUB_ENV"
}

# mc and rclone as the bucket's owner (MinIO's root user), never as the
# server's key. Credentials go in through the environment.
mc() {
    MC_HOST_ci="http://$MINIO_ROOT_USER:$MINIO_ROOT_PASSWORD@127.0.0.1:9000" \
        docker run --rm --network host -v "$WORK:/work" -e MC_HOST_ci "$MC_IMAGE" "$@"
}
owner_rclone() {
    RCLONE_CONFIG=/dev/null RCLONE_CONFIG_OWNER_TYPE=s3 RCLONE_CONFIG_OWNER_PROVIDER=Other \
        RCLONE_CONFIG_OWNER_ENDPOINT=$ENDPOINT RCLONE_CONFIG_OWNER_REGION=us-east-1 \
        RCLONE_CONFIG_OWNER_ACCESS_KEY_ID="$MINIO_ROOT_USER" \
        RCLONE_CONFIG_OWNER_SECRET_ACCESS_KEY="$MINIO_ROOT_PASSWORD" \
        docker run --rm --quiet --network host --user "$(id -u):$(id -g)" -v "$WORK:/work" \
            -e RCLONE_CONFIG -e RCLONE_CONFIG_OWNER_TYPE -e RCLONE_CONFIG_OWNER_PROVIDER \
            -e RCLONE_CONFIG_OWNER_ENDPOINT -e RCLONE_CONFIG_OWNER_REGION \
            -e RCLONE_CONFIG_OWNER_ACCESS_KEY_ID -e RCLONE_CONFIG_OWNER_SECRET_ACCESS_KEY \
            "$RCLONE_IMAGE" "$@"
}

# The server's key, as backup.sh holds it.
server_rclone() {
    RCLONE_CONFIG=/dev/null RCLONE_CONFIG_SERVER_TYPE=s3 RCLONE_CONFIG_SERVER_PROVIDER=Other \
        RCLONE_CONFIG_SERVER_ENDPOINT=$ENDPOINT RCLONE_CONFIG_SERVER_REGION=us-east-1 \
        RCLONE_CONFIG_SERVER_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY_ID" \
        RCLONE_CONFIG_SERVER_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_ACCESS_KEY" \
        docker run --rm --quiet --network host --user "$(id -u):$(id -g)" -v "$WORK:/work:ro" \
            -e RCLONE_CONFIG -e RCLONE_CONFIG_SERVER_TYPE -e RCLONE_CONFIG_SERVER_PROVIDER \
            -e RCLONE_CONFIG_SERVER_ENDPOINT -e RCLONE_CONFIG_SERVER_REGION \
            -e RCLONE_CONFIG_SERVER_ACCESS_KEY_ID -e RCLONE_CONFIG_SERVER_SECRET_ACCESS_KEY \
            "$RCLONE_IMAGE" "$@"
}

objects() { owner_rclone lsf --files-only "owner:$BUCKET/new2u/" | sort; }
dumps() { objects | grep -E '^new2u-[0-9]{8}T[0-9]{6}Z\.dump\.age$' || true; }

psql_app() {
    docker exec -i new2u-postgres-1 sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA'
}

start_minio() {
    docker run -d --name ci-minio --network host \
        -e MINIO_ROOT_USER -e MINIO_ROOT_PASSWORD \
        "$MINIO_IMAGE" server /tmp/data --address 127.0.0.1:9000 --console-address 127.0.0.1:9001 >/dev/null
    for _ in $(seq 30); do
        curl -fs -o /dev/null "$ENDPOINT/minio/health/ready" && return 0
        sleep 1
    done
    die "MinIO did not start"
}

bucket() {
    local key_id secret
    mkdir -p "$WORK/dl"
    MINIO_ROOT_USER="ci-root-$(openssl rand -hex 4)"
    MINIO_ROOT_PASSWORD="$(openssl rand -hex 24)"
    export MINIO_ROOT_USER MINIO_ROOT_PASSWORD
    echo "MINIO_ROOT_USER=$MINIO_ROOT_USER" >>"$GITHUB_ENV"
    secret_env MINIO_ROOT_PASSWORD "$MINIO_ROOT_PASSWORD"

    log "MinIO on $ENDPOINT, bucket $BUCKET"
    start_minio
    mc mb --quiet "ci/$BUCKET"

    log "the server's key: list, write and delete on $BUCKET; no read"
    cat >"$WORK/policy.json" <<EOF
{"Version": "2012-10-17", "Statement": [
  {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["arn:aws:s3:::$BUCKET"]},
  {"Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts"], "Resource": ["arn:aws:s3:::$BUCKET/*"]}]}
EOF
    key_id="ci-backup-$(openssl rand -hex 4)"
    secret="$(openssl rand -hex 24)"
    echo "::add-mask::$secret"
    mc admin policy create ci backup-writer /work/policy.json >/dev/null
    mc admin user add ci "$key_id" "$secret" >/dev/null
    mc admin policy attach ci backup-writer --user "$key_id" >/dev/null

    log "age key pair: the private half stays on the runner, as with the owner"
    install -d -m 0700 "$KEYS"
    age-keygen -o "$KEYS/backup-age.key" 2>/dev/null

    {
        echo "BACKUP_S3_ENDPOINT=$ENDPOINT"
        echo "BACKUP_S3_REGION=us-east-1"
        echo "BACKUP_S3_BUCKET=$BUCKET"
        echo "BACKUP_AGE_RECIPIENT=$(age-keygen -y "$KEYS/backup-age.key")"
    } >>"$GITHUB_ENV"
    secret_env BACKUP_S3_ACCESS_KEY_ID "$key_id"
    secret_env BACKUP_S3_SECRET_ACCESS_KEY "$secret"

    start_heartbeat
}

# A stand-in for a dead-man's-switch service: one line per request in
# HEARTBEAT_LOG (method, path, body). The path holds a random token, as a
# real ping URL does; the journal check looks for it.
start_heartbeat() {
    local token
    token="$(openssl rand -hex 16)"
    : >"$HEARTBEAT_LOG"
    log "heartbeat listener on 127.0.0.1:$HEARTBEAT_PORT"
    setsid nohup python3 - "$HEARTBEAT_PORT" "$HEARTBEAT_LOG" >"$RUNNER_TEMP/heartbeat.out" 2>&1 <<'EOF' &
import http.server, sys

class Ping(http.server.BaseHTTPRequestHandler):
    def handle_ping(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        with open(sys.argv[2], "a") as log:
            log.write(f"{self.command} {self.path} {body}\n")
        self.send_response(200)
        self.end_headers()

    do_GET = do_POST = handle_ping

    def log_message(self, *args: object) -> None:
        pass  # the request line holds the token

http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Ping).serve_forever()
EOF
    for _ in $(seq 20); do
        if curl -s -o /dev/null "http://127.0.0.1:$HEARTBEAT_PORT/ready"; then
            : >"$HEARTBEAT_LOG"
            secret_env BACKUP_HEARTBEAT_URL "http://127.0.0.1:$HEARTBEAT_PORT/ping/$token"
            return 0
        fi
        sleep 0.5
    done
    die "the heartbeat listener did not start"
}

heartbeats() { wc -l <"$HEARTBEAT_LOG"; }

row() {
    local id
    id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
    log "test row: app_user $id"
    printf "INSERT INTO app_user (id, home_timezone) VALUES ('%s', '%s');\n" "$id" "$PROBE_TZ" | psql_app >/dev/null
    echo "BACKUP_PROBE_ID=$id" >>"$GITHUB_ENV"
}

# The service the timer starts, started the same way. `systemctl start` waits
# for a oneshot service and fails when it fails.
run() {
    local before
    log "backup.env: deploy-owned, 0600"
    [[ "$(sudo stat -c '%U:%G %a' "$APP_DIR/backup.env")" == "deploy:deploy 600" ]] || die "backup.env owner/mode"
    before="$(heartbeats)"
    sudo systemctl start new2u-backup.service
    [[ "$(systemctl show -P Result new2u-backup.service)" == success ]] || die "the backup did not succeed"
    sudo journalctl -q --no-pager -u new2u-backup.service -n 20 --output=cat
    no_secret_in_journal
    (($(heartbeats) == before + 1)) || die "expected 1 heartbeat after the run, got $(($(heartbeats) - before))"
    tail -n 1 "$HEARTBEAT_LOG" | grep -qE '^POST /ping/[0-9a-f]{32} uploaded: new2u-[0-9TZ]+\.dump\.age sha256=[0-9a-f]{64} size=[0-9]+$' \
        || die "the heartbeat is not the uploaded: line"
    log "heartbeat: exactly 1, carrying the uploaded: line"
}

no_secret_in_journal() {
    local value
    for value in "$BACKUP_S3_SECRET_ACCESS_KEY" "$BACKUP_S3_ACCESS_KEY_ID" "$MINIO_ROOT_PASSWORD" \
        "$(grep '^AGE-SECRET-KEY-' "$KEYS/backup-age.key")" "$BACKUP_HEARTBEAT_URL" "${BACKUP_HEARTBEAT_URL##*/}"; do
        if sudo journalctl -q --no-pager -u new2u-backup.service --output=cat | grep -qF -- "$value"; then
            die "a secret is in the backup journal"
        fi
    done
    if sudo journalctl -q --no-pager -u new2u-backup.service --output=cat | grep -qF "$PROBE_TZ"; then
        die "database content is in the backup journal"
    fi
    log "journal: no secret, no database content"
}

restore() {
    local newest image sha
    newest="$(dumps | tail -n 1)"
    [[ -n "$newest" ]] || die "no dump in the bucket"
    log "download $newest with the owner's key"
    owner_rclone copyto "owner:$BUCKET/new2u/$newest" "/work/dl/$newest"
    [[ "$(head -c 21 "$WORK/dl/$newest")" == age-encryption.org/v1 ]] || die "the object is not an age file"
    if LC_ALL=C grep -qF "$PROBE_TZ" "$WORK/dl/$newest"; then die "the object holds plaintext"; fi

    # The runbook's step: the hash the server logged at upload, before decrypting.
    sha="$(sha256sum "$WORK/dl/$newest")"
    sha="${sha%% *}"
    sudo journalctl -q --no-pager -u new2u-backup.service --output=cat \
        | grep -qxF "uploaded: $newest sha256=$sha size=$(stat -c %s "$WORK/dl/$newest")" \
        || die "the downloaded file's sha256 and size are not the ones the server logged"
    log "sha256 $sha matches the journal's uploaded: line"

    log "decrypt with the private key"
    age -d -i "$KEYS/backup-age.key" -o "$WORK/dl/restore.dump" "$WORK/dl/$newest"

    log "restore into an empty database (same postgres image as the stack)"
    image="$(docker inspect -f '{{.Config.Image}}' new2u-postgres-1)"
    docker run -d --name ci-restore -e POSTGRES_PASSWORD=ci-restore -e POSTGRES_DB=restored "$image" >/dev/null
    # Ask over TCP: the image's init step runs a socket-only server that is
    # "ready" before POSTGRES_DB exists. Only the final server listens on TCP.
    for _ in $(seq 60); do
        docker exec ci-restore pg_isready -h 127.0.0.1 -U postgres -d restored -q && break
        sleep 1
    done
    docker exec ci-restore pg_isready -h 127.0.0.1 -U postgres -d restored -q \
        || die "the restore database did not start"
    [[ "$(docker exec ci-restore psql -U postgres -d restored -tAc 'select count(*) from pg_tables where schemaname = '\''public'\''')" == 0 ]] \
        || die "the restore target is not empty"
    docker exec -i ci-restore pg_restore -U postgres -d restored --no-owner --no-privileges --exit-on-error \
        <"$WORK/dl/restore.dump"

    [[ "$(docker exec ci-restore psql -U postgres -d restored -tAc "select home_timezone from app_user where id = '$BACKUP_PROBE_ID'")" == "$PROBE_TZ" ]] \
        || die "the test row is not in the restored database"
    [[ "$(docker exec ci-restore psql -U postgres -d restored -tAc 'select version_num from alembic_version')" \
        == "$(psql_app <<<'select version_num from alembic_version')" ]] || die "alembic_version differs"
    docker rm -f ci-restore >/dev/null
    rm -f "$WORK/dl/restore.dump"
    log "restored: the test row is there"
}

# Dump names carry their UTC time; retention reads it from the name.
stamp_days_ago() { date -u -d "$1 days ago" +%Y%m%dT%H%M%SZ; }

seed_old() {
    local days name
    : >"$WORK/dl/fake"
    for days in 10 8 6; do
        name="new2u-$(stamp_days_ago "$days").dump.age"
        owner_rclone copyto /work/dl/fake "owner:$BUCKET/new2u/$name"
        echo "BACKUP_OLD_$days=$name" >>"$GITHUB_ENV"
    done
    echo "not a dump" >"$WORK/dl/notes.txt"
    owner_rclone copyto /work/dl/notes.txt "owner:$BUCKET/new2u/notes.txt"
    log "bucket before the second run:"
    objects
}

retention() {
    local now
    now="$(objects)"
    log "bucket after the second run:"
    printf '%s\n' "$now"
    ! grep -qx "$BACKUP_OLD_10" <<<"$now" || die "the 10-day-old dump is still there"
    ! grep -qx "$BACKUP_OLD_8" <<<"$now" || die "the 8-day-old dump is still there"
    grep -qx "$BACKUP_OLD_6" <<<"$now" || die "the 6-day-old dump was deleted"
    grep -qx notes.txt <<<"$now" || die "a file that is not a dump was deleted"
    # Two real runs plus the 6-day-old one.
    (($(dumps | wc -l) == 3)) || die "expected 3 dumps, found $(dumps | wc -l)"
    sudo journalctl -q --no-pager -u new2u-backup.service --output=cat \
        | grep -qx 'retention: deleted 2 dump(s) older than 7 days: .*' || die "no retention line in the journal"
    log "retention: dumps older than 7 days deleted, the rest kept"
    log "the owner's check, as deploy"
    sudo -u deploy "$APP_DIR/deploy/backup.sh" check
}

fail() {
    local before
    log "bucket down: the run must fail and say so"
    before="$(heartbeats)"
    docker stop ci-minio >/dev/null
    if sudo systemctl start new2u-backup.service; then
        die "the backup passed with the bucket down"
    fi
    [[ "$(systemctl show -P Result new2u-backup.service)" == exit-code ]] || die "the service is not failed by exit code"
    systemctl is-failed -q new2u-backup.service || die "systemctl does not show it failed"
    sudo journalctl -q --no-pager -u new2u-backup.service -n 5 --output=cat | grep -q '^ERROR: upload failed$' \
        || die "no ERROR line in the journal"
    no_secret_in_journal
    (($(heartbeats) == before)) || die "a failed run sent a heartbeat"
    log "heartbeat: none after the failed run"
    # The partial file is removed; nothing encrypted or not is left behind.
    [[ -z "$(sudo find "$APP_DIR/.backup" -name '*.dump.age')" ]] || die "a dump file was left on the server"
    docker start ci-minio >/dev/null
    for _ in $(seq 30); do curl -fs -o /dev/null "$ENDPOINT/minio/health/ready" && break; sleep 1; done
    sudo systemctl reset-failed new2u-backup.service
    log "a failed run exits non-zero, systemctl shows failed, the journal has the error"
}

# WHO-182 N1: the key loses delete, as a B2 key without deleteFiles. The
# deploy after this sets BACKUP_PRUNE=off.
no_delete() {
    local name
    log "the server's key: list and write on $BUCKET; no read, no delete"
    cat >"$WORK/policy-nodelete.json" <<EOF
{"Version": "2012-10-17", "Statement": [
  {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["arn:aws:s3:::$BUCKET"]},
  {"Effect": "Allow", "Action": ["s3:PutObject", "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts"], "Resource": ["arn:aws:s3:::$BUCKET/*"]}]}
EOF
    mc admin policy create ci backup-writer-nodelete /work/policy-nodelete.json >/dev/null
    mc admin policy attach ci backup-writer-nodelete --user "$BACKUP_S3_ACCESS_KEY_ID" >/dev/null
    mc admin policy detach ci backup-writer --user "$BACKUP_S3_ACCESS_KEY_ID" >/dev/null

    name="new2u-$(stamp_days_ago 10).dump.age"
    : >"$WORK/dl/fake"
    owner_rclone copyto /work/dl/fake "owner:$BUCKET/new2u/$name"
    echo "BACKUP_OLD_10=$name" >>"$GITHUB_ENV"

    # The proof the policy holds: the server's key cannot delete it, by the
    # same call backup.sh's retention makes (which passed with the old key).
    printf '%s\n' "$name" >"$WORK/dl/delete.txt"
    if server_rclone delete --files-from-raw /work/dl/delete.txt --s3-no-head-object "server:$BUCKET/new2u/" 2>/dev/null; then
        die "the no-delete key deleted a dump"
    fi
    objects | grep -qx "$name" || die "the 10-day-old dump is gone"
    log "the key cannot delete: $name is still there"
}

kept() {
    grep -qx "BACKUP_PRUNE='off'" < <(sudo cat "$APP_DIR/backup.env") || die "backup.env has no BACKUP_PRUNE='off'"
    objects | grep -qx "$BACKUP_OLD_10" || die "the 10-day-old dump was deleted"
    sudo journalctl -q --no-pager -u new2u-backup.service -n 20 --output=cat \
        | grep -qx 'retention: left to the bucket lifecycle rule' || die "no 'left to the bucket lifecycle rule' line"
    log "BACKUP_PRUNE=off: the run passed without delete, the 10-day-old dump is kept"
}

off() {
    sudo test ! -e "$APP_DIR/backup.env" || die "backup.env is still there"
    sudo systemctl start new2u-backup.service
    [[ "$(systemctl show -P ConditionResult new2u-backup.service)" == no ]] || die "the service ran without backup.env"
    log "backups off: the service is skipped, nothing fails"
}

logs() {
    sudo journalctl -q --no-pager -u new2u-backup.service -n 50 --output=cat || true
    docker logs ci-minio 2>&1 | tail -n 20 || true
    cat "$RUNNER_TEMP/heartbeat.out" 2>/dev/null || true
}

case "${1:-}" in
    bucket | row | run | restore | retention | fail | kept | off | logs) "$1" ;;
    seed-old) seed_old ;;
    no-delete) no_delete ;;
    *) die "usage: $0 bucket|row|run|restore|seed-old|retention|fail|no-delete|kept|off|logs" ;;
esac
