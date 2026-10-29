"""Write the machine protocol's JSON Schema (docs/machine-protocol.schema.json).

python -m app.scripts.export_protocol_schema > ../docs/machine-protocol.schema.json
"""

import json
import sys

from app.schemas.machine_protocol import protocol_json_schema


def render() -> str:
    return json.dumps(protocol_json_schema(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render())
