"""deploy/env.template names every setting and holds no secret value (WHO-175)."""

import re
from pathlib import Path

import yaml  # type: ignore[import-untyped]  # PyYAML ships no type stubs

from app.core.config import Settings

ROOT = Path(__file__).resolve().parent.parent
SOURCES = {"secret", "input", "fixed", "compose", "default"}


def template_rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in (ROOT / "deploy" / "env.template").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, *rest = line.split()
        assert name not in rows, f"{name} is listed twice"
        rows[name] = rest
    return rows


def env_example_keys() -> set[str]:
    # "KEY=value" and the commented-out "# KEY=value" both count.
    pattern = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=")
    text = (ROOT / ".env.example").read_text()
    return {m.group(1) for line in text.splitlines() if (m := pattern.match(line))}


def test_every_env_example_key_is_in_the_template() -> None:
    missing = env_example_keys() - template_rows().keys()
    assert not missing, f"add to deploy/env.template: {sorted(missing)}"


def test_every_setting_is_in_the_template() -> None:
    missing = {name.upper() for name in Settings.model_fields} - template_rows().keys()
    assert not missing, f"add to deploy/env.template: {sorted(missing)}"


def test_only_fixed_rows_have_a_value() -> None:
    for name, rest in template_rows().items():
        assert rest, f"{name}: no source"
        source, *value = rest
        assert source in SOURCES, f"{name}: unknown source {source!r}"
        if source == "fixed":
            assert len(value) == 1, f"{name}: a fixed row needs exactly one value"
        else:
            assert not value, f"{name}: only fixed rows carry a value"


def test_the_vps_runs_in_production() -> None:
    assert template_rows()["APP_ENV"] == ["fixed", "production"]


def deploy_job_env() -> dict[str, str]:
    """NAME -> "secrets" or "vars", for each `NAME: ${{ secrets|vars.NAME }}` line."""
    pattern = re.compile(r"^\s+([A-Z][A-Z0-9_]*): \$\{\{ (secrets|vars)\.([A-Z][A-Z0-9_]*) \}\}$")
    found: dict[str, str] = {}
    text = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    for line in text.splitlines():
        if (m := pattern.match(line)) and m.group(3) != "GITHUB_TOKEN":
            assert m.group(1) == m.group(3), f"deploy.yml: {m.group(1)} reads {m.group(3)}"
            found[m.group(1)] = m.group(2)
    return found


def test_the_deploy_job_passes_every_template_input() -> None:
    # WHO-176: secrets come from GitHub secrets, everything else from variables.
    # API_IMAGE is set by the job itself. scripts/deploy-remote.sh derives
    # CADDY_TLS and CADDY_DEFAULT_SNI from API_HOSTNAME (`internal` is for test
    # targets only), and DATABASE_SSLMODE, DATABASE_CHANNEL_BINDING and
    # COMPOSE_PROFILES from DATABASE_HOST.
    passed = deploy_job_env()
    derived = {
        "API_IMAGE",
        "CADDY_TLS",
        "CADDY_DEFAULT_SNI",
        "DATABASE_SSLMODE",
        "DATABASE_CHANNEL_BINDING",
        "COMPOSE_PROFILES",
    }
    for name, (source, *_) in template_rows().items():
        if name in derived:
            assert name not in passed, f"deploy.yml must not pass {name}"
        elif source == "secret":
            assert passed.get(name) == "secrets", f"deploy.yml: {name} must come from secrets"
        elif source in {"input", "default"}:
            assert passed.get(name) == "vars", f"deploy.yml: {name} must come from vars"


# --- WHO-177 findings, the static half of each regression check ---------------

CADDYFILE = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
DEPLOY = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()


def caddy_block(text: str, opener: str) -> str:
    """The body of the brace block that starts on the line `opener {`."""
    lines = text.splitlines()
    start = lines.index(f"{opener} {{".lstrip())
    depth, body = 0, []
    for line in lines[start:]:
        depth += line.count("{") - line.count("}")
        body.append(line)
        if depth == 0:
            return "\n".join(body)
    raise AssertionError(f"unclosed block {opener!r}")


