"""Per-installation secrets for the Compose demo (ADR-031)."""

import os
import stat
import subprocess
import sys
from pathlib import Path

from pydantic import SecretStr

from app.core.config import DEV_JWT_SECRET, DEV_PICKUP_CODE_PEPPER, Settings
from app.scripts.local_secrets import FILENAME, ensure_secrets, exports


def test_generates_strong_secrets_once_and_keeps_them(tmp_path: Path) -> None:
    first = ensure_secrets(tmp_path)
    assert set(first) == {"JWT_SECRET", "PICKUP_CODE_PEPPER"}
    assert first["JWT_SECRET"] != first["PICKUP_CODE_PEPPER"]
    assert all(len(value) >= 64 for value in first.values())
    assert first["JWT_SECRET"] != DEV_JWT_SECRET
    assert first["PICKUP_CODE_PEPPER"] != DEV_PICKUP_CODE_PEPPER

    # A restart reuses them, so sessions and issued pickup codes stay valid.
    assert ensure_secrets(tmp_path) == first


def test_secrets_are_stored_privately(tmp_path: Path) -> None:
    ensure_secrets(tmp_path)
    if os.name == "posix":  # Windows has no owner-only mode bits
        assert stat.S_IMODE((tmp_path / FILENAME).stat().st_mode) == 0o600


def test_different_installations_get_different_secrets(tmp_path: Path) -> None:
    assert ensure_secrets(tmp_path / "a") != ensure_secrets(tmp_path / "b")


def test_secrets_set_in_the_environment_win(tmp_path: Path) -> None:
    assert exports(tmp_path, {"JWT_SECRET": "x" * 40, "PICKUP_CODE_PEPPER": "y" * 40}) == ""
    assert not (tmp_path / FILENAME).exists()

    only_pepper = exports(tmp_path, {"JWT_SECRET": "x" * 40})
    assert only_pepper.startswith("export PICKUP_CODE_PEPPER=")
    assert "JWT_SECRET" not in only_pepper


def test_a_damaged_file_is_completed_not_trusted(tmp_path: Path) -> None:
    (tmp_path / FILENAME).write_text("JWT_SECRET=kept-value\nGARBAGE\nPICKUP_CODE_PEPPER=\n")
    values = ensure_secrets(tmp_path)
    assert values["JWT_SECRET"] == "kept-value"
    assert len(values["PICKUP_CODE_PEPPER"]) >= 64


def test_the_printed_exports_configure_the_backend(tmp_path: Path) -> None:
    script = exports(tmp_path, {})
    env = dict(line.removeprefix("export ").split("=", 1) for line in script.splitlines())
    # Strong enough for the production checks, which refuse the dev defaults.
    settings = Settings(
        environment="production",
        jwt_secret=SecretStr(env["JWT_SECRET"]),
        pickup_code_pepper=SecretStr(env["PICKUP_CODE_PEPPER"]),
    )
    assert settings.jwt_secret.get_secret_value() == env["JWT_SECRET"]


def test_command_line(tmp_path: Path) -> None:
    clean = {k: v for k, v in os.environ.items() if k not in ("JWT_SECRET", "PICKUP_CODE_PEPPER")}
    out = subprocess.run(  # noqa: S603  (our own module, run with this interpreter)
        [sys.executable, "-m", "app.scripts.local_secrets", str(tmp_path)],
        capture_output=True, text=True, check=True, env=clean,
        cwd=Path(__file__).resolve().parents[1],
    ).stdout  # fmt: skip
    assert [line.split("=")[0] for line in out.splitlines()] == [
        "export JWT_SECRET",
        "export PICKUP_CODE_PEPPER",
    ]
