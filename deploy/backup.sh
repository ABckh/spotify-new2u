#!/usr/bin/env bash
# Nightly encrypted database backup to an S3-compatible bucket (WHO-181).
# Runs as the `deploy` user in /srv/new2u. The systemd timer installed by
# deploy/bootstrap.sh starts it every night; the deploy uploads this script
# and writes its settings to /srv/new2u/backup.env (deploy/release.sh
# backup-config).
#
#   deploy/backup.sh run     dump, encrypt, upload, delete dumps older than 7 days, ping
#   deploy/backup.sh check   exit 0 when the bucket holds a dump younger than 26 hours
#
# `run`, step by step:
#   1. pg_dump -Fc of the app database, from the postgres container, piped
#      straight into `age -r BACKUP_AGE_RECIPIENT`. Only the encrypted file
#      touches the disk (/srv/new2u/.backup, 0700). The server holds the
#      public recipient only: it cannot decrypt a dump, not even its own.
#   2. Upload as new2u/new2u-<UTC time>.dump.age with rclone, in a
#      container pinned by digest. The key needs list and write on the bucket,
#      plus delete for step 3, never read (docs/self-hosting.md "Backups"). The journal gets
#      `uploaded: <name> sha256=<hex> size=<n>` of the encrypted file: before
#      decrypting a dump, the owner compares its hash with that line, so a
#      file someone else put in the bucket is never restored (WHO-182 N3).
#   3. With BACKUP_PRUNE=on (the default): delete every
#      new2u/new2u-<UTC time>.dump.age whose time in the name is more
#      than BACKUP_KEEP_DAYS (7) days ago. Only after step 2 passed, so the
#      newest dump is always one that just worked. With BACKUP_PRUNE=off the
#      bucket's lifecycle rule alone does this, and the key needs no delete
#      right, so it cannot wipe the backups (WHO-182 N1).
#   4. With BACKUP_HEARTBEAT_URL set: one request to it, only after all of the
#      above passed. A dead-man's-switch service (healthchecks.io or any
#      other) alerts when a night's ping does not arrive (WHO-182 N2). The
#      body is the `uploaded:` line, so the hash is kept off the server too.
#      The URL is a secret: never logged, never on a command line.
#
# Logs go to the journal through systemd: object names, sizes and counts only.
# Never a key, a secret, or anything from the database. Any failed step exits
# non-zero, so `systemctl status new2u-backup.service` shows "failed".

set -euo pipefail

readonly APP_DIR=/srv/new2u
readonly CONFIG="$APP_DIR/backup.env"
readonly WORK_DIR="$APP_DIR/.backup"
readonly COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.vps.yml)
# rclone 1.75.2, pinned by the digest of the multi-arch index (amd64 and arm64).
readonly RCLONE_IMAGE=rclone/rclone@sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5
readonly PREFIX=new2u
# new2u-20261025T031502Z.dump.age: the time is UTC, so names sort by time.
readonly NAME_RE='^new2u-([0-9]{8}T[0-9]{6}Z)\.dump\.age$'
readonly KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"
readonly CHECK_MAX_AGE_HOURS=26
readonly SETTINGS=(BACKUP_S3_ENDPOINT BACKUP_S3_REGION BACKUP_S3_BUCKET
    BACKUP_S3_ACCESS_KEY_ID BACKUP_S3_SECRET_ACCESS_KEY BACKUP_AGE_RECIPIENT)
