"""Speak gRPC-Web to the assistant through Envoy, exactly as a browser does.

    python scripts/grpcweb_smoke.py --email you@example.com --password ...

Logs in through Envoy's /api route (Part A), then calls Assistant.Chat over gRPC-Web on
HTTP/1.1 -- frames of `flag(1) length(4) message`, data frames flag 0x00, the trailer frame
0x80 -- and prints each streamed event. This is the proof that the edge works: browser
protocol in, native gRPC to the assistant, native gRPC on to knowledge, a token Part A
issued verified by both against Part A's JWKS.
"""

from __future__ import annotations

import argparse
import struct
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "assistant" / "gen"))

from smarthealth.assistant.v1 import assistant_pb2  # noqa: E402


def frames(body: bytes):
    offset = 0
    while offset + 5 <= len(body):
        flag, length = body[offset], struct.unpack(">I", body[offset + 1:offset + 5])[0]
        yield flag, body[offset + 5:offset + 5 + length]
        offset += 5 + length


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--edge", default="http://localhost:8088")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--message", default="which cardiology doctors are there")
    args = parser.parse_args()

    login = httpx.post(f"{args.edge}/api/auth/login",
                       json={"email": args.email, "password": args.password}, timeout=10)
    login.raise_for_status()
    token = login.json()["access_token"]

    request = assistant_pb2.ChatRequest(conversation_id=str(uuid.uuid4()),
                                        turn_id=str(uuid.uuid4()), message=args.message)
    payload = request.SerializeToString()
    response = httpx.post(
        f"{args.edge}/smarthealth.assistant.v1.Assistant/Chat",
        content=b"\x00" + struct.pack(">I", len(payload)) + payload,
        headers={"content-type": "application/grpc-web+proto", "x-grpc-web": "1",
                 "authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    status = response.headers.get("grpc-status")
    for flag, data in frames(response.content):
        if flag & 0x80:
            trailers = dict(line.split(":", 1) for line in data.decode().strip().splitlines())
            status = trailers.get("grpc-status", status).strip()
            continue
        event = assistant_pb2.ChatEvent.FromString(data)
        print(event.seq, event.WhichOneof("kind"), str(getattr(event, event.WhichOneof("kind"))).strip())
    print("grpc-status", status)
    return 0 if status in (None, "0") else 1


if __name__ == "__main__":
    sys.exit(main())
