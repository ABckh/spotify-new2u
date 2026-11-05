#!/usr/bin/env bash
# The check functions at the end are called through `check`.
# shellcheck disable=SC2329
# Make a fresh Ubuntu 24.04 server the app's PostgreSQL host, so the database
# does not run on the app server. scripts/bootstrap-db-remote.sh copies deploy/
# to the database server and runs this there as root; see docs/self-hosting.md "External
# database".
#
#   APP_CIDR=10.0.0.0/24 DB_LISTEN_IP=10.0.1.240 bash db-host.sh <<<"$POSTGRES_PASSWORD"
#
# Inputs:
#   APP_CIDR        the app server's subnet: the only source allowed to reach SSH
#                   and PostgreSQL. The server should have no public address.
#   DB_LISTEN_IP    this server's private address; PostgreSQL listens there and
#                   on localhost only
#   POSTGRES_USER, POSTGRES_DB  optional, default new2u (deploy/env.template)
#   stdin, line 1   the app role's password (POSTGRES_PASSWORD). Never on a
#                   command line: the SQL that sets it goes to psql over stdin.
#
# What it does, each step printing [changed] or [ok], then changed=N:
#   1. Provider firewall cleanup (Oracle's iptables-persistent), as in
#      deploy/bootstrap.sh; the live REJECT rules go once ufw is on.
#   2. Packages: ufw, PostgreSQL 16, unattended-upgrades.
#   3. ufw: deny incoming; allow 22 and 5432/tcp from APP_CIDR only.
#   4. sshd: key login only, no root login.
#   5. 1 GB swap (the free micro shapes have 1 GB of RAM).
#   6. journald: lines leave the server within 14 days (deploy/journald).
#   7. PostgreSQL: listens on localhost and DB_LISTEN_IP, TLS on (Ubuntu's
#      self-signed certificate), TLS 1.2+, SCRAM passwords, logs to journald
#      with the same terse settings as docker-compose.yml (PRD P12). pg_hba
#      allows only TLS connections from APP_CIDR, for the app role to the app
#      database; local admin is peer auth as the postgres OS user.
#   8. The app role (LOGIN, no superuser, no CREATEDB/CREATEROLE) and the app
#      database it owns; PUBLIC cannot connect to it.
# Safe to run again: a second run prints changed=0.

