import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Any

from ehtg.source.errors import SourceError

MAX_OUTPUT_BYTES = 2097152


class IsolatedProcess:
    def __init__(
        self,
        *,
        timeout: float = 900,
        output_limit: int = MAX_OUTPUT_BYTES,
        command: Sequence[str] | None = None,
    ) -> None:
        self.timeout = timeout
        self.output_limit = output_limit
        self.command = (
            tuple(command)
            if command is not None
            else (
                sys.executable,
                "-I",
                "-m",
                "ehtg.source.child",
            )
        )

    def run(self, payload: dict[str, Any], heartbeat: Callable[[], None]) -> dict[str, Any]:
        data = json.dumps({**payload, "parent_pid": os.getpid()}).encode()
        if len(data) > 65536:
            raise SourceError("RESOURCE_LIMIT")
        # Do not inherit API/bot tokens, proxy credentials, Python paths, or downloader settings.
        environment = {
            key: os.environ[key]
            for key in ("SystemRoot", "WINDIR", "TEMP", "TMP")
            if key in os.environ
        }
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                close_fds=True,
                start_new_session=os.name == "posix",
                creationflags=creation_flags,
            )
        except OSError:
            raise SourceError("EXTRACTOR_INCOMPATIBLE") from None
        assert process.stdout is not None and process.stdin is not None
        output = bytearray()
        overflow = threading.Event()

        def read_output() -> None:
            assert process.stdout is not None
            while chunk := os.read(process.stdout.fileno(), 65536):
                if len(output) + len(chunk) > self.output_limit:
                    overflow.set()
                    return
                output.extend(chunk)

        def write_input() -> None:
            assert process.stdin is not None
            try:
                process.stdin.write(data)
            except (BrokenPipeError, OSError):
                pass
            finally:
                with suppress(OSError):
                    process.stdin.close()

        reader = threading.Thread(target=read_output, daemon=True)
        writer = threading.Thread(target=write_input, daemon=True)
        reader.start()
        writer.start()
        deadline = time.monotonic() + self.timeout
        next_heartbeat = 0.0
        try:
            while process.poll() is None:
                now = time.monotonic()
                if overflow.is_set():
                    raise SourceError("RESOURCE_LIMIT")
                if now >= deadline:
                    raise SourceError("SOURCE_NETWORK_UNAVAILABLE")
                if now >= next_heartbeat:
                    heartbeat()
                    next_heartbeat = now + 0.5
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue
            reader.join(timeout=2)
            writer.join(timeout=2)
            if overflow.is_set() or reader.is_alive() or writer.is_alive():
                raise SourceError("RESOURCE_LIMIT")
            if process.returncode != 0:
                if sys.platform != "win32" and process.returncode in {
                    -signal.SIGKILL,
                    -signal.SIGXCPU,
                    -signal.SIGXFSZ,
                }:
                    raise SourceError("RESOURCE_LIMIT")
                raise SourceError("EXTRACTOR_INCOMPATIBLE")
            try:
                result = json.loads(output)
            except (ValueError, UnicodeError):
                raise SourceError("EXTRACTOR_INCOMPATIBLE") from None
            if not isinstance(result, dict) or result.get("protocol") != 1:
                raise SourceError("EXTRACTOR_INCOMPATIBLE")
            if result.get("ok") is not True:
                raise SourceError(str(result.get("code", "EXTRACTOR_INCOMPATIBLE")))
            return result
        finally:
            if process.poll() is None:
                if sys.platform != "win32":
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            reader.join(timeout=2)
            writer.join(timeout=2)