def test_every_image_is_pinned_by_digest() -> None:
    # Finding 2: a moved upstream tag must not change what is built or run.
    files = [
        "docker-compose.yml",
        "docker-compose.vps.yml",
        "Dockerfile",
        ".github/workflows/ci.yml",
    ]
    pattern = re.compile(r"^[ \t]*(?:image:|FROM)[ \t]+(\S+)", re.MULTILINE)
    found = 0
    for name in files:
        for ref in pattern.findall((ROOT / name).read_text()):
            found += 1
            if ref.startswith("${API_IMAGE"):
                continue  # deploy-remote.sh takes digests only, see below
            assert re.fullmatch(
                r"[a-z0-9./_-]+@sha256:[0-9a-f]{64}", ref
            ), f"{name}: {ref} not by digest"
    assert found >= 5


def test_every_action_is_pinned_by_commit_sha() -> None:
    # Finding 7.
    for name, text in {"ci.yml": CI, "deploy.yml": DEPLOY}.items():
        for ref in re.findall(r"uses: (\S+.*)$", text, re.MULTILINE):
            if ref.startswith("./"):
                continue
            assert re.fullmatch(
                r"actions/[a-z-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+", ref
            ), f"{name}: {ref}"


def test_caddy_drops_its_error_log() -> None:
    # Finding 3: http.log.error carries the full URI, query string included.
    global_options = caddy_block(CADDYFILE, "")
    assert re.search(r"log default \{\s*exclude http\.log\.error\s*\}", global_options)
    assert not re.search(r"^\s*log\b", CADDYFILE.split(global_options, 1)[1], re.MULTILINE)


def test_caddy_sends_hsts_without_subdomains_or_preload() -> None:
    # Finding 4.
    site = caddy_block(CADDYFILE, "{$API_HOSTNAME}")
    assert 'header Strict-Transport-Security "max-age=31536000"' in site
    hsts = [line for line in CADDYFILE.splitlines() if "Strict-Transport-Security" in line]
    assert hsts == ['\theader Strict-Transport-Security "max-age=31536000"']


def test_an_ip_hostname_gets_lets_encrypts_shortlived_profile() -> None:
    # WHO-180: Let's Encrypt issues IP certificates only under `shortlived`.
    snippet = caddy_block(CADDYFILE, "(tls_public_ip)")
    assert re.search(r"issuer acme \{\s*profile shortlived\s*\}", snippet)
    assert "profile" not in caddy_block(CADDYFILE, "(tls_public)")
    assert "default_sni {$CADDY_DEFAULT_SNI}" in caddy_block(CADDYFILE, "")


def test_production_deploys_only_by_hand_on_main() -> None:
    # Finding 1: no automatic deploy; the Deploy workflow is dispatch-only.
    assert "deploy.yml" not in CI and "secrets: inherit" not in CI
    on = DEPLOY.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"^  (\w+):", on, re.MULTILINE) == ["workflow_dispatch"]
    assert "    if: vars.DEPLOY_ENABLED == 'true'\n" in DEPLOY
    assert "    environment: production\n" in DEPLOY
    first_step = DEPLOY.split("    steps:\n", 1)[1].split("\n      - ", 2)[0]
    assert 'GITHUB_REF" == refs/heads/main' in first_step


def test_root_cannot_log_in_over_ssh() -> None:
    # Finding 8.
    harden = (ROOT / "deploy" / "harden.sh").read_text()
    assert "\nPermitRootLogin no\n" in harden
    assert "prohibit-password" not in harden


def test_the_provider_rejects_go_only_after_harden() -> None:
    # Finding 5: either the provider's REJECTs or ufw guard the host.
    bootstrap = (ROOT / "deploy" / "bootstrap.sh").read_text()
    main = bootstrap.split("\nmain() {\n", 1)[1]
    steps = [line.strip() for line in main.splitlines()]
    assert steps.index("run_harden") < steps.index("delete_provider_rejects")
    cleanup = bootstrap.split("cleanup_provider_firewall() {", 1)[1].split("\n}\n", 1)[0]
    code = [line for line in cleanup.splitlines() if not line.strip().startswith("#")]
    assert not [line for line in code if "iptables" in line or "REJECT" in line]


