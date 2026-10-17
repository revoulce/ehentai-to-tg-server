import json
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ehtg.domain.errors import AppError
from ehtg.source.errors import SourceError
from ehtg.source.parser import inspect_gallery, probe
from ehtg.source.session import SessionCookie
from ehtg.source.transport import SourceGate, SourceTransport, verify_authenticated_home
from ehtg.storage.database import Database


def _limits(request: dict[str, Any]) -> None:
    if sys.platform == "linux":
        import ctypes
        import resource

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise SourceError("RESOURCE_LIMIT")
        if os.getppid() != request["parent_pid"]:
            raise SourceError("RESOURCE_LIMIT")
        memory = int(request.get("memory_mb", 256)) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main() -> None:
    try:
        request_bytes = sys.stdin.buffer.read(65537)
        if len(request_bytes) > 65536:
            raise SourceError("RESOURCE_LIMIT")
        request = json.loads(request_bytes)
        _limits(request)
        operation = request["operation"]
        if operation == "probe":
            result: dict[str, Any] = {"probe": probe()}
        elif operation in {"inspect", "verify"}:
            probe()
            cookies = tuple(SessionCookie(**item) for item in request["cookies"])
            gate = SourceGate(Database(Path(request["database_path"])), request["interval"])
            transport = SourceTransport(
                cookies,
                gate,
                timeout=request["request_timeout"],
                max_bytes=request["max_html_bytes"],
                allow_paused=operation == "verify",
            )
            verify_authenticated_home(transport.get("/home.php"))
            result = (
                {}
                if operation == "verify"
                else {
                    "snapshot": asdict(
                        inspect_gallery(
                            request["source_url"],
                            transport.get,
                            max_pages=request["max_pages"],
                            max_index_pages=request["max_index_pages"],
                        )
                    )
                }
            )
        else:
            raise SourceError("EXTRACTOR_INCOMPATIBLE")
        response = {"protocol": 1, "ok": True, **result}
    except AppError as error:
        response = {"protocol": 1, "ok": False, "code": error.code}
    except MemoryError:
        response = {"protocol": 1, "ok": False, "code": "RESOURCE_LIMIT"}
    except Exception:
        response = {"protocol": 1, "ok": False, "code": "EXTRACTOR_INCOMPATIBLE"}
    output = json.dumps(response, ensure_ascii=True).encode()
    if len(output) > 2097152:
        output = b'{"protocol":1,"ok":false,"code":"RESOURCE_LIMIT"}'
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