# Optional, read in load_config: BACKUP_PRUNE, BACKUP_HEARTBEAT_URL (WHO-182).
# https, or plain http to this machine only (CI's MinIO). Same rule in
# scripts/deploy-remote.sh.
readonly ENDPOINT_RE='^(https://[A-Za-z0-9.-]+|http://(127\.0\.0\.1|localhost))(:[0-9]+)?/?$'
readonly HEARTBEAT_RE='^(https://[A-Za-z0-9.-]+|http://(127\.0\.0\.1|localhost))(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$'

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# backup.env holds NAME='value' lines, as deploy/release.sh writes them. Read
# line by line, never sourced: a value is data, not shell.
load_config() {
    local line name
    [[ -f "$CONFIG" ]] || die "$CONFIG missing: backups are not configured (docs/self-hosting.md \"Backups\")"
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" ]] && continue
        [[ "$line" =~ ^(BACKUP_[A-Z0-9_]+)=\'([^\']*)\'$ ]] || die "$CONFIG: unexpected line"
        printf -v "${BASH_REMATCH[1]}" '%s' "${BASH_REMATCH[2]}"
    done <"$CONFIG"
    for name in "${SETTINGS[@]}"; do
        [[ -n "${!name:-}" ]] || die "$CONFIG: $name is not set"
    done
    [[ "$BACKUP_AGE_RECIPIENT" =~ ^age1[02-9ac-hj-np-z]{58}$ ]] || die "BACKUP_AGE_RECIPIENT is not an age public key (age1...)"
    [[ "$BACKUP_S3_ENDPOINT" =~ $ENDPOINT_RE ]] || die "BACKUP_S3_ENDPOINT is not a URL like https://host (http only to 127.0.0.1 or localhost)"
    [[ "$BACKUP_S3_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || die "BACKUP_S3_BUCKET is not a bucket name"
    [[ "$KEEP_DAYS" =~ ^[1-9][0-9]*$ ]] || die "BACKUP_KEEP_DAYS must be a positive number"
    BACKUP_PRUNE="${BACKUP_PRUNE:-on}"
    [[ "$BACKUP_PRUNE" == on || "$BACKUP_PRUNE" == off ]] || die "BACKUP_PRUNE must be on or off"
    BACKUP_HEARTBEAT_URL="${BACKUP_HEARTBEAT_URL:-}"
    # The message leaves the URL out: it is a secret.
    [[ -z "$BACKUP_HEARTBEAT_URL" || "$BACKUP_HEARTBEAT_URL" =~ $HEARTBEAT_RE ]] \
        || die "BACKUP_HEARTBEAT_URL is not a URL like https://host/path (http only to 127.0.0.1 or localhost)"
}

# rclone with the settings in its environment: `-e NAME` hands the value to
# the container without putting it on a command line. No config file.
rclone() {
    RCLONE_CONFIG=/dev/null \
        RCLONE_CONFIG_BUCKET_TYPE=s3 \
        RCLONE_CONFIG_BUCKET_PROVIDER=Other \
        RCLONE_CONFIG_BUCKET_ENDPOINT="$BACKUP_S3_ENDPOINT" \
        RCLONE_CONFIG_BUCKET_REGION="$BACKUP_S3_REGION" \
        RCLONE_CONFIG_BUCKET_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY_ID" \
        RCLONE_CONFIG_BUCKET_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_ACCESS_KEY" \
        docker run --rm --quiet --network host --user "$(id -u):$(id -g)" \
            -v "$WORK_DIR:/work:ro" \
            -e RCLONE_CONFIG -e RCLONE_CONFIG_BUCKET_TYPE -e RCLONE_CONFIG_BUCKET_PROVIDER \
            -e RCLONE_CONFIG_BUCKET_ENDPOINT -e RCLONE_CONFIG_BUCKET_REGION \
            -e RCLONE_CONFIG_BUCKET_ACCESS_KEY_ID -e RCLONE_CONFIG_BUCKET_SECRET_ACCESS_KEY \
            "$RCLONE_IMAGE" "$@"
}

remote_dir() { printf 'bucket:%s/%s/' "$BACKUP_S3_BUCKET" "$PREFIX"; }

# Our dump names in the bucket, oldest first. Anything else there is ignored.
list_dumps() {
    local names
    names="$(rclone lsf --files-only "$(remote_dir)")" || die "cannot list the bucket"
    { grep -E "$NAME_RE" <<<"$names" || true; } | sort
}

# The UTC time in a dump's name as seconds since the epoch.
name_epoch() {
    local stamp
    [[ "$1" =~ $NAME_RE ]] || return 1
    stamp=${BASH_REMATCH[1]}
    date -u -d "${stamp:0:4}-${stamp:4:2}-${stamp:6:2} ${stamp:9:2}:${stamp:11:2}:${stamp:13:2}" +%s
}

# Delete our dumps older than KEEP_DAYS, never the one just uploaded ($1).
prune() {
    local cutoff dump old=()
    cutoff=$(($(date -u +%s) - KEEP_DAYS * 86400))
    while IFS= read -r dump; do
        [[ -n "$dump" && "$dump" != "$1" ]] || continue
        if (($(name_epoch "$dump") < cutoff)); then
            old+=("$dump")
        fi
    done < <(list_dumps)
    if ((${#old[@]})); then
        printf '%s\n' "${old[@]}" >"$WORK_DIR/delete.txt"
        # --files-from-raw deletes by name without reading the objects, which
        # the key may not do.
        rclone delete --files-from-raw /work/delete.txt --s3-no-head-object "$(remote_dir)" \
            || { rm -f "$WORK_DIR/delete.txt"; die "deleting dumps older than $KEEP_DAYS days failed"; }
        rm -f "$WORK_DIR/delete.txt"
    fi
    log "retention: deleted ${#old[@]} dump(s) older than $KEEP_DAYS days: ${old[*]:-none}"
}

# The URL and the body go to curl through its config on stdin, not argv.
# curl's own messages are dropped: some name the URL. A failed ping fails the
# run, so `systemctl --failed` shows it even when the service stays quiet.
heartbeat() {
    local rc=0
    printf 'url = "%s"\ndata-raw = "%s"\n' "$BACKUP_HEARTBEAT_URL" "$1" \
        | curl -fsS -m 10 --retry 3 -o /dev/null -K - 2>/dev/null || rc=$?
    ((rc == 0)) || die "heartbeat failed (curl exit $rc)"
    log "heartbeat: sent"
}

run() {
    local name file size sha uploaded
    load_config
    command -v age >/dev/null || die "age is not installed (deploy/bootstrap.sh installs it)"
    install -d -m 0700 "$WORK_DIR"
    # Leftovers of a run that was killed: encrypted, but no use any more.
    find "$WORK_DIR" -mindepth 1 -maxdepth 1 -name '*.dump.age' -delete

    name="new2u-$(date -u +%Y%m%dT%H%M%SZ).dump.age"
    file="$WORK_DIR/$name"
    log "dump: $name"
    # The postgres container here, or the database server in DATABASE_HOST
    # (docs/self-hosting.md "External database"): then a one-off postgres container is the
    # client, over TLS, with the password from its own environment, never a
    # command line. Compose starts a service named on the command line even
    # without its profile.
    local db_host dump
    db_host="$(sed -n "s/^DATABASE_HOST='\([^']*\)'\$/\1/p" "$APP_DIR/.env")"
    # shellcheck disable=SC2016  # expanded by the shell in the container
    if [[ -z "$db_host" ]]; then
        dump=("${COMPOSE[@]}" exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc')
    else
        dump=("${COMPOSE[@]}" run --rm --no-deps -T -e PGHOST="$db_host" -e PGSSLMODE=require
            -e PGCHANNELBINDING=require
            --entrypoint sh postgres -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc')
    fi
    # pipefail: a pg_dump that fails half-way fails the run, even though age
    # wrote a valid (short) file. pg_dump's errors name objects, not rows.
    if ! "${dump[@]}" | age -r "$BACKUP_AGE_RECIPIENT" -o "$file"; then
        rm -f "$file"
        die "pg_dump or age failed; nothing uploaded"
    fi
    size="$(stat -c %s "$file")"
    sha="$(sha256sum "$file")"
    sha="${sha%% *}"

    rclone copyto "/work/$name" "$(remote_dir)$name" \
        --s3-no-check-bucket --s3-no-head --no-check-dest \
        || { rm -f "$file"; die "upload failed"; }
    rm -f "$file"
    uploaded="uploaded: $name sha256=$sha size=$size"
    log "$uploaded"

    if [[ "$BACKUP_PRUNE" == on ]]; then
        prune "$name"
    else
        log "retention: left to the bucket lifecycle rule"
    fi
    log "backup OK: $name"
    if [[ -n "$BACKUP_HEARTBEAT_URL" ]]; then
        heartbeat "$uploaded"
    fi
}

check() {
    local newest age_hours
    load_config
    install -d -m 0700 "$WORK_DIR"
    newest="$(list_dumps | tail -n 1)"
    [[ -n "$newest" ]] || die "no dump in $BACKUP_S3_BUCKET/$PREFIX/"
    age_hours=$((($(date -u +%s) - $(name_epoch "$newest")) / 3600))
    log "newest dump: $newest ($age_hours hours old), $(list_dumps | wc -l) in the bucket"
    ((age_hours < CHECK_MAX_AGE_HOURS)) || die "the newest dump is older than $CHECK_MAX_AGE_HOURS hours"
    log "backup check OK"
}

cd "$APP_DIR"
umask 077
case "${1:-}" in
    run) run ;;
    check) check ;;
    *) die "usage: backup.sh run|check" ;;
esac
