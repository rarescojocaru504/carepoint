"""Random per-installation secrets for the Compose demo (ADR-031).

    eval "$(python -m app.scripts.local_secrets /var/lib/carepoint/secrets)"

The settings fall back to development defaults that are published in this
repository. On a laptop showing the demo on shared Wi-Fi, those would let
anyone who obtained a user id forge access tokens, or turn leaked pickup-code
hashes back into codes. So on first start this generates a random JWT_SECRET
and PICKUP_CODE_PEPPER, keeps them in a private file in a Docker volume (so a
restart keeps everyone signed in and issued codes valid), and prints shell
`export` lines for the backend's start command.

A value already set in the environment always wins and is not printed: a
real deployment passes its own secrets and never uses this file.
"""

import os
import secrets
import sys
from pathlib import Path

NAMES = ("JWT_SECRET", "PICKUP_CODE_PEPPER")
FILENAME = "secrets.env"


def _read(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name in NAMES and value:
                values[name] = value
    return values


def _write(path: Path, values: dict[str, str]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)  # never reuse a file with someone else's mode
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.writelines(f"{name}={values[name]}\n" for name in NAMES)
    tmp.replace(path)


def ensure_secrets(directory: Path) -> dict[str, str]:
    """The stored secrets, generating (and storing) any that are missing."""
    path = directory / FILENAME
    values = _read(path)
    if any(name not in values for name in NAMES):
        for name in NAMES:
            values.setdefault(name, secrets.token_urlsafe(48))  # 64 chars of [A-Za-z0-9_-]
        directory.mkdir(parents=True, exist_ok=True)
        _write(path, values)
    return values


def exports(directory: Path, environ: dict[str, str]) -> str:
    """`export NAME=value` lines for the secrets the environment doesn't set."""
    missing = [name for name in NAMES if not environ.get(name)]
    if not missing:
        return ""
    stored = ensure_secrets(directory)
    # token_urlsafe values contain only [A-Za-z0-9_-], so no shell quoting is needed.
    return "".join(f"export {name}={stored[name]}\n" for name in missing)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m app.scripts.local_secrets <directory>")
    sys.stdout.write(exports(Path(sys.argv[1]), dict(os.environ)))
