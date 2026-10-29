"""Write the API's OpenAPI schema to mobile/openapi.json, the source of the app's types.

python -m app.scripts.export_openapi [PATH]

The file is written directly, with LF line endings and UTF-8, rather than
through a shell redirect: on Windows a redirect writes CRLF (and PowerShell 5.1
writes UTF-16), which would make the drift test fail for no real change.
"""

import argparse
import json
from pathlib import Path

from app.main import app

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "mobile" / "openapi.json"


def render() -> str:
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args()
    path: Path = args.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
