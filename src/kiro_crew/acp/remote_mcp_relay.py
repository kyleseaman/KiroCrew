"""Standalone stdlib relay copied into a remote ACP workspace."""

from __future__ import annotations

# When executed from this source directory, ``types.py`` beside this relay would
# shadow the standard library's ``types`` module. The copied workspace relay has
# no package siblings, but keeping direct execution safe makes the artifact
# independently testable before it is uploaded.
import sys

if __name__ == "__main__" and sys.path:
    sys.path.pop(0)

import argparse
import json
import os
import socket
import threading
from pathlib import Path

_TOKEN_FILE_LIMIT = 4 * 1024
_TOKEN_TEXT_LIMIT = 512
_JSON_LINE_LIMIT = 1024 * 1024
_COPY_CHUNK_BYTES = 64 * 1024
_CONNECT_TIMEOUT_SECONDS = 15.0
_JSONRPC_TRANSPORT_UNAVAILABLE = -32000


def _read_token(path: Path) -> str:
    with path.open("rb") as handle:
        raw = handle.read(_TOKEN_FILE_LIMIT + 1)
    if len(raw) > _TOKEN_FILE_LIMIT:
        raise ValueError("capability file is too large")
    token = raw.decode("utf-8").strip()
    if not token or len(token) > _TOKEN_TEXT_LIMIT:
        raise ValueError("capability file is invalid")
    return token


def _copy_stdin(sock: socket.socket) -> None:
    try:
        while data := os.read(sys.stdin.fileno(), _COPY_CHUNK_BYTES):
            sock.sendall(data)
    except (BrokenPipeError, ConnectionError, OSError):
        return
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _run_proxy(port: int, cap_file: Path) -> int:
    try:
        token = _read_token(cap_file)
        sock = socket.create_connection(
            ("127.0.0.1", port),
            timeout=_CONNECT_TIMEOUT_SECONDS,
        )
    except (OSError, UnicodeError, ValueError):
        return 1

    try:
        sock.settimeout(None)
        auth = json.dumps(
            {"version": 1, "token": token},
            separators=(",", ":"),
        ).encode("utf-8")
        sock.sendall(auth + b"\n")
        input_thread = threading.Thread(
            target=_copy_stdin,
            args=(sock,),
            name="remote-mcp-stdin",
            daemon=True,
        )
        input_thread.start()
        while data := sock.recv(_COPY_CHUNK_BYTES):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        return 0
    except (BrokenPipeError, ConnectionError, OSError):
        return 1
    finally:
        sock.close()


def _unsupported_response(request: object, code: str) -> dict[str, object] | None:
    if not isinstance(request, dict) or "id" not in request:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "error": {
            "code": _JSONRPC_TRANSPORT_UNAVAILABLE,
            "message": "Remote MCP transport unavailable",
            "data": {"code": code},
        },
    }


def _run_unsupported(code: str) -> int:
    for raw_line in sys.stdin.buffer:
        if len(raw_line) > _JSON_LINE_LIMIT:
            return 1
        try:
            request = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        response = _unsupported_response(request, code)
        if response is None:
            continue
        sys.stdout.buffer.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--cap-file", type=Path)
    parser.add_argument("--unsupported-code")
    args = parser.parse_args(argv)
    proxy_mode = args.port is not None or args.cap_file is not None
    if args.unsupported_code:
        if proxy_mode:
            parser.error("unsupported mode cannot include proxy arguments")
    elif args.port is None or args.cap_file is None:
        parser.error("proxy mode requires --port and --cap-file")
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.unsupported_code:
        return _run_unsupported(args.unsupported_code)
    return _run_proxy(args.port, args.cap_file)


if __name__ == "__main__":
    raise SystemExit(main())
