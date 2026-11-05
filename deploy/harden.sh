#!/usr/bin/env bash
# Basic hardening of the new2u VPS (WHO-54). Runs as root on a fresh
# Ubuntu 24.04 server; deploy/bootstrap.sh calls it (WHO-175). By hand, from a
# copy of the deploy/ directory:
#
#   sudo deploy/harden.sh           # apply, then verify
#   sudo deploy/harden.sh --check   # verify only, change nothing (deploy drift check)
#   sudo deploy/harden.sh --preflight  # only the guards below, change nothing
#
# What it does:
#   1. ufw: deny incoming, allow 22/tcp, 80/tcp, 443/tcp.
#   2. sshd: keys only (no passwords), no root login. Refuses to touch sshd
#      unless a sudo user has a key, and runs `sshd -t` before reload.
#   3. unattended-upgrades for security updates.
#   4. fail2ban for sshd, distribution defaults.
#   5. journald retention: a line is off the server within 14 days
#      (deploy/journald/new2u.conf).
#   6. rpcbind (portmapper, port 111) stopped, disabled and masked where the
#      image ships it (Oracle's Ubuntu 24.04 does, WHO-179). Nothing on this
#      server uses NFS or RPC. Where it is not installed the step does nothing.
#
# Safe to run twice: every step writes a file only when its content differs
# and every command it runs is idempotent. Each step prints "changed" or "ok",
# and apply ends with "harden.sh changed=N"; a second run prints changed=0.
# Plain Ubuntu tools only, no provider-specific agent.
#
# Docker bypasses ufw for published ports (its iptables rules come before
# ufw's), so the firewall does not protect a container port published on
# 0.0.0.0. The compose files publish Postgres and the API on 127.0.0.1 only;
# the verify step fails if any running container publishes anything other
# than 80/443 on a non-loopback address.

set -euo pipefail

# ${BASH_SOURCE[0]} is unset when the script comes on stdin (bash -s, docs/self-hosting.md).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
readonly SCRIPT_DIR
readonly SSHD_DROPIN=/etc/ssh/sshd_config.d/00-new2u.conf
readonly FAIL2BAN_JAIL=/etc/fail2ban/jail.d/new2u.local
readonly AUTO_UPGRADES=/etc/apt/apt.conf.d/20auto-upgrades
readonly JOURNALD_DROPIN=/etc/systemd/journald.conf.d/new2u.conf
readonly ALLOWED_PORTS=(22 80 443)
# Socket first: while rpcbind.socket listens it starts the service again.
readonly RPCBIND_UNITS=(rpcbind.socket rpcbind.service)
readonly PACKAGES=(ufw unattended-upgrades fail2ban python3-systemd)

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Steps that changed something on this run.
CHANGED=0
# step_result NAME 0|1: print the step's result (1 = changed) and count it.
step_result() {
    if (($2)); then
        printf '[changed] harden: %s\n' "$1"
        CHANGED=$((CHANGED + 1))
    else
        printf '[ok]      harden: %s\n' "$1"
    fi
}