# WHO-181: the nightly backup's settings. The bucket key is a secret (both
# halves: the id names the account at some providers); the rest are variables.
BACKUP_SETTINGS = {
    "BACKUP_S3_ENDPOINT": "vars",
    "BACKUP_S3_REGION": "vars",
    "BACKUP_S3_BUCKET": "vars",
    "BACKUP_S3_ACCESS_KEY_ID": "secrets",
    "BACKUP_S3_SECRET_ACCESS_KEY": "secrets",
    "BACKUP_AGE_RECIPIENT": "vars",
}


def bash_array(path: str, name: str) -> list[str]:
    """The words of `readonly NAME=(...)` in a script, across lines."""
    text = (ROOT / path).read_text()
    match = re.search(rf"^readonly {name}=\(([^)]*)\)", text, re.MULTILINE)
    assert match, f"{path}: no {name}=(...)"
    return match.group(1).split()


# WHO-182: optional, on top of the six. The ping URL is a secret: whoever has
# it can fake a night's "backup OK".
BACKUP_OPTIONAL = {
    "BACKUP_PRUNE": "vars",
    "BACKUP_HEARTBEAT_URL": "secrets",
}


def bash_value(path: str, name: str) -> str:
    """The value of `readonly NAME='...'` in a script."""
    text = (ROOT / path).read_text()
    match = re.search(rf"^readonly {name}='([^']*)'$", text, re.MULTILINE)
    assert match, f"{path}: no {name}='...'"
    return match.group(1)


def test_the_deploy_job_passes_the_backup_settings() -> None:
    passed = deploy_job_env()
    for name, source in {**BACKUP_SETTINGS, **BACKUP_OPTIONAL}.items():
        assert passed.get(name) == source, f"deploy.yml: {name} must come from {source}"


def test_deploy_and_backup_script_agree_on_the_backup_settings() -> None:
    assert bash_array("scripts/deploy-remote.sh", "BACKUP_SETTINGS") == list(BACKUP_SETTINGS)
    assert bash_array("deploy/backup.sh", "SETTINGS") == list(BACKUP_SETTINGS)
    assert bash_array("scripts/deploy-remote.sh", "BACKUP_OPTIONAL") == list(BACKUP_OPTIONAL)


def test_deploy_and_backup_script_agree_on_the_url_rules() -> None:
    # WHO-182 I1: the deploy refuses what the job would refuse, before the server.
    for deploy_name, backup_name in [
        ("BACKUP_ENDPOINT_RE", "ENDPOINT_RE"),
        ("BACKUP_HEARTBEAT_RE", "HEARTBEAT_RE"),
    ]:
        assert bash_value("scripts/deploy-remote.sh", deploy_name) == bash_value(
            "deploy/backup.sh", backup_name
        )


def test_the_heartbeat_url_never_reaches_a_command_line() -> None:
    text = (ROOT / "deploy" / "backup.sh").read_text()
    code = [line for line in text.splitlines() if not line.strip().startswith("#")]
    curls = [line for line in code if re.search(r"^\s*\|? *curl ", line)]
    assert curls, "backup.sh: no curl"
    for line in curls:
        assert "BACKUP_HEARTBEAT_URL" not in line
        assert "-K -" in line, "the URL goes to curl in its config on stdin"


def test_the_deploy_uploads_and_installs_the_backup_script() -> None:
    assert "deploy/backup.sh" in bash_array("scripts/deploy-remote.sh", "UPLOAD_FILES")
    assert "deploy/backup.sh" in bash_array("deploy/release.sh", "FILES")


def test_the_backup_runs_as_deploy_only_when_configured() -> None:
    unit = (ROOT / "deploy" / "systemd" / "new2u-backup.service").read_text()
    lines = {line.strip() for line in unit.splitlines()}
    assert "User=deploy" in lines
    assert "ConditionPathExists=/srv/new2u/backup.env" in lines
    assert "ExecStart=/srv/new2u/deploy/backup.sh run" in lines
    timer = (ROOT / "deploy" / "systemd" / "new2u-backup.timer").read_text()
    assert "Persistent=true" in timer.splitlines()


