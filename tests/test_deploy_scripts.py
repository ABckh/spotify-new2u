"""The deploy shell scripts' own rules, run with fake docker/ssh (WHO-180).

- deploy/release.sh public-ports: what `release.sh verify` refuses (WHO-177
  finding 6).
- scripts/deploy-remote.sh: which API_HOSTNAME it takes, the certificate mode
  it picks for Caddy, and that it deploys by digest only (finding 9).
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]  # PyYAML ships no type stubs

ROOT = Path(__file__).resolve().parent.parent
DIGEST = "sha256:" + "a" * 64
IMAGE = f"ghcr.io/owner/new2u@{DIGEST}"


def fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return bin_dir


def public_ports(tmp_path: Path, docker_ps: str) -> list[str]:
    bin_dir = fake_bin(tmp_path, "docker", 'printf "%s\\n" "$FAKE_DOCKER_PS"\n')
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_DOCKER_PS": docker_ps}
    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / "release.sh"), "public-ports"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def test_public_ports_passes_the_stack_as_deployed(tmp_path: Path) -> None:
    docker_ps = "\n".join(
        [
            "new2u-caddy-1 0.0.0.0:80->80/tcp, [::]:80->80/tcp, "
            "0.0.0.0:443->443/tcp, [::]:443->443/tcp",
            "new2u-api-1 8000/tcp",
            "new2u-postgres-1 5432/tcp",
            "new2u-poller-1 ",
            "local-db 127.0.0.1:5432->5432/tcp, [::1]:5433->5432/tcp",
        ]
    )
    assert public_ports(tmp_path, docker_ps) == []


def test_public_ports_names_every_other_public_mapping(tmp_path: Path) -> None:
    docker_ps = "\n".join(
        [
            "new2u-caddy-1 0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp",
            "new2u-api-1 0.0.0.0:8000->8000/tcp, [::]:8000->8000/tcp",
            "new2u-postgres-1 10.0.0.5:5432->5432/tcp",
            "dns 0.0.0.0:53->53/udp",
            "range 0.0.0.0:9000-9001->9000-9001/tcp",
        ]
    )
    assert public_ports(tmp_path, docker_ps) == [
        "new2u-api-1 publishes 0.0.0.0:8000",
        "new2u-api-1 publishes [::]:8000",
        "new2u-postgres-1 publishes 10.0.0.5:5432",
        "dns publishes 0.0.0.0:53",
        "range publishes 0.0.0.0:9000-9001",
    ]


def test_release_verify_runs_the_port_check() -> None:
    script = (ROOT / "deploy" / "release.sh").read_text()
    verify = script.split("\nverify() {\n", 1)[1].split("\n}\n", 1)[0]
    assert 'public="$(public_ports)"' in verify
    assert '[[ -z "$public" ]] || die' in verify


# --- scripts/deploy-remote.sh --------------------------------------------------

# A fake ssh: answers the upload mktemp, keeps the .env that write-env gets on
# stdin, and fails there, so nothing after it runs.
FAKE_SSH = """\
case "$*" in
    *mktemp*) echo /srv/new2u/.upload.abc123 ;;
    *write-env*) cat >"$ENV_CAPTURE"; exit 97 ;;
    *) cat >/dev/null ;;
