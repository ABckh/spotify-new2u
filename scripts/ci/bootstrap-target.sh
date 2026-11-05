#!/usr/bin/env bash
# CI only (WHO-175): turn this GitHub runner into a throwaway "fresh Oracle
# server" for deploy/bootstrap.sh, and check the result. The stack itself is
# deployed and checked by scripts/ci/deploy-target.sh (WHO-176). Never run this
# on a real server or your own machine: it installs packages, rewrites the
# firewall and creates users.
#
#   scripts/ci/bootstrap-target.sh setup    # sudo user, keys, Oracle's firewall trap and rpcbind
#   scripts/ci/bootstrap-target.sh verify   # after two bootstrap runs
#   scripts/ci/bootstrap-target.sh logs     # stack logs, on failure
#
# Every key here is generated on the runner and dies with it.

set -euo pipefail

[[ "${GITHUB_ACTIONS:-}" == true ]] || { echo "CI only, see the header" >&2; exit 1; }

readonly SUDO_USER_NAME=bootstrap
readonly KEYS="$RUNNER_TEMP/keys"
readonly APP_DIR=/srv/new2u
readonly COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.vps.yml)

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Oracle's Ubuntu 24.04 image ships iptables-persistent with these rules
# (abridged): only 22 gets in, everything else is REJECTed.
oracle_rules() {
    cat <<'EOF'
*filter
:INPUT ACCEPT [0:0]
:FORWARD ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
-A INPUT -m state --state RELATED,ESTABLISHED -j ACCEPT
-A INPUT -p icmp -j ACCEPT
-A INPUT -i lo -j ACCEPT
-A INPUT -p tcp -m state --state NEW -m tcp --dport 22 -j ACCEPT
-A INPUT -j REJECT --reject-with icmp-host-prohibited
-A FORWARD -j REJECT --reject-with icmp-host-prohibited
COMMIT
EOF
}

setup() {
    log "openssh-server, and iptables-persistent as on Oracle (it removes ufw)"
    sudo apt-get update -q
    # Answer no to "save current rules?": the Oracle rules file is written below.
    echo 'iptables-persistent iptables-persistent/autosave_v4 boolean false' | sudo debconf-set-selections
    echo 'iptables-persistent iptables-persistent/autosave_v6 boolean false' | sudo debconf-set-selections
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q openssh-server iptables-persistent
    oracle_rules | sudo tee /etc/iptables/rules.v4 >/dev/null
    # --noflush: add to the runner's live rules instead of replacing them, so
    # the runner's own Docker keeps its chains. A real Oracle server has no
    # Docker yet.
    oracle_rules | sudo iptables-restore --noflush
    sudo iptables -S INPUT | grep -q -- '-j REJECT' || die "trap not armed"

    log "rpcbind on port 111, as on Oracle's image (WHO-179)"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q rpcbind
    sudo systemctl enable --now rpcbind.socket rpcbind.service
    [[ -n "$(ss -Hlntu 'sport = :111')" ]] || die "rpcbind does not listen on 111"

    log "sudo user $SUDO_USER_NAME with a generated key"
    mkdir -p "$KEYS"
    ssh-keygen -q -t ed25519 -N '' -C bootstrap-ci -f "$KEYS/bootstrap"
    ssh-keygen -q -t ed25519 -N '' -C deploy-ci -f "$KEYS/deploy"
    sudo useradd --create-home --shell /bin/bash "$SUDO_USER_NAME"
    echo "$SUDO_USER_NAME ALL=(ALL) NOPASSWD:ALL" | sudo tee "/etc/sudoers.d/$SUDO_USER_NAME" >/dev/null
    sudo chmod 0440 "/etc/sudoers.d/$SUDO_USER_NAME"
    sudo install -d -m 0700 -o "$SUDO_USER_NAME" -g "$SUDO_USER_NAME" "/home/$SUDO_USER_NAME/.ssh"
    sudo install -m 0600 -o "$SUDO_USER_NAME" -g "$SUDO_USER_NAME" \
        "$KEYS/bootstrap.pub" "/home/$SUDO_USER_NAME/.ssh/authorized_keys"
    sudo systemctl start ssh.socket ssh.service || sudo systemctl start ssh.service

    # The host key comes from the disk, not from ssh-keyscan: the pin is
    # known, not learned on first use.
    echo "127.0.0.1 $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)" >"$KEYS/known_hosts"
    {
        echo "DEPLOY_HOST=127.0.0.1"
        echo "BOOTSTRAP_USER=$SUDO_USER_NAME"
        echo "DEPLOY_SSH_KEY_FILE=$KEYS/bootstrap"
        echo "DEPLOY_KNOWN_HOSTS=$(cat "$KEYS/known_hosts")"
        echo "DEPLOY_PUBKEY=$(cat "$KEYS/deploy.pub")"
    } >>"$GITHUB_ENV"
}

