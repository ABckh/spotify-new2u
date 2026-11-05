#!/usr/bin/env bash
# Make a fresh Ubuntu 24.04 server ready to run the new2u stack (WHO-175).
# Runs as root, from a copy of the deploy/ directory; scripts/bootstrap-remote.sh
# copies it over SSH and runs it through sudo:
#
#   sudo deploy/bootstrap.sh --deploy-pubkey FILE
#
# FILE holds the public key(s) the `deploy` user logs in with (one per line).
#
# Steps, in this order:
#   1. Provider firewall cleanup: remove iptables-persistent and its rules
#      files (Oracle's Ubuntu images ship them; they conflict with ufw).
#   2. deploy/harden.sh: ufw, sshd keys only, no root login,
#      unattended-upgrades, fail2ban, journald retention (WHO-54).
#      2b. Then, with ufw on, delete the provider's live REJECT rules.
#   3. Docker Engine and the Compose plugin from Docker's apt repository, its
#      signing key pinned by fingerprint.
#   4. The `deploy` user (key login only, group docker) and /srv/new2u.
#   5. The nightly backup job (WHO-181): `age`, and the systemd timer and
#      service from deploy/systemd/. The service runs only once a deploy has
#      written /srv/new2u/backup.env.
#
# Every step prints "[changed]" or "[ok]" and the run ends with "changed=N".
# Safe to run again: a second run changes nothing and prints changed=0.
# Plain Ubuntu tools only; nothing here is specific to one provider beyond
# undoing step 1's trap, which does nothing where it is not present.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly DEPLOY_USER=deploy
readonly APP_DIR=/srv/new2u
# Docker's Ubuntu repository signing key, from
# https://docs.docker.com/engine/install/ubuntu/ ("Docker Release (CE deb)").
readonly DOCKER_KEY_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
readonly DOCKER_KEY_URL=https://download.docker.com/linux/ubuntu/gpg
readonly DOCKER_KEYRING=/etc/apt/keyrings/docker.asc
readonly DOCKER_SOURCES=/etc/apt/sources.list.d/docker.sources
readonly DOCKER_PACKAGES=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
# docker-compose.vps.yml uses `!reset`, which needs Compose 2.24.
readonly COMPOSE_MIN_VERSION=2.24.0
readonly PROVIDER_FIREWALL_PACKAGES=(iptables-persistent netfilter-persistent)
readonly PROVIDER_RULES_FILES=(/etc/iptables/rules.v4 /etc/iptables/rules.v6)
readonly BACKUP_UNITS=(new2u-backup.service new2u-backup.timer)

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

CHANGED=0
# step_result NAME 0|1: print the step's result (1 = changed) and count it.
step_result() {
    if (($2)); then
        printf '[changed] %s\n' "$1"
        CHANGED=$((CHANGED + 1))
    else
        printf '[ok]      %s\n' "$1"
    fi
}

# Write stdin to $1 with mode $2 when the content differs.
# Returns 0 when the file changed, 1 when it was already up to date.
write_if_changed() {
    local path=$1 mode=$2 tmp
    tmp="$(mktemp)"
    cat >"$tmp"
    if [[ -f "$path" ]] && cmp -s "$tmp" "$path"; then
        rm -f "$tmp"
        return 1
    fi
    install -D -m "$mode" "$tmp" "$path"
    rm -f "$tmp"
    log "wrote $path"
    return 0
}

# Set owner (user:group) $2 and octal mode $3 on $1 where they differ.
# Returns 0 when it changed something, 1 when both were already right.
ensure_owner_mode() {
    local path=$1 owner=$2 mode=$3 rc=1
    if [[ "$(stat -c %U:%G "$path")" != "$owner" ]]; then
        chown "$owner" "$path"
        rc=0
    fi
    if [[ "$(stat -c %a "$path")" != "${mode#0}" ]]; then
        chmod "$mode" "$path"
        rc=0
    fi
    return $rc
}

package_installed() {
    [[ "$(dpkg-query -W -f '${db:Status-Status}' "$1" 2>/dev/null)" == installed ]]
}

usage() { die "usage: $0 --deploy-pubkey FILE"; }

# ---- 0. preflight ----------------------------------------------------------