set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
CHANGED=0
step() { # step NAME CHANGED(0|1)
    if [[ "$2" == 1 ]]; then printf '[changed] %s\n' "$1"; CHANGED=$((CHANGED + 1)); else printf '[ok]      %s\n' "$1"; fi
}
# write_if_differs FILE MODE: content from stdin; prints 1 when the file changed.
# has LINE_REGEX CMD...: CMD's output has a line matching LINE_REGEX. Not
# `CMD | grep -q`: grep -q exits on the first match, CMD gets SIGPIPE and
# pipefail turns the match into a failure (as in deploy/harden.sh).
has() {
    local re=$1 out
    shift
    out="$("$@" 2>/dev/null)" || return 1
    grep -qE "$re" <<<"$out"
}
write_if_differs() {
    local file=$1 mode=$2 tmp
    tmp="$(mktemp)"
    cat >"$tmp"
    if [[ -f "$file" ]] && cmp -s "$tmp" "$file"; then
        rm -f "$tmp"; echo 0
    else
        install -m "$mode" "$tmp" "$file"; rm -f "$tmp"; echo 1
    fi
}

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
: "${APP_CIDR:?set APP_CIDR to the app server subnet, e.g. 10.0.0.0/24}"
: "${DB_LISTEN_IP:?set DB_LISTEN_IP to this server private address}"
POSTGRES_USER="${POSTGRES_USER:-new2u}"
POSTGRES_DB="${POSTGRES_DB:-new2u}"
[[ "$APP_CIDR" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]] || die "APP_CIDR is not an IPv4 CIDR"
[[ "$APP_CIDR" != 0.0.0.0/0 ]] || die "APP_CIDR must not be the whole internet"
[[ "$DB_LISTEN_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "DB_LISTEN_IP is not an IPv4 address"
has "inet ${DB_LISTEN_IP//./\\.}/" ip -4 -o addr show || die "DB_LISTEN_IP $DB_LISTEN_IP is not an address of this server"
[[ "$POSTGRES_USER" =~ ^[a-z_][a-z0-9_]*$ && "$POSTGRES_DB" =~ ^[a-z_][a-z0-9_]*$ ]] \
    || die "POSTGRES_USER and POSTGRES_DB must be plain lower-case identifiers"
IFS= read -r PASSWORD || true
# The password goes into SQL as a literal, so only characters that need no quoting.
[[ "$PASSWORD" =~ ^[A-Za-z0-9_-]{16,128}$ ]] || die "the password on stdin must be 16-128 characters of [A-Za-z0-9_-]"
# Turning passwords off would lock out a sudo user without a key.
SUDO_HOME="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)"
[[ -s "$SUDO_HOME/.ssh/authorized_keys" ]] || die "${SUDO_USER:-root} has no SSH key; refusing to turn passwords off"

export DEBIAN_FRONTEND=noninteractive

# 1. Provider firewall cleanup -------------------------------------------------
c=0
if dpkg -s iptables-persistent >/dev/null 2>&1 || dpkg -s netfilter-persistent >/dev/null 2>&1; then
    apt-get purge -y -qq iptables-persistent netfilter-persistent >/dev/null
    c=1
fi
if ls /etc/iptables/rules.v* >/dev/null 2>&1; then rm -f /etc/iptables/rules.v*; c=1; fi
step "provider firewall cleanup" $c

# 2. Packages -------------------------------------------------------------------
c=0
missing=()
for p in ufw postgresql-16 unattended-upgrades; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if ((${#missing[@]})); then
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "${missing[@]}" >/dev/null
    c=1
fi
step "packages (ufw, postgresql-16, unattended-upgrades)" $c

# 3. ufw -------------------------------------------------------------------------
c=0
# `ufw status` lists rules from line 5 on as "22/tcp  ALLOW  10.0.0.0/24".
ufw_rules() { ufw status | awk 'NR > 4 && NF {print $1, $2, $3}' | sort; }
want_rules="$(printf '22/tcp ALLOW %s\n5432/tcp ALLOW %s\n' "$APP_CIDR" "$APP_CIDR" | sort)"
if ! has '^Status: active' ufw status || [[ "$(ufw_rules)" != "$want_rules" ]]; then
    ufw --force reset >/dev/null
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    ufw allow proto tcp from "$APP_CIDR" to any port 22 >/dev/null
    ufw allow proto tcp from "$APP_CIDR" to any port 5432 >/dev/null
    ufw --force enable >/dev/null
    c=1
fi
step "ufw: only $APP_CIDR may reach 22 and 5432" $c
# Oracle's live REJECT rules, now that ufw guards the host.
c=0
while iptables -C INPUT -j REJECT --reject-with icmp-host-prohibited 2>/dev/null; do
    iptables -D INPUT -j REJECT --reject-with icmp-host-prohibited; c=1
done
while iptables -C FORWARD -j REJECT --reject-with icmp-host-prohibited 2>/dev/null; do
    iptables -D FORWARD -j REJECT --reject-with icmp-host-prohibited; c=1
done
step "provider REJECT rules" $c

# 4. sshd ---------------------------------------------------------------------------
c=$(write_if_differs /etc/ssh/sshd_config.d/00-new2u.conf 0644 <<'EOF'
# new2u database host (deploy/db-host.sh): keys only, no root login.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
)
if [[ "$c" == 1 ]]; then install -d -m 0755 /run/sshd && sshd -t && systemctl reload ssh; fi
step "sshd: key login only, no root login" "$c"

# 5. swap -----------------------------------------------------------------------------
c=0
if [[ ! -f /swapfile ]]; then
    fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null
    c=1
fi
has '^/swapfile$' swapon --show=NAME --noheadings || { swapon /swapfile; c=1; }
grep -q '^/swapfile ' /etc/fstab || { echo '/swapfile none swap sw 0 0' >>/etc/fstab; c=1; }
c2=$(printf 'vm.swappiness=10\n' | write_if_differs /etc/sysctl.d/90-new2u-swap.conf 0644)
[[ "$c2" == 1 ]] && { sysctl -q -p /etc/sysctl.d/90-new2u-swap.conf; c=1; }
step "1 GB swap" $c

# 6. journald -------------------------------------------------------------------------
mkdir -p /etc/systemd/journald.conf.d
c=$(write_if_differs /etc/systemd/journald.conf.d/new2u.conf 0644 <"$SCRIPT_DIR/journald/new2u.conf")
[[ "$c" == 1 ]] && systemctl restart systemd-journald
step "journald: lines leave the server within 14 days" "$c"

# 7. PostgreSQL configuration ------------------------------------------------------
PGDIR=/etc/postgresql/16/main
[[ -d "$PGDIR" ]] || die "$PGDIR not found: is postgresql-16 installed with its default cluster?"
c=$(write_if_differs "$PGDIR/conf.d/new2u.conf" 0644 <<EOF
# new2u database host (deploy/db-host.sh). Changes here need a restart.
listen_addresses = 'localhost,$DB_LISTEN_IP'
ssl = on
ssl_min_protocol_version = 'TLSv1.2'
password_encryption = 'scram-sha-256'
# Small server (1 GB RAM).
max_connections = 40
shared_buffers = '128MB'
effective_cache_size = '512MB'
work_mem = '4MB'
# Logs go to journald (no rsyslog here), so they follow its 14-day retention.
logging_collector = off
log_destination = 'syslog'
syslog_ident = 'postgres'
# PRD P12, as in docker-compose.yml: no row data or statements in the log.
log_error_verbosity = terse
log_min_error_statement = panic
# Who connected: user, database, source address, TLS and auth method, never a
# query value. Failed authentication is logged as FATAL anyway.
log_connections = on
EOF
)
c2=$(write_if_differs "$PGDIR/pg_hba.conf" 0640 <<EOF
# new2u database host (deploy/db-host.sh). Anything not listed is refused.
# TYPE    DATABASE          USER              ADDRESS       METHOD
local     all               postgres                        peer
hostssl   $POSTGRES_DB      $POSTGRES_USER    $APP_CIDR     scram-sha-256
EOF
)
chown postgres:postgres "$PGDIR/pg_hba.conf"
if [[ "$c" == 1 ]]; then
    systemctl restart postgresql@16-main
elif [[ "$c2" == 1 ]]; then
    systemctl reload postgresql@16-main
fi
systemctl is-active --quiet postgresql@16-main || die "postgresql@16-main is not running"
[[ "$c" == 1 || "$c2" == 1 ]] && c=1
step "postgresql: TLS only, from $APP_CIDR only" "$c"

# 8. App role and database -----------------------------------------------------------
psql_admin() { runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -d postgres "$@"; }
c=0
if [[ "$(psql_admin -tAc "SELECT 1 FROM pg_roles WHERE rolname = '$POSTGRES_USER'")" != 1 ]]; then c=1; fi
# The password reaches psql over stdin only.
psql_admin <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$POSTGRES_USER') THEN
    CREATE ROLE $POSTGRES_USER LOGIN;
  END IF;
END
\$\$;
ALTER ROLE $POSTGRES_USER LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '$PASSWORD';
SQL
if [[ "$(psql_admin -tAc "SELECT 1 FROM pg_database WHERE datname = '$POSTGRES_DB'")" != 1 ]]; then
    psql_admin -c "CREATE DATABASE $POSTGRES_DB OWNER $POSTGRES_USER"
    c=1
fi
psql_admin -c "REVOKE ALL ON DATABASE $POSTGRES_DB FROM PUBLIC" -c "GRANT CONNECT, TEMPORARY ON DATABASE $POSTGRES_DB TO $POSTGRES_USER"
step "role $POSTGRES_USER and database $POSTGRES_DB" $c

# Checks ---------------------------------------------------------------------------------
fail=0
check() { # check DESCRIPTION FUNCTION
    if "$2" >/dev/null 2>&1; then printf '  ok    %s\n' "$1"; else printf '  FAIL  %s\n' "$1"; fail=1; fi
}
ufw_denies_incoming() { has '^Status: active' ufw status verbose && has 'deny \(incoming\)' ufw status verbose; }
ufw_only_app_subnet() { [[ "$(ufw_rules)" == "$want_rules" ]]; }
# sshd -T needs /run/sshd, which socket-activated ssh creates only on first use.
ssh_no_passwords() { install -d -m 0755 /run/sshd && has '^passwordauthentication no$' sshd -T; }
ssh_no_root() { install -d -m 0755 /run/sshd && has '^permitrootlogin no$' sshd -T; }
pg_listens_private_only() {
    local public
    public="$(ss -Hltn 'sport = :5432' | awk '{print $4}' | grep -vE '^(127\.0\.0\.1|\[::1\]):5432$' | sort -u)"
    [[ "$public" == "$DB_LISTEN_IP:5432" ]]
}
pg_tls_on() { [[ "$(psql_admin -tAc 'SHOW ssl')" == on ]]; }
app_role_not_superuser() { [[ "$(psql_admin -tAc "SELECT rolsuper FROM pg_roles WHERE rolname = '$POSTGRES_USER'")" == f ]]; }
updates_enabled() { systemctl is-enabled --quiet apt-daily-upgrade.timer; }
swap_on() { has '^/swapfile$' swapon --show=NAME --noheadings; }
check "ufw active, incoming denied by default" ufw_denies_incoming
check "ufw allows only $APP_CIDR (22, 5432)" ufw_only_app_subnet
check "ssh: password login off" ssh_no_passwords
check "ssh: no root login" ssh_no_root
check "postgres listens on $DB_LISTEN_IP and localhost only" pg_listens_private_only
check "postgres TLS on" pg_tls_on
check "$POSTGRES_USER is not a superuser" app_role_not_superuser
check "unattended-upgrades enabled" updates_enabled
check "swap on" swap_on
echo
echo "changed=$CHANGED"
exit "$fail"