deploy_ssh() {
    ssh -i "$KEYS/deploy" -o IdentitiesOnly=yes -o BatchMode=yes \
        -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$KEYS/known_hosts" \
        deploy@127.0.0.1 "$@"
}

verify() {
    local out
    log "first run changed something, second run nothing"
    grep -E '^changed=[1-9][0-9]*$' "$RUNNER_TEMP/bootstrap-1.log"
    grep -x 'changed=0' "$RUNNER_TEMP/bootstrap-2.log"
    if grep -q '^\[changed\]' "$RUNNER_TEMP/bootstrap-2.log"; then
        die "second run reported a changed step"
    fi

    log "rpcbind: masked by the first run, nothing on 111"
    grep -x '\[changed\] harden: rpcbind' "$RUNNER_TEMP/bootstrap-1.log"
    for unit in rpcbind.socket rpcbind.service; do
        [[ "$(systemctl is-enabled "$unit" 2>/dev/null)" == masked ]] || die "$unit not masked"
        ! systemctl is-active -q "$unit" || die "$unit still active"
    done
    [[ -z "$(ss -Hlntu 'sport = :111')" ]] || die "something still listens on 111"

    log "the provider's REJECT rules went only after ufw was on (WHO-177 finding 5)"
    local ufw_line reject_line
    ufw_line="$(grep -nx '\[changed\] harden: firewall' "$RUNNER_TEMP/bootstrap-1.log" | head -n 1 | cut -d: -f1)"
    reject_line="$(grep -n 'deleting REJECT rules' "$RUNNER_TEMP/bootstrap-1.log" | head -n 1 | cut -d: -f1)"
    [[ -n "$ufw_line" && -n "$reject_line" ]] || die "bootstrap-1.log lacks the firewall or the REJECT step"
    ((ufw_line < reject_line)) || die "REJECT rules deleted (line $reject_line) before ufw was on (line $ufw_line)"
    grep -x '\[changed\] provider REJECT rules' "$RUNNER_TEMP/bootstrap-1.log"

    log "root cannot log in over SSH (WHO-177 finding 8)"
    sudo sshd -T | grep -x 'permitrootlogin no'

    log "harden.sh --check, sent on stdin as in docs/self-hosting.md"
    # The file is ours and readable; only bash needs root.
    # shellcheck disable=SC2024
    sudo bash -s -- --check <deploy/harden.sh

    log "Oracle's trap is gone"
    ! dpkg-query -W -f '${db:Status-Status}' iptables-persistent 2>/dev/null | grep -qx installed \
        || die "iptables-persistent still installed"
    [[ ! -e /etc/iptables/rules.v4 ]] || die "/etc/iptables/rules.v4 still there"
    if sudo iptables -S INPUT | grep -q -- '-j REJECT' || sudo iptables -S FORWARD | grep -q -- '-j REJECT'; then
        die "a REJECT rule is still live"
    fi

    log "deploy user: key login works and reaches docker; password login is refused"
    deploy_ssh 'id -nG | tr " " "\n" | grep -qx docker && docker info --format "{{.ServerVersion}}"'
    if out="$(ssh -o BatchMode=yes -o PubkeyAuthentication=no -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile="$KEYS/known_hosts" deploy@127.0.0.1 true 2>&1)"; then
        die "password-less login without a key succeeded"
    fi
    grep -q 'Permission denied (publickey)' <<<"$out" || die "unexpected answer: $out"
    [[ "$(sudo getent shadow deploy | cut -d: -f2)" == '*' ]] || die "deploy has a password"

    log "backup job: age installed, timer enabled, service skipped until a deploy writes backup.env"
    age --version
    systemctl is-enabled -q new2u-backup.timer || die "new2u-backup.timer not enabled"
    systemctl is-active -q new2u-backup.timer || die "new2u-backup.timer not active"
    systemctl list-timers --no-pager new2u-backup.timer
    sudo systemctl start new2u-backup.service
    [[ "$(systemctl show -P ConditionResult new2u-backup.service)" == no ]] \
        || die "new2u-backup.service ran without backup.env"

    log "layout"
    [[ "$(sudo stat -c '%U:%G %a' "$APP_DIR")" == "deploy:deploy 750" ]] || die "$APP_DIR owner/mode"
    [[ "$(sudo stat -c '%U:%G %a' "$APP_DIR/.env")" == "deploy:deploy 600" ]] || die "$APP_DIR/.env owner/mode"
    docker compose version
}

logs() {
    deploy_ssh "cd $APP_DIR && ${COMPOSE[*]} ps -a" || true
    sudo journalctl -q --no-pager -n 200 CONTAINER_NAME=new2u-api-1 || true
    sudo journalctl -q --no-pager -n 100 CONTAINER_NAME=new2u-caddy-1 || true
}

case "${1:-}" in
    setup | verify | logs) "$1" ;;
    *) die "usage: $0 setup|verify|logs" ;;
esac