PUBKEY_FILE=
parse_args() {
    while (($#)); do
        case "$1" in
            --deploy-pubkey)
                (($# >= 2)) || usage
                PUBKEY_FILE=$2
                shift 2
                ;;
            *) usage ;;
        esac
    done
    [[ -n "$PUBKEY_FILE" ]] || usage
}

# The deploy user's keys: plain public key lines only. No authorized_keys
# options (command=, from=, ...), so a pasted line cannot carry one.
read_pubkeys() {
    local line count=0
    [[ -s "$PUBKEY_FILE" ]] || die "$PUBKEY_FILE is empty or missing"
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        [[ "$line" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)\ [A-Za-z0-9+/]+=*(\ .*)?$ ]] \
            || die "$PUBKEY_FILE: not a plain SSH public key line: ${line:0:40}..."
        count=$((count + 1))
    done <"$PUBKEY_FILE"
    ((count > 0)) || die "$PUBKEY_FILE holds no public key"
    # ssh-keygen parses every key; a damaged one fails here.
    (($(ssh-keygen -l -f "$PUBKEY_FILE" | wc -l) == count)) \
        || die "$PUBKEY_FILE: ssh-keygen could not read every key"
}

preflight() {
    [[ $EUID -eq 0 ]] || die "run as root (sudo $0)"
    [[ -x "$SCRIPT_DIR/harden.sh" ]] || die "$SCRIPT_DIR/harden.sh is missing; copy the whole deploy/ directory"
    read_pubkeys
    # harden.sh's guards (Ubuntu, an SSH key for the caller, sshd on 22) run
    # before step 1, so a refusal leaves the server exactly as it was.
    "$SCRIPT_DIR/harden.sh" --preflight
}

# ---- 1. provider firewall cleanup ----------------------------------------

# Oracle's Ubuntu images load /etc/iptables/rules.v4 at boot through
# netfilter-persistent: INPUT allows 22 and then REJECTs everything, FORWARD
# REJECTs everything. Those REJECTs sit in front of ufw's chains, so 80/443
# would stay closed with ufw on, and iptables-persistent cannot be installed
# beside ufw anyway.
#
# This step removes the packages and the rules files only, so the trap does
# not come back at the next boot. The live REJECT rules stay until ufw is on
# (step 2b, after harden.sh): at every moment either they or ufw guard the
# host, even if harden.sh fails half-way (WHO-177 finding 5).
cleanup_provider_firewall() {
    local pkg file installed=() changed=0
    for pkg in "${PROVIDER_FIREWALL_PACKAGES[@]}"; do
        package_installed "$pkg" && installed+=("$pkg")
    done
    if ((${#installed[@]})); then
        log "removing ${installed[*]} (conflicts with ufw)"
        DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 purge -y -q "${installed[@]}"
        changed=1
    fi
    for file in "${PROVIDER_RULES_FILES[@]}"; do
        if [[ -f "$file" ]]; then
            rm -f "$file"
            log "removed $file"
            changed=1
        fi
    done
    step_result "provider firewall cleanup" "$changed"
}

# ---- 2b. live provider REJECT rules, once ufw guards the host ---------------

# SSH is never cut: only REJECT rules in INPUT and FORWARD are deleted (the
# chain policy is ACCEPT on these images, and Oracle's own "allow 22" rule
# stays), and ufw already allows 22. ufw never puts a REJECT there itself: its
# rules live in its own ufw-* chains. Oracle's other live rules (ACCEPTs, and
# the OUTPUT rules for 169.254.0.0/16) stay until the next reboot, after which
# only ufw's rules load. Runs every time, so a run that stopped between
# step 1 and here still gets the REJECTs out on the next one.
delete_provider_rejects() {
    local cmd chain status changed=0
    # Into a variable first: `ufw status | grep -q` can fail on SIGPIPE.
    status="$(ufw status)"
    grep -qx 'Status: active' <<<"$status" || die "ufw is not active; not deleting the provider's REJECT rules"
    for cmd in iptables ip6tables; do
        command -v "$cmd" >/dev/null || continue
        for chain in INPUT FORWARD; do
            # "-A INPUT -j REJECT --reject-with icmp-host-prohibited" becomes
            # "-D INPUT ...". xargs keeps quoted words (comments) together.
            if "$cmd" -S "$chain" | grep -qE '^-A .* -j REJECT'; then
                log "$cmd: deleting REJECT rules from $chain"
                "$cmd" -S "$chain" | grep -E '^-A .* -j REJECT' | sed 's/^-A /-D /' \
                    | xargs -r -L 1 "$cmd"
                changed=1
            fi
        done
    done
    step_result "provider REJECT rules" "$changed"
}

# ---- 2. harden.sh ----------------------------------------------------------

run_harden() {
    local out n
    out="$(mktemp)"
    # harden.sh prints its own [changed]/[ok] per step and ends with
    # "harden.sh changed=N"; its N counts toward the total.
    "$SCRIPT_DIR/harden.sh" | tee "$out"
    n="$(sed -n 's/^harden\.sh changed=\([0-9][0-9]*\)$/\1/p' "$out" | tail -n 1)"
    rm -f "$out"
    [[ -n "$n" ]] || die "harden.sh did not report its changes"
    CHANGED=$((CHANGED + n))
}

# ---- 3. Docker Engine + Compose plugin ----------------------------------

# Fingerprints of the primary keys in a key file, one per line.
key_fingerprints() {
    local home
    # A throwaway gpg home, so nothing is left in /root/.gnupg.
    home="$(mktemp -d)"
    GNUPGHOME="$home" gpg --batch --show-keys --with-colons "$1" 2>/dev/null \
        | awk -F: '$1 == "pub" {want = 1; next} $1 == "fpr" && want {print $10; want = 0}'
    rm -rf "$home"
}

docker_keyring_ok() {
    [[ -f "$DOCKER_KEYRING" && "$(key_fingerprints "$DOCKER_KEYRING")" == "$DOCKER_KEY_FINGERPRINT" ]]
}

compose_new_enough() {
    local version
    version="$(docker compose version --short 2>/dev/null)" || return 1
    dpkg --compare-versions "${version#v}" ge "$COMPOSE_MIN_VERSION"
}

install_docker() {
    local pkg tmp arch codename changed=0 missing=()
    arch="$(dpkg --print-architecture)"
    # shellcheck source=/dev/null
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"

    for pkg in ca-certificates curl gnupg; do
        package_installed "$pkg" || missing+=("$pkg")
    done
    if ((${#missing[@]})); then
        apt-get -o DPkg::Lock::Timeout=600 update -q
        DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q "${missing[@]}"
        changed=1
    fi

    # The key file is the whole trust anchor of the repository: take it only
    # when it holds exactly the one key with the expected fingerprint.
    if ! docker_keyring_ok; then
        tmp="$(mktemp)"
        curl -fsSL --proto '=https' --tlsv1.2 "$DOCKER_KEY_URL" -o "$tmp"
        if [[ "$(key_fingerprints "$tmp")" != "$DOCKER_KEY_FINGERPRINT" ]]; then
            rm -f "$tmp"
            die "Docker's signing key at $DOCKER_KEY_URL does not have fingerprint $DOCKER_KEY_FINGERPRINT. Nothing installed."
        fi
        install -D -m 0644 "$tmp" "$DOCKER_KEYRING"
        rm -f "$tmp"
        log "installed Docker's signing key ($DOCKER_KEY_FINGERPRINT)"
        changed=1
    fi

    if write_if_changed "$DOCKER_SOURCES" 0644 <<EOF; then
# Installed by deploy/bootstrap.sh (WHO-175).
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $codename
Components: stable
Architectures: $arch
Signed-By: $DOCKER_KEYRING
EOF
        changed=1
    fi

    missing=()
    for pkg in "${DOCKER_PACKAGES[@]}"; do
        package_installed "$pkg" || missing+=("$pkg")
    done
    if ((${#missing[@]})) || ! compose_new_enough; then
        log "installing ${DOCKER_PACKAGES[*]}"
        # Wait for the dpkg lock (up to 10 minutes): unattended-upgrades, enabled by
        # harden.sh a moment ago, may be running. Same on every apt-get here.
        apt-get -o DPkg::Lock::Timeout=600 update -q
        DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q "${DOCKER_PACKAGES[@]}"
        compose_new_enough || die "docker compose is older than $COMPOSE_MIN_VERSION after install"
        changed=1
    fi

    if ! systemctl is-enabled -q docker.service || ! systemctl is-active -q docker.service; then
        systemctl enable --now docker.service
        changed=1
    fi
    step_result "docker ($(docker --version | cut -d, -f1), compose $(docker compose version --short))" "$changed"
}

# ---- 4. deploy user and layout ------------------------------------------

# The deploy user runs `docker compose` and nothing else. Membership of the
# docker group is root-equivalent (it can start a container that mounts /);
# accepted for a single-user server, see docs/self-hosting.md "Server setup".
setup_deploy_user() {
    local home changed=0
    if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
        useradd --create-home --user-group --shell /bin/bash "$DEPLOY_USER"
        log "created user $DEPLOY_USER"
        changed=1
    fi
    # "*": no password can match, so key login only. Not "!" (locked), which
    # sshd refuses even for keys when PAM is off.
    if [[ "$(getent shadow "$DEPLOY_USER" | cut -d: -f2)" != '*' ]]; then
        usermod -p '*' "$DEPLOY_USER"
        changed=1
    fi
    if ! id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -qx docker; then
        usermod -aG docker "$DEPLOY_USER"
        log "added $DEPLOY_USER to group docker"
        changed=1
    fi

    home="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
    if [[ ! -d "$home/.ssh" ]]; then
        install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home/.ssh"
        changed=1
    fi
    ensure_owner_mode "$home/.ssh" "$DEPLOY_USER:$DEPLOY_USER" 0700 && changed=1
    # The file holds exactly the given keys: a key dropped from the input is
    # removed from the server on the next run.
    if grep -vE '^[[:space:]]*(#|$)' "$PUBKEY_FILE" | write_if_changed "$home/.ssh/authorized_keys" 0600; then
        changed=1
    fi
    ensure_owner_mode "$home/.ssh/authorized_keys" "$DEPLOY_USER:$DEPLOY_USER" 0600 && changed=1

    if [[ ! -d "$APP_DIR" ]]; then
        install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR"
        log "created $APP_DIR"
        changed=1
    fi
    ensure_owner_mode "$APP_DIR" "$DEPLOY_USER:$DEPLOY_USER" 0750 && changed=1
    # Secrets live here; the deploy step (WHO-174 part 2) fills it.
    if [[ ! -e "$APP_DIR/.env" ]]; then
        install -m 0600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /dev/null "$APP_DIR/.env"
        changed=1
    fi
    ensure_owner_mode "$APP_DIR/.env" "$DEPLOY_USER:$DEPLOY_USER" 0600 && changed=1

    step_result "deploy user and $APP_DIR" "$changed"
}

# ---- 5. backup job ---------------------------------------------------------

# Root installs the units; they run deploy/backup.sh as `deploy`, the script
# the deploy uploads. Settings come from backup.env, which the deploy writes
# (or removes when the backup settings are unset). Without it the timer still
# fires and the service is skipped by its ConditionPathExists.
setup_backup() {
    local unit changed=0 reload=0
    if ! package_installed age; then
        apt-get -o DPkg::Lock::Timeout=600 update -q
        DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q age
        changed=1
    fi
    for unit in "${BACKUP_UNITS[@]}"; do
        if write_if_changed "/etc/systemd/system/$unit" 0644 <"$SCRIPT_DIR/systemd/$unit"; then
            reload=1
        fi
    done
    if ((reload)); then
        systemctl daemon-reload
        changed=1
    fi
    if ! systemctl is-enabled -q new2u-backup.timer || ! systemctl is-active -q new2u-backup.timer; then
        systemctl enable --now new2u-backup.timer
        changed=1
    fi
    step_result "backup job (age $(age --version), new2u-backup.timer)" "$changed"
}

main() {
    parse_args "$@"
    preflight
    cleanup_provider_firewall
    run_harden
    delete_provider_rejects
    install_docker
    setup_deploy_user
    setup_backup
    echo
    printf 'changed=%d\n' "$CHANGED"
}

main "$@"
