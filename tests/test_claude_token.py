"""Claude plan token: loading, expiry warnings, the in-factory check, and scripts/claude-token.sh."""
import json
import os
import pathlib
import shutil
import subprocess
import time

import pytest

from factory import claude_token
from factory.config import claude_oauth_token, claude_token_warning

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "claude-token.sh"
TOKEN = "sk-ant-oat01-" + "Ab1_-" * 18 + "ZZ12"


@pytest.fixture()
def secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    return tmp_path


def write(secrets, token=TOKEN, age_days=0):
    (secrets / "claude_oauth_token").write_text(json.dumps(
        {"token": token, "created_epoch": int(time.time() - age_days * 86400), "source": "setup-token"}))


# ------------------------------------------------------------------ loading
def test_secret_file_wins_over_env(secrets, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-token")
    write(secrets)
    token, created = claude_oauth_token()
    assert token == TOKEN and created is not None


def test_raw_file_empty_file_and_env_fallback(secrets, monkeypatch):
    (secrets / "claude_oauth_token").write_text(TOKEN + "\n")
    assert claude_oauth_token() == (TOKEN, None)
    (secrets / "claude_oauth_token").write_text("")                  # `make init` placeholder
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-token")
    assert claude_oauth_token() == ("env-token", None)


def test_corrupt_json_is_a_clear_error(secrets):
    (secrets / "claude_oauth_token").write_text("{not json")
    with pytest.raises(SystemExit, match="make claude-token"):
        claude_oauth_token()


# ------------------------------------------------------------------ expiry warnings
def test_expiry_warnings(secrets):
    assert claude_token_warning() == "No Claude plan token is stored. On the Mac, run: make claude-token"
    write(secrets, age_days=10)
    assert claude_token_warning() is None
    write(secrets, age_days=350)
    assert "expires in about 15 days" in claude_token_warning()
    write(secrets, age_days=400)
    assert "has expired" in claude_token_warning()
    (secrets / "claude_oauth_token").write_text(TOKEN)              # unknown age: no false alarm
    assert claude_token_warning() is None


def test_no_warning_on_the_api_key_route(secrets, monkeypatch):
    from factory import config
    p = dict(config.policy(), anthropic_route="api_key")
    monkeypatch.setattr(config, "policy", lambda: p)
    assert claude_token_warning() is None


# ------------------------------------------------------------------ in-factory check
@pytest.mark.parametrize("behaviour,code,expect", [
    ("ok", 0, "OK: Claude answered"),
    ("quota", 2, "usage limit is reached"),
    ("reject", 1, "Claude rejected the call"),
])
def test_check_reports_without_leaking_the_token(secrets, monkeypatch, capsys, behaviour, code, expect):
    from types import SimpleNamespace

    from factory import pool
    write(secrets, age_days=5)

    class Fake:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kw):
            if behaviour == "quota":
                raise pool.QuotaExhausted("usage limit reached")
            if behaviour == "reject":
                raise RuntimeError("agent sdk error: invalid bearer token")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    monkeypatch.setattr(pool, "SubscriptionAdapter", Fake)
    assert claude_token.check() == code
    out = capsys.readouterr().out
    assert expect in out and "Expires: in about 360 days" in out and TOKEN not in out


def test_check_without_a_token(secrets, capsys):
    assert claude_token.check() == 1
    assert "make claude-token" in capsys.readouterr().out


# ------------------------------------------------------------------ scripts/claude-token.sh
FAKE_CLAUDE = r'''#!/usr/bin/env bash
[ "$1" = "setup-token" ] || exit 0
printf '\033[?25l\033[2K\033[1G✓ Long-lived authentication token created successfully!\r\n\r\n'
printf 'Your OAuth token (valid for 1 year):\r\n\r\n\033[1m%s\033[22m\r\n\033[?25h' "$FAKE_TOKEN"
'''


@pytest.fixture()
def factory_dir(tmp_path):
    if not shutil.which("script"):
        pytest.skip("script(1) not installed")
    d = tmp_path / "factory"
    (d / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, d / "scripts" / "claude-token.sh")
    (d / ".env.factory").write_text(f"LANGFUSE_PUBLIC_KEY=pk\nCLAUDE_CODE_OAUTH_TOKEN={TOKEN[:-4]}OLD1\n")
    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    return d, fake


def run(d, fake, *args, stdin="", **env):
    return subprocess.run(["bash", str(d / "scripts" / "claude-token.sh"), *args], input=stdin, text=True,
                          capture_output=True, timeout=60,
                          env={**os.environ, "CLAUDE_BIN": str(fake), "FAKE_TOKEN": TOKEN, **env})


def test_script_captures_setup_token_and_never_prints_it(factory_dir):
    d, fake = factory_dir
    r = run(d, fake, "new")
    assert r.returncode == 0, r.stderr
    secret = d / "secrets" / "claude_oauth_token"
    assert json.loads(secret.read_text())["token"] == TOKEN
    assert oct(secret.stat().st_mode & 0o777) == "0o600"
    assert "CLAUDE_CODE_OAUTH_TOKEN=\n" in (d / ".env.factory").read_text()      # stale copy cleared
    summary = r.stdout.split("Cleared")[-1]                                       # the script's own lines
    assert TOKEN not in summary and "sk-ant-oat01-…ZZ12" in summary
    s = run(d, fake, "status")
    assert s.returncode == 0 and "expires in about 365 days" in s.stdout and TOKEN not in s.stdout


def test_script_paste_rejects_garbage_and_keeps_inode(factory_dir):
    d, fake = factory_dir
    assert run(d, fake, "paste", stdin=TOKEN + "\n").returncode == 0
    inode = (d / "secrets" / "claude_oauth_token").stat().st_ino
    bad = run(d, fake, "paste", stdin="hello\n")
    assert bad.returncode == 1 and "does not look like" in bad.stderr
    assert run(d, fake, "paste", stdin=TOKEN[:-4] + "NEW9\n").returncode == 0
    assert (d / "secrets" / "claude_oauth_token").stat().st_ino == inode            # running containers see it


def test_script_failed_login_stores_nothing(factory_dir, tmp_path):
    d, _ = factory_dir
    failing = tmp_path / "claude-fail"
    failing.write_text("#!/usr/bin/env bash\necho 'Authorization cancelled'; exit 3\n")
    failing.chmod(0o755)
    r = run(d, failing, "new")
    assert r.returncode == 1 and "Nothing was stored" in r.stderr
    assert not (d / "secrets" / "claude_oauth_token").exists()