# Enable and start each unit that is not enabled and running yet.
# Returns 0 when it enabled or started one, 1 when all were already up.
enable_units() {
    local unit rc=1
    for unit in "$@"; do
        if ! systemctl is-enabled -q "$unit" || ! systemctl is-active -q "$unit"; then
            systemctl enable --now "$unit"
            rc=0
        fi
    done
    return $rc
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

preflight() {
    [[ $EUID -eq 0 ]] || die "run as root (sudo $0)"
    # shellcheck source=/dev/null
    . /etc/os-release
    [[ "${ID:-}" == ubuntu ]] || die "this script is for Ubuntu, found ${ID:-unknown}"
    [[ "${VERSION_ID:-}" == 24.04 ]] || log "WARNING: written for Ubuntu 24.04, found ${VERSION_ID:-unknown}"
}

# Lockout guard: list the accounts that can log in with a key. Root cannot log
# in at all after this script (PermitRootLogin no, WHO-177 finding 8), so a key
# for root does not count: fails unless a sudo user can log in with a key,
# because otherwise turning passwords and root off would lock everyone out.
check_ssh_keys() {
    local file user count rights found=0
    for file in /home/*/.ssh/authorized_keys; do
        [[ -s "$file" ]] || continue
        # ssh-keygen -l parses authorized_keys (options included) and prints one
        # fingerprint per valid key; comments and junk lines do not count.
        count="$(ssh-keygen -l -f "$file" 2>/dev/null | wc -l)" || count=0
        ((count > 0)) || continue
        user="$(stat -c %U "$file")"
        [[ "$user" != root ]] || continue
        # sudo's own answer, so a sudoers.d entry counts as much as group sudo.
        # Into a variable first: `sudo -l | grep -q` can fail on SIGPIPE.
        rights="$(sudo -l -U "$user" 2>/dev/null)" || rights=
        if grep -q 'may run the following commands' <<<"$rights"; then
            log "SSH key login possible for sudo user '$user' ($count key(s) in $file)"
            found=1
        fi
    done
    ((found)) || die "no sudo user has an SSH public key in /home/*/.ssh/authorized_keys.
Root login is turned off, so a sudo user with a key is needed (keys for root do not count).
Add one first, then run this again. Nothing was changed."
    # Run through sudo (deploy/bootstrap.sh is): the calling user must keep key
    # login too, not only some other account.
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then
        file="$(getent passwd "$SUDO_USER" | cut -d: -f6)/.ssh/authorized_keys"
        count="$(ssh-keygen -l -f "$file" 2>/dev/null | wc -l)" || count=0
        ((count > 0)) || die "you ran this through sudo as '$SUDO_USER', who has no SSH key in $file.
Turning passwords off would lock '$SUDO_USER' out. Add the key first. Nothing was changed."
    fi
}

check_ssh_port() {
    local ports
    install -d -m 0755 /run/sshd
    ports="$(sshd -T | awk '$1 == "port" {print $2}' | sort -u | paste -sd ' ' -)" \
        || die "sshd -T failed on the current config. Nothing was changed."
    [[ "$ports" == 22 ]] || die "sshd listens on port(s) '${ports}', this script only opens 22/tcp. Nothing was changed."
}

package_installed() {
    [[ "$(dpkg-query -W -f '${db:Status-Status}' "$1" 2>/dev/null)" == installed ]]
}

install_packages() {
    local pkg missing=()
    for pkg in "${PACKAGES[@]}"; do
        package_installed "$pkg" || missing+=("$pkg")
    done
    if ((${#missing[@]})); then
        log "installing ${missing[*]}"
        export DEBIAN_FRONTEND=noninteractive
        apt-get -o DPkg::Lock::Timeout=600 update -q
        # python3-systemd lets fail2ban read sshd's log from the journal.
        apt-get -o DPkg::Lock::Timeout=600 install -y -q "${missing[@]}"
    fi
    step_result packages "$((${#missing[@]} > 0))"
}

setup_firewall() {
    local port before
    log "firewall: deny incoming, allow ${ALLOWED_PORTS[*]}/tcp"
    before="$(ufw status verbose)"
    ufw default deny incoming
    ufw default allow outgoing
    for port in "${ALLOWED_PORTS[@]}"; do
        # ufw skips a rule that already exists.
        ufw allow "${port}/tcp"
    done
    # SSH is allowed above, so enabling does not drop this session. Enabling an
    # active ufw reloads it, so skip that.
    if ! ufw status | grep -qx 'Status: active'; then
        ufw --force enable
    fi
    if [[ "$(ufw status verbose)" == "$before" ]]; then
        step_result firewall 0
    else
        step_result firewall 1
    fi
}

setup_sshd() {
    local changed=0
    log "sshd: keys only"
    # Files in sshd_config.d are read in name order and the first value of a
    # setting wins, so 00- beats cloud-init's 50-cloud-init.conf and anything
    # below the Include line in sshd_config.
    if write_if_changed "$SSHD_DROPIN" 0644 <<'EOF'; then
# Installed by deploy/harden.sh (WHO-54). Keys only, no passwords.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
        # Ubuntu 24.04 starts sshd from ssh.socket; without a running sshd this
        # directory is missing and `sshd -t` fails on it.
        install -d -m 0755 /run/sshd
        if ! sshd -t; then
            rm -f "$SSHD_DROPIN"
            die "sshd -t rejected the config; removed $SSHD_DROPIN, sshd not reloaded"
        fi
        # Reload keeps open sessions. When sshd is not running (socket
        # activation) the next connection reads the new config anyway.
        systemctl try-reload-or-restart ssh.service
        log "sshd reloaded"
        changed=1
    fi
    step_result sshd "$changed"
}

setup_updates() {
    local changed=0
    log "unattended-upgrades: security updates daily"
    # Same content `dpkg-reconfigure unattended-upgrades` writes. Ubuntu's
    # 50unattended-upgrades allows only the -security origin (and ESM), so
    # nothing else, including Docker from download.docker.com, upgrades by
    # itself. Automatic reboot stays off.
    if write_if_changed "$AUTO_UPGRADES" 0644 <<'EOF'; then
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
        changed=1
    fi
    if enable_units unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer; then
        changed=1
    fi
    step_result unattended-upgrades "$changed"
}

setup_fail2ban() {
    local changed=0
    log "fail2ban: sshd jail"
    if write_if_changed "$FAIL2BAN_JAIL" 0644 <<'EOF'; then
# Installed by deploy/harden.sh (WHO-54). Distribution defaults otherwise.
[sshd]
enabled = true
backend = systemd
EOF
        systemctl restart fail2ban.service
        changed=1
    fi
    if enable_units fail2ban.service; then
        changed=1
    fi
    step_result fail2ban "$changed"
}

setup_journald() {
    local changed=0
    log "journald: lines removed within 14 days"
    if write_if_changed "$JOURNALD_DROPIN" 0644 <"$SCRIPT_DIR/journald/new2u.conf"; then
        systemctl restart systemd-journald.service
        changed=1
    fi
    step_result journald "$changed"
}

# True when systemd knows a unit file for $1 (masked ones included).
unit_file_exists() {
    local files
    files="$(systemctl list-unit-files --no-legend "$1" 2>/dev/null)" || return 1
    [[ -n "$files" ]]
}

setup_rpcbind() {
    local unit changed=0 found=0
    for unit in "${RPCBIND_UNITS[@]}"; do
        unit_file_exists "$unit" || continue
        found=1
        # is-enabled prints "masked" for a masked unit (and exits non-zero).
        if [[ "$(systemctl is-enabled "$unit" 2>/dev/null)" != masked ]] \
            || systemctl is-active -q "$unit"; then
            log "rpcbind: stop, disable and mask $unit"
            systemctl disable --now "$unit"
            systemctl mask "$unit"
            changed=1
        fi
    done
    ((found)) || log "rpcbind: not installed, nothing to do"
    step_result rpcbind "$changed"
}

# ---- verify ---------------------------------------------------------------

FAILURES=0
pass() { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
check() {
    local what=$1
    shift
    if "$@" >/dev/null 2>&1; then pass "$what"; else fail "$what"; fi
}

# Ports allowed by ufw beyond 22, 80, 443 (any protocol, v4 or v6).
ufw_extra_rules() {
    # "22/tcp (v6)  ALLOW  Anywhere (v6)": the port is the first field either way.
    ufw status | awk '/ALLOW/ {print $1}' | sort -u \
        | grep -vxE '(22|80|443)/tcp' || true
}

# Container port mappings on a non-loopback address, other than 80/443.
# `docker ps` prints e.g. "0.0.0.0:80->80/tcp, [::]:80->80/tcp, 127.0.0.1:8000->8000/tcp".
docker_public_ports() {
    local containers
    command -v docker >/dev/null || return 0
    containers="$(docker ps --format '{{.Names}} {{.Ports}}')" || {
        echo "docker ps failed, cannot check published ports"
        return 0
    }
    while read -r name ports; do
        tr ',' '\n' <<<"$ports" | { grep -- '->' || true; } | sed 's/^ *//; s/->.*//' | while read -r hostside; do
            local addr=${hostside%:*} port=${hostside##*:}
            case "$addr" in 127.* | '[::1]') continue ;; esac
            case "$port" in 80 | 443) continue ;; esac
            printf '%s publishes %s\n' "$name" "$hostside"
        done
    done <<<"$containers"
}

# Not `sshd -T | grep -q`: grep -q exits on the first match, sshd gets SIGPIPE
# and pipefail turns the match into a failure.
sshd_setting() {
    local settings
    settings="$(sshd -T 2>/dev/null)" || return 1
    grep -qx "$1" <<<"$settings"
}

# TCP listeners and unconnected UDP sockets (-l with -u) on port 111.
nothing_on_port_111() {
    local sockets
    sockets="$(ss -Hlntu 'sport = :111')" || return 1
    [[ -z "$sockets" ]]
}

verify() {
    local extra public
    echo
    log "summary"
    echo "--- ufw status verbose"
    ufw status verbose || true
    echo "--- sshd effective settings (sshd -T)"
    install -d -m 0755 /run/sshd
    sshd -T | grep -E '^(port|passwordauthentication|kbdinteractiveauthentication|permitrootlogin|pubkeyauthentication) ' || true
    echo "--- unattended-upgrades"
    apt-config dump APT::Periodic | grep -E 'Update-Package-Lists|Unattended-Upgrade' || true
    systemctl is-enabled unattended-upgrades.service || true
    echo

    log "checks"
    check "ufw active" bash -c 'ufw status | grep -qx "Status: active"'
    check "ufw denies incoming by default" bash -c 'ufw status verbose | grep -q "deny (incoming)"'
    extra="$(ufw_extra_rules)"
    if [[ -z "$extra" ]]; then pass "ufw allows only 22/tcp, 80/tcp, 443/tcp"; else fail "ufw allows more: $(tr '\n' ' ' <<<"$extra")"; fi
    check "ssh: password login off" sshd_setting 'passwordauthentication no'
    check "ssh: keyboard-interactive login off" sshd_setting 'kbdinteractiveauthentication no'
    check "ssh: no root login" sshd_setting 'permitrootlogin no'
    check "unattended-upgrades enabled" bash -c 'apt-config dump APT::Periodic::Unattended-Upgrade | grep -q "\"1\";" && systemctl is-enabled -q unattended-upgrades.service'
    check "apt-daily-upgrade.timer active" systemctl is-active -q apt-daily-upgrade.timer
    check "fail2ban sshd jail running" fail2ban-client status sshd
    check "journald removes lines within 14 days" bash -c 'systemd-analyze cat-config systemd/journald.conf | grep -qx "MaxRetentionSec=13day" && systemd-analyze cat-config systemd/journald.conf | grep -qx "MaxFileSec=1day"'
    check "nothing listens on port 111 (rpcbind)" nothing_on_port_111
    public="$(docker_public_ports)"
    if [[ -z "$public" ]]; then pass "no container publishes beyond 80/443 on a public address"; else fail "$(tr '\n' ';' <<<"$public")"; fi
    if [[ -f /var/run/reboot-required ]]; then
        log "NOTE: a reboot is pending to finish installing updates (kernel or libc)"
    fi

    echo
    if ((FAILURES)); then
        die "$FAILURES check(s) failed"
    fi
    log "all checks passed"
}

main() {
    local mode=${1:-apply}
    case "$mode" in
        apply | --check | --preflight) ;;
        *) die "usage: $0 [--check | --preflight]" ;;
    esac
    preflight
    if [[ "$mode" == --preflight ]]; then
        check_ssh_keys
        check_ssh_port
        return 0
    fi
    if [[ "$mode" == apply ]]; then
        # Both guards run before anything changes on the host.
        check_ssh_keys
        check_ssh_port
        install_packages
        setup_firewall
        setup_sshd
        setup_updates
        setup_fail2ban
        setup_journald
        setup_rpcbind
    fi
    verify
    if [[ "$mode" == apply ]]; then
        # deploy/bootstrap.sh reads this line; keep its format.
        printf 'harden.sh changed=%d\n' "$CHANGED"
    fi
}

main "$@"
