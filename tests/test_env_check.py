"""scripts/env-check.sh: .env.factory is made complete before `make up` (no Docker needed: it is faked)."""
import os
import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent


def setup(tmp_path, *, docker_up=True, db_exists=False):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "env-check.sh", tmp_path / "scripts")
    shutil.copy(ROOT / ".env.factory.example", tmp_path)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text("#!/bin/sh\n"
                                 f'[ "$1" = info ] && exit {0 if docker_up else 1}\n'
                                 f'[ "$1" = volume ] && exit {0 if db_exists else 1}\nexit 1\n')
    (bin_ / "docker").chmod(0o755)
    return tmp_path


def run(d):
    env = {**os.environ, "PATH": f"{d / 'bin'}:{os.environ['PATH']}"}
    return subprocess.run(["bash", "scripts/env-check.sh"], cwd=d, env=env, capture_output=True, text=True)


def values(d):
    return dict(line.split("=", 1) for line in (d / ".env.factory").read_text().splitlines()
                if "=" in line and not line.startswith("#"))


def test_creates_the_file_with_generated_passwords(tmp_path):
    d = setup(tmp_path)
    assert run(d).returncode == 0
    v = values(d)
    assert len(v["FACTORY_DB_PASSWORD"]) == 48 and len(v["FACTORY_BOARD_DB_PASSWORD"]) == 48
    assert oct((d / ".env.factory").stat().st_mode & 0o777) == "0o600"


def test_adds_a_missing_key_to_an_older_file_and_keeps_the_rest(tmp_path):
    d = setup(tmp_path)
    (d / ".env.factory").write_text("FACTORY_DB_PASSWORD=keepme123\nLANGFUSE_PUBLIC_KEY=pk\n")
    r = run(d)
    assert r.returncode == 0, r.stderr
    v = values(d)
    assert v["FACTORY_DB_PASSWORD"] == "keepme123" and v["LANGFUSE_PUBLIC_KEY"] == "pk"
    assert len(v["FACTORY_BOARD_DB_PASSWORD"]) == 48 and "FACTORY_SELF_REPOS" in v
    before = (d / ".env.factory").read_text()
    assert run(d).returncode == 0 and (d / ".env.factory").read_text() == before     # second run: no change


def test_never_changes_the_main_password_of_an_existing_database(tmp_path):
    d = setup(tmp_path, db_exists=True)
    (d / ".env.factory").write_text("FACTORY_DB_PASSWORD=change-me\n")
    r = run(d)
    assert r.returncode == 1 and "already exists" in r.stderr
    assert values(d)["FACTORY_DB_PASSWORD"] == "change-me"


def test_board_password_on_an_existing_database_says_to_run_db_board(tmp_path):
    d = setup(tmp_path, db_exists=True)
    (d / ".env.factory").write_text("FACTORY_DB_PASSWORD=realpw123456\n")
    r = run(d)
    assert r.returncode == 0 and "make db-board" in r.stdout


def test_refuses_to_guess_when_docker_is_not_running(tmp_path):
    d = setup(tmp_path, docker_up=False)
    (d / ".env.factory").write_text("FACTORY_DB_PASSWORD=\n")
    r = run(d)
    assert r.returncode == 1 and "Docker is not running" in r.stderr and values(d)["FACTORY_DB_PASSWORD"] == ""
