"""Generate each Part B service's gRPC bindings from contracts/proto.

Contracts are files, not shared code (spec section 1.2): each service gets its own copy of
the generated modules, under `services/<service>/gen/`, from the same .proto files. A
service generates the contracts it serves *and* the ones it calls.

    python scripts/gen_proto.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1]
PROTO = ROOT / "contracts" / "proto"

#: service -> the contracts it needs
NEEDS = {
    "knowledge": ["smarthealth/knowledge/v1/knowledge.proto"],
    "assistant": [
        "smarthealth/assistant/v1/assistant.proto",
        "smarthealth/knowledge/v1/knowledge.proto",
    ],
}


def main() -> int:
    include = Path(protoc.__file__).parent / "_proto"
    for service, files in NEEDS.items():
        out = ROOT / "services" / service / "gen"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        code = protoc.main([
            "grpc_tools.protoc", f"-I{PROTO}", f"-I{include}",
            f"--python_out={out}", f"--grpc_python_out={out}", f"--pyi_out={out}",
            *[str(PROTO / name) for name in files],
        ])
        if code:
            return code
        # No __init__.py anywhere: `smarthealth` stays a namespace package, so one process
        # holding two services' bindings (the test suite) sees both, as each container
        # sees its own.
        print(f"{service}: {', '.join(files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