esac
"""

SECRETS = {
    "POSTGRES_PASSWORD": "pg",
    "TOKEN_ENCRYPTION_KEY": "key",
    "SESSION_TOKEN_SECRET": "session",
    "SPOTIFY_CLIENT_ID": "client",
    "SPOTIFY_CLIENT_SECRET": "",
    "SPOTIFY_ALLOWED_ACCOUNT_IDS": "account",
    "SPOTIFY_REDIRECT_URI": "https://example.invalid/auth/spotify/callback",
    "APP_CALLBACK_URL": "new2u://signed-in",
}


def deploy_remote(
    tmp_path: Path, **values: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    bin_dir = fake_bin(tmp_path, "ssh", FAKE_SSH)
    capture = tmp_path / "env"
    key = tmp_path / "key"
    key.write_text("unused\n")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "ENV_CAPTURE": str(capture),
        "DEPLOY_HOST": "192.0.2.10",
        "DEPLOY_USER": "deploy",
        "DEPLOY_KNOWN_HOSTS": "192.0.2.10 ssh-ed25519 AAAA",
        "DEPLOY_SSH_KEY_FILE": str(key),
        "API_IMAGE": IMAGE,
        **SECRETS,
        **values,
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "deploy-remote.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    rendered: dict[str, str] = {}
    if capture.exists():
        for line in capture.read_text().splitlines():
            name, _, value = line.partition("=")
            rendered[name] = value.strip("'")
    return result, rendered


@pytest.mark.parametrize(
    ("hostname", "tls", "sni"),
    [
        ("api.example.org", "public", "api.example.org"),
        ("203.0.113.11", "public_ip", "203.0.113.11"),
        ("[2001:db8::10]", "public_ip", "2001:db8::10"),
    ],
)
def test_the_certificate_mode_follows_the_hostname(
    tmp_path: Path, hostname: str, tls: str, sni: str
) -> None:
    result, rendered = deploy_remote(tmp_path, API_HOSTNAME=hostname)

    assert result.returncode == 97, result.stderr
    assert rendered["API_HOSTNAME"] == hostname
    assert rendered["CADDY_TLS"] == tls
    assert rendered["CADDY_DEFAULT_SNI"] == sni


def test_internal_certificates_stay_possible_for_test_targets(tmp_path: Path) -> None:
    result, rendered = deploy_remote(
        tmp_path, API_HOSTNAME="api.bootstrap.test", CADDY_TLS="internal"
    )

    assert result.returncode == 97, result.stderr
    assert rendered["CADDY_TLS"] == "internal"


@pytest.mark.parametrize(
    ("hostname", "tls"),
    [
        # `public` on an IP would quietly serve a certificate from Caddy's own CA.
        ("203.0.113.11", "public"),
        ("api.example.org", "public_ip"),
        ("api.example.org", "letsencrypt"),
    ],
)
def test_a_certificate_mode_that_does_not_fit_is_refused(
    tmp_path: Path, hostname: str, tls: str
) -> None:
    result, rendered = deploy_remote(tmp_path, API_HOSTNAME=hostname, CADDY_TLS=tls)

    assert result.returncode == 2
    assert "CADDY_TLS" in result.stderr
    assert rendered == {}


@pytest.mark.parametrize(
    "hostname",
    [
        "256.1.1.1",
        "203.0.113.011",
        "1.2.3",
        "2001:db8::10",  # IPv6 without brackets
        "[2001:db8::g]",
        "API.example.org",
        "https://203.0.113.11",
        "203.0.113.11:443",
    ],
)
def test_a_bad_api_hostname_is_refused(tmp_path: Path, hostname: str) -> None:
    result, rendered = deploy_remote(tmp_path, API_HOSTNAME=hostname)

    assert result.returncode == 2
    assert "API_HOSTNAME" in result.stderr
    assert rendered == {}


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/owner/new2u:" + "b" * 40,
        "ghcr.io/owner/new2u:latest",
        "ghcr.io/owner/new2u",
    ],
)
def test_deploy_takes_an_image_by_digest_only(tmp_path: Path, image: str) -> None:
    result, rendered = deploy_remote(tmp_path, API_HOSTNAME="api.example.org", API_IMAGE=image)

    assert result.returncode == 2
    assert "by digest" in result.stderr
    assert rendered == {}


# --- deploy-remote.sh: the external database (WHO-188 F1) ---------------------


def compose_database_urls(rendered: dict[str, str]) -> dict[str, str]:
    """api's and poller's DATABASE_URL as Compose fills them in from .env."""
    text = (ROOT / "docker-compose.vps.yml").read_text()
    services = yaml.safe_load(text.replace("!reset", "").replace("!override", ""))["services"]

    def fill(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(3) or ""
        value = rendered.get(name, "")
        return value if value or match.group(2) != ":-" else default

    pattern = r"\$\{(\w+)(:-|:\?)?([^}]*)\}"
    return {
        name: re.sub(pattern, fill, services[name]["environment"]["DATABASE_URL"])
        for name in ("api", "poller")
    }


def test_an_external_database_requires_channel_binding(tmp_path: Path) -> None:
    result, rendered = deploy_remote(
        tmp_path, API_HOSTNAME="api.example.org", DATABASE_HOST="10.0.1.240"
    )

    assert result.returncode == 97, result.stderr
    assert rendered["DATABASE_CHANNEL_BINDING"] == "require"
    for name, url in compose_database_urls(rendered).items():
        assert "@10.0.1.240:5432/" in url, name
        assert url.endswith("?sslmode=require&channel_binding=require"), f"{name}: {url}"


def test_the_local_container_does_not_require_tls(tmp_path: Path) -> None:
    # The postgres container has no TLS: require would refuse every connection.
    result, rendered = deploy_remote(tmp_path, API_HOSTNAME="api.example.org")

    assert result.returncode == 97, result.stderr
    # Empty default rows are left out of .env; Compose then fills in prefer.
    assert "DATABASE_CHANNEL_BINDING" not in rendered
    for name, url in compose_database_urls(rendered).items():
        assert "@postgres:5432/" in url, name
        assert "require" not in url, f"{name}: {url}"


@pytest.mark.parametrize(
    "extra",
    [
        {"DATABASE_HOST": "10.0.1.240", "DATABASE_CHANNEL_BINDING": "prefer"},
        {"DATABASE_HOST": "10.0.1.240", "DATABASE_CHANNEL_BINDING": "disable"},
        {"DATABASE_CHANNEL_BINDING": "require"},
    ],
)
def test_a_channel_binding_that_does_not_fit_is_refused(
    tmp_path: Path, extra: dict[str, str]
) -> None:
    result, rendered = deploy_remote(tmp_path, API_HOSTNAME="api.example.org", **extra)

    assert result.returncode == 2
    assert "DATABASE_CHANNEL_BINDING" in result.stderr
    assert rendered == {}


# --- deploy-remote.sh: backup settings (WHO-181, WHO-182) ---------------------

BACKUP = {
    "BACKUP_S3_ENDPOINT": "https://s3.eu-central-003.backblazeb2.com",
    "BACKUP_S3_REGION": "eu-central-003",
    "BACKUP_S3_BUCKET": "new2u-backups",
    "BACKUP_S3_ACCESS_KEY_ID": "key-id",
    "BACKUP_S3_SECRET_ACCESS_KEY": "key-secret",
    "BACKUP_AGE_RECIPIENT": "age1" + "q" * 58,
}


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"BACKUP_PRUNE": "off"},
        {"BACKUP_PRUNE": "on"},
        {"BACKUP_HEARTBEAT_URL": "https://hc-ping.com/0a1b2c3d-4e5f-6789-abcd-ef0123456789"},
        {"BACKUP_S3_ENDPOINT": "http://127.0.0.1:9000"},
        {"BACKUP_S3_ENDPOINT": "http://localhost:9000/"},
        {"BACKUP_HEARTBEAT_URL": "http://127.0.0.1:9099/ping/abc"},
    ],
)
def test_backup_settings_that_pass(tmp_path: Path, extra: dict[str, str]) -> None:
    # 97: the fake ssh stops at write-env, which comes after both renders.
    result, _ = deploy_remote(tmp_path, API_HOSTNAME="api.example.org", **{**BACKUP, **extra})

    assert result.returncode == 97, result.stderr


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        # WHO-182 I1: plain http only to this machine.
        ({"BACKUP_S3_ENDPOINT": "http://s3.example.org"}, "BACKUP_S3_ENDPOINT"),
        ({"BACKUP_S3_ENDPOINT": "http://127.0.0.1.example.org"}, "BACKUP_S3_ENDPOINT"),
        ({"BACKUP_S3_ENDPOINT": "ftp://s3.example.org"}, "BACKUP_S3_ENDPOINT"),
        ({"BACKUP_PRUNE": "no"}, "BACKUP_PRUNE"),
        ({"BACKUP_HEARTBEAT_URL": "http://hc-ping.com/abc"}, "BACKUP_HEARTBEAT_URL"),
        ({"BACKUP_HEARTBEAT_URL": "https://hc-ping.com/a b"}, "BACKUP_HEARTBEAT_URL"),
        ({"BACKUP_HEARTBEAT_URL": 'https://hc-ping.com/"x'}, "BACKUP_HEARTBEAT_URL"),
    ],
)
def test_backup_settings_that_are_refused(
    tmp_path: Path, extra: dict[str, str], message: str
) -> None:
    result, rendered = deploy_remote(
        tmp_path, API_HOSTNAME="api.example.org", **{**BACKUP, **extra}
    )

    assert result.returncode == 2
    assert message in result.stderr
    assert rendered == {}
    # The URL is a secret: an error names the setting, never the value.
    if "BACKUP_HEARTBEAT_URL" in extra:
        assert extra["BACKUP_HEARTBEAT_URL"] not in result.stderr + result.stdout


@pytest.mark.parametrize("name", ["BACKUP_PRUNE", "BACKUP_HEARTBEAT_URL"])
def test_optional_backup_settings_need_the_six(tmp_path: Path, name: str) -> None:
    result, rendered = deploy_remote(
        tmp_path, API_HOSTNAME="api.example.org", **{name: "https://hc-ping.com/x"}
    )

    assert result.returncode == 2
    assert f"{name} set, but none of" in result.stderr
    assert rendered == {}