def test_the_backup_tools_are_pinned_by_digest() -> None:
    for path, name in [
        ("deploy/backup.sh", "RCLONE_IMAGE"),
        ("scripts/ci/backup-target.sh", "MINIO_IMAGE"),
        ("scripts/ci/backup-target.sh", "MC_IMAGE"),
    ]:
        text = (ROOT / path).read_text()
        match = re.search(rf"^readonly {name}=(\S+)$", text, re.MULTILINE)
        assert match, f"{path}: no {name}"
        assert re.search(r"@sha256:[0-9a-f]{64}$", match.group(1)), f"{path}: {name} not pinned"


def test_backup_secrets_never_reach_a_command_line() -> None:
    # The key travels to rclone as `-e NAME`, never `-e NAME=value` or a flag.
    text = (ROOT / "deploy" / "backup.sh").read_text()
    assert "--s3-secret-access-key" not in text
    assert "--s3-access-key-id" not in text
    assert not re.search(r"-e RCLONE_CONFIG_\w+=", text)
    # backup.env is parsed, never sourced.
    assert not re.search(r"^\s*(source|\.) ", text, re.MULTILINE)


# --- External database (docs/self-hosting.md "External database") ---------------

VPS_COMPOSE = (ROOT / "docker-compose.vps.yml").read_text()


def test_the_local_postgres_runs_only_with_the_localdb_profile() -> None:
    services = yaml.safe_load(VPS_COMPOSE.replace("!reset", "").replace("!override", ""))[
        "services"
    ]
    assert services["postgres"]["profiles"] == ["localdb"]
    for name in ("api", "poller"):
        url = services[name]["environment"]["DATABASE_URL"]
        assert (
            "@${DATABASE_HOST:-postgres}:5432/" in url
        ), f"{name}: DATABASE_URL must follow DATABASE_HOST"
        assert url.endswith(
            "?sslmode=${DATABASE_SSLMODE:-prefer}"
            "&channel_binding=${DATABASE_CHANNEL_BINDING:-prefer}"
        ), f"{name}: no sslmode or channel_binding"
        assert services[name]["depends_on"]["postgres"]["required"] is False
    assert (
        "depends_on: !override" in VPS_COMPOSE
    ), "the base depends_on must be replaced, not merged"


def test_the_deploy_starts_the_local_postgres_unless_database_host_is_set() -> None:
    text = (ROOT / "scripts" / "deploy-remote.sh").read_text()
    assert "COMPOSE_PROFILES=localdb" in text
    assert "DATABASE_SSLMODE=require" in text
    assert "export DATABASE_SSLMODE DATABASE_CHANNEL_BINDING COMPOSE_PROFILES" in text


def test_the_external_dump_requires_channel_binding() -> None:
    # WHO-188 F1: the backup's pg_dump to DATABASE_HOST takes SCRAM-SHA-256-PLUS
    # only, like api and poller; the local container's exec branch has no TLS.
    text = (ROOT / "deploy" / "backup.sh").read_text()
    external = text.split('dump=("${COMPOSE[@]}" run ', 1)[1].split("\n    fi\n", 1)[0]
    assert "-e PGSSLMODE=require" in external
    assert "-e PGCHANNELBINDING=require" in external


def test_the_database_password_never_reaches_a_command_line() -> None:
    remote = (ROOT / "scripts" / "bootstrap-db-remote.sh").read_text()
    sends = [
        line for line in remote.splitlines() if "POSTGRES_PASSWORD" in line and "ssh_run" in line
    ]
    assert sends == ["printf '%s\\n' \"$POSTGRES_PASSWORD\" | ssh_run \\"], sends
    host = (ROOT / "deploy" / "db-host.sh").read_text()
    assert "IFS= read -r PASSWORD" in host, "db-host.sh reads the password from stdin"
    assert "psql_admin <<SQL" in host, "the SQL with the password goes to psql over stdin"
    backup = (ROOT / "deploy" / "backup.sh").read_text()
    assert (
        'PGPASSWORD="$POSTGRES_PASSWORD"' in backup
    ), "the backup client takes the password from its own environment"
    assert "-e PGPASSWORD" not in backup


def test_the_database_host_logs_connections_but_no_statements() -> None:
    host = (ROOT / "deploy" / "db-host.sh").read_text()
    conf = host.split('conf.d/new2u.conf" 0644 <<EOF\n', 1)[1].split("\nEOF\n", 1)[0]
    lines = conf.splitlines()
    assert "log_connections = on" in lines
    assert "log_min_error_statement = panic" in lines
