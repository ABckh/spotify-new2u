# Self-hosting

Each instance is for you and a few people you know. A Spotify app in
development mode works only for the Spotify accounts its owner lists in the
Spotify dashboard (at most 5), and the server itself lets in only the accounts
in `SPOTIFY_ALLOWED_ACCOUNT_IDS`. So you run your own server, register your own
Spotify app, and build the iPhone app yourself.

## What you need

- A small Ubuntu 24.04 server with a public IP address, and SSH access as a
  sudo user.
- A domain name pointing at the server, or just its IP address (see
  `API_HOSTNAME` in [Deploy credentials](#deploy-credentials)).
- A Spotify account to own the Spotify app. Spotify may require Premium for
  the owner of an app in development mode; check its current developer terms.
- A Mac with Xcode and an iPhone, for the app. A free Apple ID works; the app
  then has to be installed again every 7 days.
- Optional: an S3-compatible bucket for nightly encrypted backups (see
  [Backups](#backups)).

## The steps

1. **Create the Spotify app** ([below](#create-the-spotify-app)).
2. **Get an image.** CI builds the server image on every push to `main` and
   pushes it to `ghcr.io/<owner>/new2u` (see [The pipeline](#the-pipeline)).
   Fork this repository and keep Actions on: your fork's CI publishes the
   image to your own account. If that package is private, the deploy needs a
   token that can read it (`REGISTRY_USER` and `REGISTRY_TOKEN`); for a public
   one, leave both unset.
3. **Prepare the server** ([Server setup](#server-setup)).
4. **Deploy** ([Deploy runbook](#deploy-runbook)): from your own machine with
   `scripts/deploy-remote.sh`, or with the GitHub Actions deploy workflow.
5. **Build the iPhone app** for your server ([ios.md](ios.md)).

## Create the Spotify app

1. In the [Spotify developer dashboard](https://developer.spotify.com/dashboard),
   create an app. Pick **Web API** as the API it uses.
2. Add the redirect URI `https://<API_HOSTNAME>/auth/spotify/callback`. It must
   equal `SPOTIFY_REDIRECT_URI` character for character.
3. Copy the **Client ID** into `SPOTIFY_CLIENT_ID`. The client secret is
   optional (`SPOTIFY_CLIENT_SECRET`): sign-in uses PKCE and works without it.
4. Under **User Management**, add every person who will use your instance,
   with the email address of their Spotify account. Spotify refuses accounts
   that are not listed there.
5. Put the same people's Spotify account IDs in `SPOTIFY_ALLOWED_ACCOUNT_IDS`
   ([how to find one](configuration.md)).

Sign-in asks for `user-read-recently-played` (required) and the read-only
`user-top-read`, `user-library-read` and `user-follow-read`. Under Spotify's
terms you are the developer of your app: keep its client secret to yourself
and follow its rules on Spotify content.

## Server setup

One command makes a fresh Ubuntu 24.04 server ready to run the stack. It runs from your machine (or CI) over SSH, as a non-root user with
passwordless sudo, and does not depend on a provider: it was written for
Oracle Cloud first and works the same on Hetzner or DigitalOcean.
`deploy/bootstrap.sh` does, in this order:

1. **Provider firewall cleanup.** Oracle's Ubuntu images ship
   `iptables-persistent` with rules that let in only 22 and REJECT the rest
   (which also blocks 80/443 and conflicts with ufw). The packages and
   `/etc/iptables/rules.v*` are removed here; the live REJECT rules stay until
   ufw is on (step 2), so the host is never without a firewall, even if
   step 2 fails half-way. SSH stays open throughout: only REJECTs are
   removed, and ufw allows 22 before it is enabled. Where none of this exists
   the step does nothing.
2. **`deploy/harden.sh`**, then the live REJECT rules are deleted:
   - ufw: incoming traffic is refused except 22, 80 and 443/tcp
   - SSH: key login only, and **no root login** (`PermitRootLogin no`). It
     refuses to start unless a sudo user (yours, e.g. `ubuntu`) has a key, so
     turning root off cannot lock you out
   - unattended security updates
   - fail2ban for SSH
   - journald retention: a line is off the server within 14 days (see [Logs](#logs))
   - rpcbind (port 111) stopped, disabled and masked where the image ships it
     (Oracle does); nothing here uses it
3. **Docker Engine and the Compose plugin** from Docker's apt repository. The
   repository key is taken only if its fingerprint is
   `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`. Skipped when Docker
   is installed with Compose 2.24 or newer (`docker-compose.vps.yml` needs it).
4. **The `deploy` user and `/srv/new2u`.** `deploy` has no password, logs in
   with the key(s) you pass in only, and is in the `docker` group.
   `/srv/new2u` (mode 0750) and its `.env` (mode 0600, empty until the first
   deploy) belong to `deploy`.
5. **The nightly backup job**: the `age` package and the systemd
   units `new2u-backup.timer` and `new2u-backup.service`, timer enabled.
   The service runs `deploy/backup.sh` as `deploy`, and only once a deploy has
   written `/srv/new2u/backup.env`; until then each night is skipped. See
   [Backups](#backups).

Every step prints `[changed]` or `[ok]`, and the run ends with `changed=N`.
Run it as often as you like: a second run prints `changed=0`. CI proves both on
a throwaway Ubuntu 24.04 machine on every push (the `bootstrap` job).

**The `docker` group is root-equivalent.** Anyone who can talk to the Docker
daemon can start a container that mounts `/` and do anything on the host. So
the `deploy` key is as powerful as a root key. That is accepted for this
single-user server, where `deploy` exists to run `docker compose` and nothing
else; guard its private key like a root key.

### Run it

1. Create the server with your SSH public key added (every provider asks for
   one). On Oracle the sudo user is `ubuntu`; on Hetzner and DigitalOcean it is
   `root`, so create a sudo user first or use `BOOTSTRAP_USER=root`.

   The script stops, and changes nothing, if the user you connect as has no key
   in its `authorized_keys`: turning passwords off would lock you out.

2. Pin the server's host key. Read it from the provider's console (Oracle:
   instance > Console connection; or `cat /etc/ssh/ssh_host_ed25519_key.pub` in
   a web console), not from the network, and make a known_hosts line:

   ```sh
   echo "SERVER_IP ssh-ed25519 AAAA...from-the-console" > known_hosts.new2u
   ```

   `bootstrap-remote.sh` refuses to connect when the host key does not match.
   It never accepts an unknown key.

3. Make the key the `deploy` user (and later the deploy job) logs in with:

   ```sh
   ssh-keygen -t ed25519 -N '' -C new2u-deploy -f ~/.ssh/new2u-deploy
   ```

4. Bring the server up to date, and reboot if it asks for one:

   ```sh
   ssh ubuntu@SERVER_IP 'sudo apt-get update && sudo apt-get full-upgrade -y'
   ssh ubuntu@SERVER_IP '[ -f /var/run/reboot-required ] && sudo reboot'
   ```

5. From the repo root, run the bootstrap. **Keep a second SSH session to the
   server open** until step 6 passes:

   ```sh
   DEPLOY_HOST=SERVER_IP BOOTSTRAP_USER=ubuntu \
   DEPLOY_SSH_KEY_FILE=~/.ssh/id_ed25519 \
   DEPLOY_KNOWN_HOSTS="$(cat known_hosts.new2u)" \
   DEPLOY_PUBKEY="$(cat ~/.ssh/new2u-deploy.pub)" \
   scripts/bootstrap-remote.sh
   ```

   Inputs come from the environment only. In CI, put the private key itself
   in `DEPLOY_SSH_KEY` instead of a path in `DEPLOY_SSH_KEY_FILE`; it is
   written to a 0600 temporary file and removed afterwards. `DEPLOY_SSH_PORT`
   defaults to 22. The script copies `deploy/` to a temporary directory on the
   server, runs `sudo deploy/bootstrap.sh` there and removes the directory.
   It ends with `ufw status`, the effective sshd settings, a list of
   `ok`/`FAIL` checks and `changed=N`. Any `FAIL` makes it exit non-zero.

6. From your own machine, check that key login still works, that the server
   refuses passwords, and that `deploy` can log in:

   ```sh
   ssh ubuntu@SERVER_IP true && echo "key login ok"
   ssh -o PubkeyAuthentication=no ubuntu@SERVER_IP
   # expected: ubuntu@SERVER_IP: Permission denied (publickey).
   ssh -i ~/.ssh/new2u-deploy deploy@SERVER_IP docker compose version
   ```

   If key login fails, fix it from the session you kept open:
   `sudo rm /etc/ssh/sshd_config.d/00-new2u.conf && sudo systemctl reload ssh`
   turns passwords back on. If every session is lost, use the provider's web
   console.

7. Run it once more; it must print `changed=0`. After the stack is up (see
   [Deploy runbook](#deploy-runbook)), run the checks again; this changes
   nothing (the bootstrap removes its copy of `deploy/`, so the script is
   sent over stdin):

   ```sh
   ssh ubuntu@SERVER_IP 'sudo bash -s -- --check' < deploy/harden.sh
   ```

   From your own machine, only 22, 80 and 443 should answer:

   ```sh
   nc -zv -w 3 SERVER_IP 5432   # expected: fails
   nc -zv -w 3 SERVER_IP 8000   # expected: fails
   ```

On Oracle, also open 80 and 443 in the subnet's security list (or the
instance's network security group) in the Oracle console; that is Oracle's
firewall in front of the server, and the script cannot change it.

### The VPS stack

`docker-compose.vps.yml` is laid over `docker-compose.yml` on the server:

```sh
cd /srv/new2u
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d
```

- `api` and `poller` run the image in `API_IMAGE` (GHCR, by the digest CI's
  push printed); nothing is built on the server. Locally, `docker compose up` still
  builds from the `Dockerfile`.
- `caddy` is the reverse proxy (`deploy/caddy/Caddyfile`): it serves
  `API_HOSTNAME` on 443 with a Let's Encrypt certificate, redirects 80 to 443,
  sends `Strict-Transport-Security: max-age=31536000`, and passes requests to
  `api:8000`. Certificates live in the `caddy-data` volume and survive
  restarts. Caddy writes no access log and no error log: its error log would
  hold full request URIs, query strings included. `CADDY_TLS=internal` uses
  Caddy's own CA instead, for test targets without a public name (the CI job
  does this).
- **`API_HOSTNAME` may be the server's IP address**, e.g.
  `203.0.113.10`, or an IPv6 address in brackets. Let's Encrypt issues IP
  certificates only under its `shortlived` profile: valid about 6 days
  (160 hours), so Caddy renews every 2 to 4 days (Let's Encrypt's renewal
  info, or a third of the lifetime left), on its own. Renewal needs 80 or 443
  open to the internet the whole time (no DNS challenge for IPs): if the
  ports are closed for a few days, the certificate expires and the app stops
  connecting. `scripts/deploy-remote.sh` sees the IP and sets
  `CADDY_TLS=public_ip`, which asks for that profile; a name keeps Caddy's
  default (90-day certificates).
- **Caddy is the only service that publishes ports.** The override resets the
  `ports` of `postgres` and `api` (`!reset []`). Reach Postgres with
  `docker compose ... exec postgres psql -U new2u new2u`.
- The compose network is fixed at `10.201.0.0/24` and Caddy at `10.201.0.2`,
  so `FORWARDED_ALLOW_IPS` is exactly Caddy's address and the sign-in rate
  limits see the real client IP. The override sets it; do not put it in `.env`.
- Every service logs to journald (see [Logs](#logs)).

`deploy/env.template` lists every key of `/srv/new2u/.env` and where its
value comes from: a secret, a non-secret deploy input, a fixed value (for
example `APP_ENV=production`), or left to the app's default. It holds no
secret value.

Docker skips ufw for the ports it publishes. Its iptables rules run before
ufw's, so `ports: "5432:5432"` would put Postgres on the internet even with the
firewall on. That is why only Caddy publishes on the VPS, and locally Postgres
and the API publish on `127.0.0.1` only. `harden.sh --check` fails if a running
container publishes anything but 80/443 on a public address.

Good to know:

- fail2ban bans an address for 10 minutes after 5 failed SSH logins. To unban
  one, run `fail2ban-client set sshd unbanip ADDRESS`.
- Security updates install every day, but the server never reboots on its own.
  If `/var/run/reboot-required` exists (the check prints a note), reboot when
  convenient. The poller catches up afterwards, because Spotify keeps the last
  50 plays.
- Docker from Docker's repository is not upgraded by unattended-upgrades
  (it only takes Ubuntu's security pocket). Upgrade it by hand.

### External database

By default PostgreSQL runs as the `postgres` container on the app server. It
can instead run on a server of its own, so the data is not on the machine that
faces the internet. Then that server has **no public address**: only the app
server's subnet can reach it, on 22 and 5432, and PostgreSQL accepts only TLS
connections from there.

**The database server.** A plain Ubuntu 24.04 server in a private subnet (on
Oracle, an Always Free `VM.Standard.E2.1.Micro` is enough). It needs outbound
internet for packages and updates (a NAT gateway), and the provider's firewall
must allow 22 and 5432/tcp from the app subnet only. Then, from the repo root,
through the app server as jump host. Type the app role's password at the
`read` prompt: it is not echoed and stays out of your shell history.

```sh
read -rs POSTGRES_PASSWORD && export POSTGRES_PASSWORD
DB_HOST=DB_PRIVATE_IP BOOTSTRAP_USER=ubuntu DEPLOY_SSH_KEY_FILE=~/.ssh/id_ed25519 \
DB_KNOWN_HOSTS="DB_PRIVATE_IP ssh-ed25519 AAAA..." \
JUMP_HOST=ubuntu@APP_IP JUMP_KNOWN_HOSTS="$(cat known_hosts.new2u)" \
APP_CIDR=APP_SUBNET_CIDR \
scripts/bootstrap-db-remote.sh
```

Pin the host key from the provider's console (Oracle: the instance's console
history shows the ED25519 fingerprint cloud-init printed). `deploy/db-host.sh`
sets up ufw (22 and 5432 from `APP_CIDR` only), key-only SSH without root,
1 GB swap, journald retention, and PostgreSQL 16: listening on the private
address, TLS 1.2+ (Ubuntu's self-signed certificate), SCRAM passwords, the same
terse logging as the container plus one line per connection (`log_connections`), and a `pg_hba.conf` that allows only
`hostssl new2u new2u APP_CIDR`. The app role is not a superuser and owns the
app database. A second run prints `changed=0`.

**The app.** Set `DATABASE_HOST` (a GitHub variable, or in `.env.prod` for a
deploy from your machine) to the database server's private address and deploy.
`scripts/deploy-remote.sh` then sets `DATABASE_SSLMODE=require` and
`DATABASE_CHANNEL_BINDING=require` and leaves the `localdb` profile off, so
`api` and `poller` connect to that server over TLS and no `postgres` container
starts. Unset `DATABASE_HOST`, and the next deploy runs the container again.
The certificate is not verified against a CA (`require`, not `verify-full`),
but channel binding accepts only SCRAM-SHA-256-PLUS, which is bound to the
certificate the client saw: a host in between can neither relay the login nor
ask for a cleartext password.

**Moving an existing database there:** stop `api` and `poller`, dump the old
database (see [A dump by hand](#a-dump-by-hand)), restore it on the database
server as the app role, compare the row counts, deploy with `DATABASE_HOST`,
then stop the old container: `docker compose --profile localdb stop postgres`.
Its volume (`new2u_postgres-data`) still holds the old copy until you remove
it: `docker volume rm new2u_postgres-data`.

On the app server, `docker compose exec postgres` no longer works with an
external database. The nightly backup (`deploy/backup.sh`) dumps over TLS from a
one-off `postgres` container instead.

## Deploy runbook

### The pipeline

Every push to `main` runs CI (`.github/workflows/ci.yml`). When every check
passes, including the throwaway-server job below, **`image`** builds the image
once and pushes it to `ghcr.io/<owner>/new2u:<commit sha>` (owner in
lowercase). The digest the push printed is in the run's summary
(`Image: ghcr.io/<owner>/new2u@sha256:...`) and kept as the run's
`image-ref` artifact for 90 days. `api` and `poller` both run it; nothing is
built on the server.

**Nothing deploys on its own.** A production deploy is only the owner's
**Actions → Deploy → Run workflow**, on `main`, with the commit SHA
(`.github/workflows/deploy.yml`). It looks up the digest from that commit's
successful CI run and deploys the image **by digest**, never by its tag. It
runs only when the repository variable `DEPLOY_ENABLED` is `true`, its first
step fails on any branch but `main`, and it takes its secrets from the
`production` environment, which only `main` may use (see
[Deploy credentials](#deploy-credentials)).

The deploy job runs `scripts/deploy-remote.sh`, which SSHes to the server as
`DEPLOY_USER` (host key pinned by `DEPLOY_KNOWN_HOSTS`) and runs the steps of
`deploy/release.sh` in `/srv/new2u`:

1. Upload `docker-compose.yml`, `docker-compose.vps.yml`,
   `deploy/caddy/Caddyfile` and `deploy/release.sh`. If the Caddyfile changed,
   Caddy is recreated.
2. Write `.env` (mode 0600) from the environment's secrets and variables,
   following `deploy/env.template`. It travels over SSH stdin: never on a
   command line, never printed. A missing secret stops the deploy before the
   server is touched.
3. `docker login ghcr.io` on the server with the job's own `GITHUB_TOKEN`
   (read-only, expires with the job), over stdin.
4. Pull the image, run `alembic upgrade head` in a one-off `api` container,
   `up -d`, then `docker logout`.
5. Check `https://API_HOSTNAME/healthz` through Caddy, retried for up to 3
   minutes, then check that `api` and `poller` run the new image and do not
   restart for 10 seconds, and that no container publishes a port other than
   80/443 on a non-loopback address (`deploy/release.sh verify`; the same
   rule as `harden.sh --check`, without root).
6. Record the release: `/srv/new2u/current` and `previous` each hold an
   image reference by digest (`ghcr.io/<owner>/new2u@sha256:...`).
   `current` only changes after step 5 passed, so it is always the last
   release that worked.

**If step 4 or 5 fails,** the job puts the image in `current` back
(`API_IMAGE` in `.env`, `up -d`, no migration), checks `/healthz` again and
fails. Exit status 1 means "rolled back, the earlier release is healthy"; 2
means no healthy release is running (for example the very first deploy
failed): look at the server.

**Re-running a deploy** of the same commit changes nothing: same files, same
`.env`, same containers (compose sees no difference), no migration to run,
same release files.

The CI `bootstrap` job proves all of this on every push, on a throwaway Ubuntu
24.04 runner that plays the server: it bootstraps it, then runs the same
`scripts/deploy-remote.sh` with test inputs (a local registry with a password
in place of GHCR, generated secrets, Caddy with `tls internal`). It deploys a
first release, deploys it again and checks that nothing changed, deploys a
second release, then a deliberately broken one, and checks that the deploy
fails, rolls back to the second release, and `/healthz` is OK again.

The deploy needs Ubuntu packages only on the runner side (`ssh`, `tar`,
`curl`). **To deploy from your own machine** (no GitHub secrets at all), export
the same inputs and run `scripts/deploy-remote.sh` from the repo root, with
`DEPLOY_SSH_KEY_FILE` pointing at the deploy key and `API_IMAGE` set to the
digest from the CI run's summary. The server then also needs a GHCR login
with a token that can read the package: pass a personal access token with
only `read:packages` as `REGISTRY_TOKEN` and your GitHub user as
`REGISTRY_USER`; the script logs in over stdin and out again.

### Deploy credentials

The only provider-specific input is this list. Values live only in GitHub,
never in the repo, a task, a document or a log. You create every one of
them. "Environment" means the GitHub
`production` environment (Settings → Environments → production);
`tests/test_deploy_files.py` checks that `deploy.yml` reads every key of
`deploy/env.template` from the right place.

**The gate is the `production` environment**. Everything
the deploy job reads, every secret and every variable below except
`DEPLOY_ENABLED`, lives **only on that environment**: no repository-level
secrets at all. Its **deployment-branch policy allows `main` only**, so a run
from any other branch gets none of them, whatever that branch's copy of the
workflow says. Branch protection, a required reviewer, or `DEPLOY_ENABLED`
alone do not protect the secrets: a branch can edit the workflow that checks
them. On a private repository, environments with a branch policy need GitHub
Pro (or higher) on the owner's account; required reviewers need a public
repository and are not part of this design. Without Pro, use the local path
above (`scripts/deploy-remote.sh` from your machine) and keep no deploy
secrets in GitHub.

Set up once, by the owner:

- the **`production` environment**: Settings → Environments → New
  environment `production`, then Deployment branches and tags → **Selected
  branches** → add `main`. Put every secret and variable of the table below
  there (except `DEPLOY_ENABLED`). Check:
  `gh api repos/OWNER/new2u/environments/production` shows the branch
  policy, and `gh api repos/OWNER/new2u/actions/secrets` lists nothing;
- the **deploy SSH key pair**:
  `ssh-keygen -t ed25519 -N '' -C new2u-deploy -f ~/.ssh/new2u-deploy`.
  The public half goes to the server through the bootstrap (`DEPLOY_PUBKEY`,
  see [Run it](#run-it)); the private half is the `DEPLOY_SSH_KEY` secret.

| Name | Secret or variable | Where | Who creates it | Value |
| --- | --- | --- | --- | --- |
| `DEPLOY_ENABLED` | variable | **repository** (Settings → Secrets and variables → Actions → Variables) | owner | `true` to let deploys run. Unset: no deploy. |
| `DEPLOY_HOST` | variable | environment | owner | server IP or name |
| `DEPLOY_USER` | variable | environment | owner | `deploy` (made by the bootstrap; not `ubuntu`) |
| `DEPLOY_KNOWN_HOSTS` | variable | environment | owner | the server's known_hosts lines, pinned (see below) |
| `DEPLOY_SSH_KEY` | **secret** | environment | owner | private half of the deploy key (`~/.ssh/new2u-deploy`, whole file) |
| `API_HOSTNAME` | variable | environment | owner | public name of the API (its DNS A record points at the server), **or the server's IP address**, e.g. `203.0.113.10` (IPv6 in brackets). With an IP, Caddy gets 6-day Let's Encrypt certificates and renews them every few days, which needs 80/443 open all the time (see [Server setup](#server-setup)). No default. |
| `SPOTIFY_REDIRECT_URI` | variable | environment | owner | `https://<API_HOSTNAME>/auth/spotify/callback`, e.g. `https://203.0.113.10/auth/spotify/callback`. Register **exactly the same URI** in the Spotify developer dashboard (app → Settings → Redirect URIs); Spotify compares them character for character. |
| `APP_CALLBACK_URL` | variable | environment | owner | the app's callback, e.g. `new2u://signed-in` |
| `POSTGRES_PASSWORD` | **secret** | environment | owner | `openssl rand -hex 24` (hex: it goes into a URL) |
| `TOKEN_ENCRYPTION_KEY` | **secret** | environment | owner | see `.env.example` |
| `SESSION_TOKEN_SECRET` | **secret** | environment | owner | see `.env.example` |
| `SPOTIFY_CLIENT_ID` | **secret** | environment | owner | from the Spotify developer dashboard |
| `SPOTIFY_CLIENT_SECRET` | **secret** | environment | owner | optional: empty for a public (PKCE-only) client |
| `SPOTIFY_ALLOWED_ACCOUNT_IDS` | **secret** | environment | owner | Spotify account ids that may sign in, comma-separated |
| `BACKUP_S3_ENDPOINT` | variable | environment | owner | bucket's S3 endpoint URL, e.g. `https://s3.eu-central-003.backblazeb2.com` or `https://<account id>.r2.cloudflarestorage.com` |
| `BACKUP_S3_REGION` | variable | environment | owner | B2: the region in the endpoint (`eu-central-003`); R2: `auto`; others: their region |
| `BACKUP_S3_BUCKET` | variable | environment | owner | bucket name, e.g. `new2u-backups` |
| `BACKUP_S3_ACCESS_KEY_ID` | **secret** | environment | owner | bucket key id (see [Backups](#backups) for its rights) |
| `BACKUP_S3_SECRET_ACCESS_KEY` | **secret** | environment | owner | bucket key secret |
| `BACKUP_AGE_RECIPIENT` | variable | environment | owner | the backup `age` **public** key, `age1...`. The private key never goes to GitHub or the server. |
| `BACKUP_PRUNE` | variable | environment | owner | optional: `on` (unset means `on`) or `off`. `off`: the job deletes nothing and the bucket's lifecycle rule alone removes old dumps; use it with a key that cannot delete (see [Backups](#backups)) |
| `BACKUP_HEARTBEAT_URL` | **secret** | environment | owner | optional: a dead-man's-switch ping URL, e.g. a healthchecks.io check's `https://hc-ping.com/<uuid>`. Pinged after every backup that passed |

The six `BACKUP_S3_*`/`BACKUP_AGE_RECIPIENT` names are all or nothing: none
set turns the nightly backup off (the deploy passes and says so), some set
stops the deploy before it touches the server. `BACKUP_PRUNE` and
`BACKUP_HEARTBEAT_URL` are optional on top of those six, and only with them.
`BACKUP_S3_ENDPOINT` and `BACKUP_HEARTBEAT_URL` must be `https://` (plain
`http://` is accepted for `127.0.0.1`/`localhost` only, which CI uses). The key id is a secret, not a variable, although it is not
a password on its own: GitHub masks a secret in every log, and at B2 the key id
names the account. Nothing needs to read it back.

`DEPLOY_KNOWN_HOSTS` holds the server's public host keys (not secret), one
line per key, taken from the server, never from `ssh-keyscan`:

```
203.0.113.10 ssh-ed25519 AAAA...from-the-console
```

Check it before you save it: `ssh-keygen -lf <(echo 'THE ssh-ed25519 LINE')`
prints the fingerprint, which must match the one the provider's console shows.

Optional environment variables (owner, only if needed), the `default` rows of
`deploy/env.template` (left out, the app's default applies): `LOG_LEVEL`,
`API_DOCS_ENABLED`,
`SESSION_TOKEN_TTL_SECONDS`, `REFRESH_TOKEN_TTL_SECONDS`, `OAUTH_LOGIN_TTL_SECONDS`,
`LOGIN_RATE_LIMIT_PER_MINUTE`, `CLAIM_RATE_LIMIT_PER_MINUTE`,
`MAX_PENDING_LOGINS`, `READ_RATE_LIMIT_PER_MINUTE`,
`EXPORT_RATE_LIMIT_PER_MINUTE`, `EXPORT_IDLE_TIMEOUT_SECONDS`,
`POLLER_INTERVAL_SECONDS`, `POLLER_NEW_CONNECTION_CHECK_SECONDS`,
`MANUAL_POLL_COOLDOWN_SECONDS`, `REAUTH_WARNING_DAYS`,
`COLLECTION_RECORDS_RETENTION_DAYS`.

Set by the pipeline, not by you: `API_IMAGE` (the commit's image, by digest),
the GHCR login (`GITHUB_TOKEN`), and `CADDY_TLS` / `CADDY_DEFAULT_SNI`
(`scripts/deploy-remote.sh` derives both from `API_HOSTNAME`). Fixed in the template: `APP_ENV=production`,
`POSTGRES_USER`, `POSTGRES_DB`, `API_PORT`, `COMPOSE_FILE`.

Values are written single-quoted, so they may contain `$`, but not a quote or
a line break.

### Migrations must work with the previous release

Migrations run forward only, before the new code starts, and a rollback only
changes the image. So the release before must keep working on the migrated
database: **expand first, remove later.** Add a column or table (nullable or
with a default) in one release; stop using the old one in the code; remove it
in a later release. Never rename or drop in the same release that stops using
it.

### On the server

Log in as `deploy` with the deploy key, and work in `/srv/new2u`:

```sh
ssh -i ~/.ssh/new2u-deploy deploy@SERVER_IP
cd /srv/new2u
deploy/release.sh status        # current, previous, and the image in .env
docker compose ps
```

`.env` sets `COMPOSE_FILE`, so plain `docker compose` there uses both
`docker-compose.yml` and `docker-compose.vps.yml`. Do not edit `.env` by hand:
the next deploy writes it again from GitHub. Change the value in GitHub and
re-run the deploy instead.

### Roll back to a given commit

Through the pipeline (preferred): Actions → **Deploy** → Run workflow, on
`main`, with the full commit SHA whose image to run. Turn "Skip migrations"
on: the older code does not know the newer migrations, and the database stays
as it is (see above). The run deploys that commit's image by the digest its
CI run kept, and records it as `current`. CI keeps the digest for 90 days;
for an older commit, put the image by digest in the `image` input
(`ghcr.io/<owner>/new2u@sha256:...`, from that run's summary, or
`docker buildx imagetools inspect ghcr.io/<owner>/new2u:<sha>`).

By hand on the server, when GitHub is not available (the image must still be
on the server; `docker image ls --digests ghcr.io/*/new2u` lists them, and
`deploy/release.sh status` shows `current` and `previous`):

```sh
cd /srv/new2u
deploy/release.sh set-image ghcr.io/OWNER/new2u@sha256:DIGEST
deploy/release.sh up --no-pull --no-migrate
curl -fsS https://API_HOSTNAME/healthz && deploy/release.sh verify && deploy/release.sh record
```

### Move to a new provider

No code changes: only credentials, DNS and data.

1. **New server.** Create a plain Ubuntu 24.04 server with your SSH key. Open
   22, 80 and 443/tcp in the provider's firewall, if it has one.
2. **Bootstrap it** (see [Run it](#run-it)): pin its host key, then
   `scripts/bootstrap-remote.sh` with the new `DEPLOY_HOST` and the existing
   deploy public key (or a new one, see below). Run it twice: `changed=0`.
3. **Stop the old server's writes** so no play is stored there after the dump:
   `docker compose stop api poller` on the old server. From here until step 7
   no plays are collected; Spotify keeps the last 50, so finish within a few
   hours.
4. **Take a dump** of the old database: `sudo systemctl start
   new2u-backup.service`, then download and decrypt it (see
   [Get a dump back](#get-a-dump-back)). Without backups, a dump by hand
   ([A dump by hand](#a-dump-by-hand)). The bucket stays where it is: the new
   server uses the same `BACKUP_*` settings, so nothing changes there.
5. **Change the credentials** in the `production` environment: `DEPLOY_HOST`,
   `DEPLOY_KNOWN_HOSTS`, and `DEPLOY_SSH_KEY` if the key changed.
6. **DNS:** point the A record of `API_HOSTNAME` at the new server's IP. Caddy
   gets its certificate once the name resolves to it. If `API_HOSTNAME` is
   the old server's IP, set it to the new IP instead, change
   `SPOTIFY_REDIRECT_URI` to match, and register that URI in the Spotify
   dashboard; the app's API address changes too.
7. **Deploy:** Actions → Deploy with the current SHA of `main`. The new server
   gets the stack and an empty database.
8. **Restore the dump** (see [Restore](#restore)), then check
   `https://API_HOSTNAME/healthz` and the app.
9. Delete the old server (and its disks and snapshots) once the new one has
   polled.

### Rotate the deploy SSH key

1. Make a new key: `ssh-keygen -t ed25519 -N '' -C new2u-deploy -f ~/.ssh/new2u-deploy-new`.
2. Re-run the bootstrap with **both** public keys in `DEPLOY_PUBKEY` (one per
   line). The bootstrap manages the `deploy` user's `authorized_keys` as a whole.
3. Put the new private key in the `DEPLOY_SSH_KEY` secret and run a deploy
   (Actions → Deploy with the current SHA): it must pass.
4. Re-run the bootstrap with only the new public key in `DEPLOY_PUBKEY`. The
   old key is removed from the server. Delete the old private key everywhere.

If the old key may have leaked, skip step 2's overlap: bootstrap with the new
key only right away, then update the secret.

### Backups

Every night `new2u-backup.timer` (03:15 UTC, up to 15 minutes later; a
night missed while the server was off runs at the next boot) starts
`new2u-backup.service`, which runs `deploy/backup.sh run` as `deploy`:

1. `pg_dump -Fc` of the app database, from the `postgres` container, piped
   straight into `age -r BACKUP_AGE_RECIPIENT`. Only the encrypted file
   touches the server's disk. The server has the **public** key only, so it
   cannot decrypt any dump, not even its own; the private key stays with the
   owner.
2. Upload to the bucket as `new2u/new2u-<UTC time>.dump.age`, with
   rclone in a container pinned by digest. Works with any S3-compatible
   provider; no provider code. The journal gets
   `uploaded: <name> sha256=<hex> size=<n>` of the encrypted file, which
   [Get a dump back](#get-a-dump-back) checks before decrypting (the copy in
   the heartbeat, step 4, first).
3. With `BACKUP_PRUNE` `on` (the default): delete every
   `new2u/new2u-<UTC time>.dump.age` whose time in the name is more
   than 7 days ago. Only after step 2 passed, so the newest dump is always
   one that just worked. Other files in the bucket are not touched. With
   `off`: nothing is deleted, the journal says `retention: left to the bucket
   lifecycle rule`.
4. With `BACKUP_HEARTBEAT_URL` set: one POST to it, after everything above
   passed; the body is the `uploaded:` line. A failed ping fails the run.

Logs go to the journal: dump name, size, hash, what was deleted. Never a
key, a secret, the heartbeat URL or anything from the database. Any failed
step stops the run with a non-zero exit and an `ERROR:` line.

**Is it working?** On the server:

```sh
systemctl status new2u-backup.service      # last run: "status=0/SUCCESS" or "failed"
journalctl -u new2u-backup.service -n 20   # last runs' lines
systemctl list-timers new2u-backup.timer   # last and next run
sudo -u deploy /srv/new2u/deploy/backup.sh check   # exit 0 when the bucket has a dump < 26 h old
```

`systemctl --failed` lists it after a failed night, but only someone who
logs in sees that. With `BACKUP_HEARTBEAT_URL` the alert comes to you: make a
check at a dead-man's-switch service (healthchecks.io's free plan works;
period 1 day, grace 2 hours), put its ping URL in the secret, and the service
emails you when a night's ping does not arrive, for any reason (job failed,
server down, timer gone). A failure must be fixed within 7 days: the lifecycle rule below keeps deleting dumps older than that,
whether or not new ones arrive. A run by hand: `sudo systemctl start
new2u-backup.service` (waits until it is done).

CI proves all of this on every push, on the throwaway server of the
`bootstrap` job, with MinIO standing in for the bucket and a key that cannot
read: deploy with the backup settings, write a test row, run the service,
download the dump with the bucket owner's key, decrypt it, restore it into an
empty database and find the row, after checking the file's sha256 against
the journal's `uploaded:` line; then a second run deletes the dumps older
than 7 days and keeps the rest; a run with the bucket down fails, shows as
failed and sends no heartbeat (each run that passed sends exactly one, to a
local listener, and the URL is not in the journal); with delete taken off the
key and `BACKUP_PRUNE=off`, a run passes and a 10-day-old dump stays; a
deploy without the settings turns backups off.

#### Set it up (owner, once)

1. **The `age` key pair**, on your own machine (`brew install age` or `apt
   install age`):

   ```sh
   age-keygen -o new2u-backup.agekey
   # prints "Public key: age1..." -> the BACKUP_AGE_RECIPIENT variable
   ```

   `new2u-backup.agekey` holds the private key: it is the only way to read a
   backup. Keep it off the server and out of GitHub, in your password manager
   plus one offline copy. Lose it and every backup is useless; leak it and
   whoever also gets the bucket can read every user's history.

2. **A bucket** at a provider of your choice, private, with a lifecycle rule
   that deletes dumps after 7 days. The job deletes them too; the rule covers
   the nights the job does not run, and is the only deletion where the job's
   key cannot delete.
   - **Backblaze B2:** bucket "Private", default encryption on, Object Lock
     on (step 3). Lifecycle rules → custom: file name prefix `new2u/`,
     "days from uploading to hiding" 7, "days from hiding to deleting" 7. B2
     keeps old versions: a delete through the S3 API, or an upload under a
     name that already exists, only hides a file, and the second number
     removes it for good. The 7 days there are your window to get back a dump
     that was hidden or uploaded over (see
     [Get a dump back](#get-a-dump-back)). A dump is gone about 14 days after
     its upload, or 7 days after it was hidden early.
   - **Cloudflare R2:** bucket → Settings → Object lifecycle rules: prefix
     `new2u/`, "delete uploaded objects after" 7 days.
   - **Oracle Object Storage** (Always Free up to 20 GB): a
     private bucket in the root compartment (the S3 API sees only the
     tenancy's S3 compartment, the root unless changed), and a lifecycle rule
     `DELETE` after 7 days on prefix `new2u/`. The rule needs the
     `objectstorage-<region>` policy statement of step 3.
   - **Anything else S3-compatible:** an expiration rule of 7 days on prefix
     `new2u/`, if the provider has one.

3. **A key for this bucket only**, with the least rights the provider allows:
   - **B2, recommended: Object Lock on the bucket, and a key that can
     neither read nor delete**, scoped to one bucket, plus `BACKUP_PRUNE=off`.
     The lifecycle rule from step 2 does the retention. Make the key with the
     B2 command-line tool (the web UI's "Write Only" choice does not include
     listing, which the job needs):

     ```sh
     b2 key create --bucket new2u-backups new2u-backup listBuckets,listFiles,writeFiles
     ```

     Then turn on **Object Lock** for the bucket (bucket settings → Object
     Lock), default retention 7 days, compliance mode if offered. A locked
     version cannot be deleted before its 7 days are over, not even with the
     account's master key.

     Without Object Lock this key is weaker than it looks: B2's `writeFiles`
     also allows hiding a file, and an upload under a name that already
     exists hides the older version. Someone who takes the key off the server
     cannot download a dump, but can hide or overwrite every one, and the
     lifecycle rule then deletes them for good after "days from hiding to
     deleting" (7 above). With Object Lock the hidden versions stay until
     their retention ends, so [Get a dump back](#get-a-dump-back) still finds
     them.

     After turning on Object Lock, start one backup by hand (`sudo systemctl
     start new2u-backup.service`) and check that it passes. CI's MinIO
     does not have Object Lock, and a locked bucket can refuse an upload
     that carries no checksum.

     Without `BACKUP_PRUNE=off` the key also needs `deleteFiles`
     (`listBuckets,listFiles,writeFiles,deleteFiles`), and a stolen key can
     then delete every version that Object Lock does not hold, at once,
     without the 7 days of hiding.
   - **R2:** an API token with "Object Read & Write", applied to this bucket
     only. R2 has no write-without-read token.
   - **Oracle:** a service user that may use customer secret keys only (an
     identity domain that requires an email takes a placeholder such as
     `new2u-backup@service.invalid`), alone in a group, and one policy:

     ```text
     Allow group new2u-backup to read buckets in tenancy where target.bucket.name = 'new2u-backups'
     Allow group new2u-backup to manage objects in tenancy where all {target.bucket.name = 'new2u-backups', any {request.permission = 'OBJECT_CREATE', request.permission = 'OBJECT_INSPECT'}}
     Allow service objectstorage-REGION to manage object-family in tenancy where target.bucket.name = 'new2u-backups'
     ```

     The key can add and list dumps and nothing else: no read, no overwrite
     (`OBJECT_OVERWRITE` is not granted), no delete, so `BACKUP_PRUNE=off`.
     The user's customer secret key gives `BACKUP_S3_ACCESS_KEY_ID` (its id)
     and `BACKUP_S3_SECRET_ACCESS_KEY`; `BACKUP_S3_ENDPOINT` is
     `https://NAMESPACE.compat.objectstorage.REGION.oraclecloud.com` and
     `BACKUP_S3_REGION` the bucket's region.
   - **Elsewhere:** list on the bucket and write on `new2u/*`, no read; no
     delete with `BACKUP_PRUNE=off`, delete on `new2u/*` without it.

4. **The six `BACKUP_*` settings**, plus `BACKUP_PRUNE=off` for a key
   without delete and `BACKUP_HEARTBEAT_URL` if you want alerts, in the
   `production` environment or the shell you deploy from (see
   [Deploy credentials](#deploy-credentials)), then re-run the bootstrap once
   on an existing server (it adds `age` and the timer; `changed=0` after) and
   deploy. The deploy writes `/srv/new2u/backup.env` (mode 0600, `deploy`).
   Run one backup by hand and check it: `sudo systemctl start
   new2u-backup.service && sudo -u deploy /srv/new2u/deploy/backup.sh check`.

#### Get a dump back

On your machine, with the private key and a key that can read the bucket (B2:
your account's master key or a read key; R2: the dashboard or a read token):

```sh
# list, then download one, e.g. with rclone, B2's or R2's own tool, or the web UI
rclone lsf :s3,provider=Other,endpoint=https://ENDPOINT,region=REGION:BUCKET/new2u/
rclone copyto :s3,provider=Other,endpoint=https://ENDPOINT,region=REGION:BUCKET/new2u/new2u-20261009T031502Z.dump.age .
# (keys from the environment: RCLONE_S3_ACCESS_KEY_ID, RCLONE_S3_SECRET_ACCESS_KEY)

# B2, a hidden or uploaded-over dump: list every version, then download one by its file id
b2 ls --versions b2://BUCKET/new2u/
b2 file download b2id://FILE_ID new2u-20261009T031502Z.dump.age

# before decrypting: the hash must be the one the server logged at upload
shasum -a 256 new2u-20261009T031502Z.dump.age
# with BACKUP_HEARTBEAT_URL: the body of that night's ping at the heartbeat service
# else, on the server: journalctl -u new2u-backup.service | grep 'uploaded: new2u-20261009T031502Z.dump.age '

age -d -i new2u-backup.agekey -o new2u-20261009T031502Z.dump new2u-20261009T031502Z.dump.age
```

A file whose hash is not in an `uploaded:` line was not written by the
backup job: anyone with the bucket's write key can upload a file that
decrypts fine with your key, and `pg_restore` runs what the archive holds.
Do not restore it. The same goes for an older version of a name: check each
version's hash, not just the newest.

Check against the heartbeat body first. It is kept off the server, where the
write key cannot reach it. The journal is on the same server as the write
key, so whoever took the key may also have written to it: trust it only if
the server is not suspected. The journal keeps the lines 14 days, and a B2
dump can live about as long (step 2 of [Set it up](#set-it-up-owner-once)).
For a dump more than 13 days old, the journal may no longer hold its
`uploaded:` line nor the deletes made since it (see [Restore](#restore)):
use the heartbeat body, and escalate to the CTO before restoring it.

The `.dump` file is a plain `pg_dump -Fc` and holds every user's history:
restore it (see [Restore](#restore)), then `shred -u` it. The file name is
the dump's UTC time, which the restore needs.

#### A dump by hand

Before a risky change, or when moving providers: `sudo systemctl start
new2u-backup.service` puts a fresh dump in the bucket. Without backups set
up, on the server as `deploy`:

```sh
cd /srv/new2u
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > ~/new2u-$(date -u +%Y%m%dT%H%M%SZ).dump
```

That file is not encrypted: copy it off the server
(`scp deploy@SERVER_IP:new2u-*.dump .`), keep it encrypted, and remove the
server copy (`shred -u ~/new2u-*.dump`).

With an external database (`DATABASE_HOST` set, see
[External database](#external-database)) there is no `postgres` container to
`exec` into. Dump from the app server as `deploy` through a one-off `postgres`
container over TLS, as `deploy/backup.sh` does, straight into `age`, so only an
encrypted file is written. Replace `DB_PRIVATE_IP` with `DATABASE_HOST` and
`age1...` with `BACKUP_AGE_RECIPIENT`:

```sh
cd /srv/new2u
set -o pipefail   # a pg_dump that fails half-way must fail the command
docker compose -f docker-compose.yml -f docker-compose.vps.yml run --rm --no-deps -T \
  -e PGHOST=DB_PRIVATE_IP -e PGSSLMODE=require -e PGCHANNELBINDING=require \
  --entrypoint sh postgres \
  -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  | age -r age1... -o ~/new2u-$(date -u +%Y%m%dT%H%M%SZ).dump.age
```

The password comes from the container's environment, never a command line.
If the command fails, `age` may still have written a short file: delete it.
Never dump on the database server itself, and never leave a plaintext dump on
either server: copy the `.dump.age` off (`scp`), then `shred -u` the server
copy. Decrypt and restore it as in [Restore](#restore).

### Logs

Every server log is removed from the server after 14 days; no log holds a token, an OAuth `code` or `state`, or
anything the user played (PRD P12, fixed string W6). The restore runbook
replays `account_deleted` lines from the journal, so the journal is kept for
those 14 days on purpose. On the VPS this needs two settings, both by
configuration:

1. Every container logs to journald. Start the stack with the override, which
   sets `logging: driver: journald` on every service:

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d
   docker inspect --format '{{.Name}} {{.HostConfig.LogConfig.Type}}' $(docker compose ps -q)
   ```

   Every line must end in `journald`. A service added later gets the same `logging: *journald` line in `docker-compose.vps.yml`,
   and Caddy's access log stays off.

2. journald removes a line within 14 days. It deletes whole files, so the
   drop-in sets `MaxRetentionSec=13day` and `MaxFileSec=1day`: 13 + 1 = 14.
   Database dumps (see [Backups](#backups)) hold no journal line, and nothing
   else copies the disk, so the 14 days are the whole story. `deploy/harden.sh` installs the drop-in (see
   [Server setup](#server-setup)); by hand it is:

   ```sh
   sudo install -m 0644 deploy/journald/new2u.conf /etc/systemd/journald.conf.d/
   sudo systemctl restart systemd-journald
   systemd-analyze cat-config systemd/journald.conf | grep -E 'MaxRetentionSec|MaxFileSec'
   ```

What is written, by whom:

- **API:** one `app.access` line per request with method, path, status, client
  address and duration. Never the query string. Uvicorn's own access log is off.
- **API and poller:** sign-in and authorization decisions, and one
  `account_deleted user_id=<id>` line per account delete. Tracebacks of database
  errors omit the error text, which can contain row values.
- **Postgres:** errors only, with `log_error_verbosity=terse` and
  `log_min_error_statement=panic` (`docker-compose.yml`), so no row value or SQL
  statement is logged.

### Restore

From a `pg_dump` file: a nightly dump, downloaded and decrypted (see
[Get a dump back](#get-a-dump-back)), or one taken by hand. A restore brings back every
account deleted after the dump was taken. Each account delete writes one line,
`account_deleted user_id=<id>`, to the server's journal; after a restore you
delete each of those ids again. The replay is idempotent: an id that is not in
the restored database prints `not present` and is skipped, and each id that is
deleted writes a fresh `account_deleted` line, so a second restore is covered
too.

**Do step 1 before you restore.** The journal is on the server, not in the
dump. If the server that ran since the dump is gone and step 1 cannot be done,
the deletes made since the dump cannot be replayed: stop and escalate to the
CTO before starting `api` or `poller`. (When moving providers with `api` and
`poller` stopped before the dump, no delete can have happened since: skip
step 1 and step 5.)

1. On the server that ran **since the dump**, export the deleted ids since the
   dump's time (the UTC time in its file name), minus one hour as a margin. A
   wider window is always safe; a narrower one can miss a delete.

   ```sh
   sudo journalctl --since "2026-10-09 02:00:00 UTC" --output=cat \
     | grep -o 'account_deleted user_id=[0-9a-f-]\{36\}' \
     | cut -d= -f2 | sort -u > ~/deleted-ids.txt
   wc -l ~/deleted-ids.txt
   ```

   Copy it off the server (from your machine): `scp <vps>:deleted-ids.txt .`

2. On the server you restore to, in `/srv/new2u`, stop the processes that
   read user data before anything else. The poller would otherwise poll the
   restored accounts, and the API serve them, before the deletes are replayed.

   ```sh
   docker compose stop api poller
   ```

3. Copy the dump there (from your machine) and restore it. `--clean` replaces
   what is in the database; `--single-transaction` means a failed restore
   changes nothing.

   ```sh
   scp new2u-20261009T020000Z.dump deleted-ids.txt deploy@SERVER_IP:
   ```

   ```sh
   docker compose exec -T postgres sh -c \
     'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --single-transaction' \
     < ~/new2u-20261009T020000Z.dump
   ```

4. Bring the schema up to the running release (the dump may be older):

   ```sh
   docker compose run --rm -T api alembic upgrade head
   ```

5. Replay the deletes:

   ```sh
   xargs -r docker compose run --rm -T api python -m app.privacy.delete < ~/deleted-ids.txt
   ```

   Each id prints either an `account_deleted user_id=<id>` log line or
   `not present: user_id=<id>`.

6. Check: run the same `xargs` command again. Every id must now print
   `not present`. Then start the stack and remove every copy of the dump and
   the list:

   ```sh
   docker compose up -d
   shred -u ~/deleted-ids.txt ~/new2u-*.dump     # on the server; same on your machine
   ```

### Rotating secrets

There is no two-key support yet: the stored ciphertexts carry a version byte but
no key id, and there is no re-encrypt command (a later task). Changing a key
therefore means signing in again, as below. Rotate when a key or the server may
be compromised; there is no routine schedule.

**`TOKEN_ENCRYPTION_KEY`** encrypts the Spotify refresh tokens and the PKCE
verifiers of sign-ins in progress. Nothing encrypted under the old key is usable
after the change, so every user must reconnect Spotify. Until they do, plays
beyond Spotify's last 50 are lost, so do this right after a poll and have each
user reconnect straight away.

1. Generate the new key (see `.env.example`).
2. In the deploy directory, stop the processes that use the key:

   ```sh
   docker compose stop api poller
   ```

3. Drop everything encrypted under the old key. Connections become
   `reauth_required` with reason `key_rotated`, which the app shows as "reconnect".

   ```sh
   docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
   BEGIN;
   UPDATE spotify_connection
      SET status = 'reauth_required', reauth_reason = 'key_rotated',
          refresh_token_ciphertext = NULL
    WHERE status = 'active';
   DELETE FROM oauth_login;
   COMMIT;
   SQL
   ```

4. Put the new key in `.env` as `TOKEN_ENCRYPTION_KEY`. Do not keep the old one.
5. If the old key may have leaked, each user first removes the app's access at
   <https://www.spotify.com/account/apps/>. That revokes every refresh token
   encrypted under it.
6. `docker compose up -d`. Each user signs in again from the app.
   `GET /v1/me/connection` shows `active` again once they have.

A backup taken before the rotation holds ciphertexts under the old key. After
restoring one, run step 3 again; the poller would otherwise log
`token_decrypt_failed` for each connection on every tick.

**`SESSION_TOKEN_SECRET`** keys the hashes of session tokens, refresh tokens,
claim secrets, callback codes and OAuth states. Change it in `.env` and run
`docker compose up -d`. Every session and every sign-in in progress stops
matching, so each user signs in again. No history is lost, because the Spotify
grant is untouched and the poller keeps collecting. The orphaned rows are
purged once they expire (`REFRESH_TOKEN_TTL_SECONDS`, `OAUTH_LOGIN_TTL_SECONDS`).

The one-off jobs below fill in data that older releases did not store yet. A
database that started on a current release already has it, so a new instance
never needs them.

### One-off: album artists backfill

Migration 0009 starts storing each play's album artists
(`provider_extras.spotify_album_artists`), so "New to you" names an album by its
own artist (for a compilation, often the label) instead of its tracks' artists.
Plays stored before it have none; until they are filled, the app shows the
artists found on every played track of the album, or "Unknown artist".

Run once, after deploying 0009, with the API and poller running:

```sh
docker compose run --rm -T api alembic upgrade head   # if not already at 0009
docker compose run --rm -T api python -m app.spotify.album_artists_backfill
```

- It calls `GET /v1/albums/{id}` once per album that still lacks artists, one
  at a time, 0.2 s apart, waiting on `429 Retry-After`. Several hundred albums
  take a few minutes.
- It only writes rows that lack the key and never replaces a stored value, so it
  is safe to run again and safe to run while the poller runs.
- Exit code `0` means every active connection succeeded. On `1`, read the log
  lines `album artists backfill user_id=... status=failed error_kind=...` and
  run it again later; it picks up where it stopped. `invalid_grant` means the
  user must reconnect (the poller marks that on its next tick).
- The log holds counts only, never album names or IDs.
- A later poll or history import also fills a row it sees again, so new plays
  never need this.

### One-off: album track count backfill

Migration 0010 starts storing each play's album track count
(`provider_extras.spotify_album_total_tracks`). An album is "new to you" only
once more than half of its tracks were played, so an album whose plays were all
stored before 0010 has an unknown count and is **not new** until it is filled.

The same job fills it, from the same `GET /v1/albums/{id}` call as the album
artists. Run once, after deploying 0010:

```sh
docker compose run --rm -T api alembic upgrade head   # if not already at 0010
docker compose run --rm -T api python -m app.spotify.album_artists_backfill
```

Everything in the album artists section above holds: one call per album that
lacks either value, safe to run again, counts-only log lines (still named
`album artists backfill`).

### One-off: album cover backfill

Plays are now stored with their album cover URL (`artwork_url`, a
`https://i.scdn.co/` image picked from the album's images: the smallest at least
300 px wide, else the widest). Plays stored before have none, and the app shows
no cover for them. No migration: the column already exists.

Run once, after deploying, with the API and poller running:

```sh
docker compose run --rm -T api python -m app.spotify.artwork_backfill
```

- It calls `GET /v1/albums/{id}` once per album that still has a play without a
  cover, one at a time, 0.2 s apart, waiting on `429 Retry-After`.
- It only writes rows with no cover and never replaces one, so it is safe to run
  again and safe to run while the poller runs. An album Spotify no longer has,
  or one with no usable image, is asked again on the next run.
- Exit code `0` means every active connection succeeded. On `1`, read the log
  lines `artwork backfill user_id=... status=failed error_kind=...` and run it
  again later; it picks up where it stopped.
- The log holds counts only, never album IDs or URLs.
- A later poll or history import also fills a cover on a row it sees again.

### One-off: album type backfill

Migration 0011 starts storing each play's Spotify release type
(`provider_extras.spotify_album_type`: `album`, `single` or `compilation`; any
other value is not stored). Singles, EPs included, are left out of every album
list and album count. An album whose plays were all stored before 0011 has no
type and **stays listed** until it is filled.

The album artists job fills it, from the same `GET /v1/albums/{id}` call. Run
once, after deploying 0011:

```sh
docker compose run --rm -T api alembic upgrade head   # if not already at 0011
docker compose run --rm -T api python -m app.spotify.album_artists_backfill
```

Everything in the album artists section above holds: one call per album that
lacks any of its values, safe to run again, counts-only log lines (still named
`album artists backfill`). `401` or `403` on an album call now stops that user's
run at once (`error_kind=album_http_401` or `album_http_403`), as the cover
backfill does.
