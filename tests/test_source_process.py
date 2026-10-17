import sys
import time

import pytest

from ehtg.domain.errors import AppError
from ehtg.source.errors import SourceError
from ehtg.source.process import IsolatedProcess


def child(code: str, *, timeout: float = 5, limit: int = 2048) -> IsolatedProcess:
    return IsolatedProcess(
        command=(sys.executable, "-I", "-c", code), timeout=timeout, output_limit=limit
    )


def test_process_does_not_inherit_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EHTG_BOT_TOKEN", "synthetic-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://secret:password@proxy.invalid")
    process = child(
        "import os,json; print(json.dumps({'protocol':1,'ok':True,'environment':dict(os.environ)}))"
    )
    result = process.run({}, lambda: None)
    assert "EHTG_BOT_TOKEN" not in result["environment"]
    assert "HTTPS_PROXY" not in result["environment"]
    assert "synthetic-secret" not in str(result)


def test_process_timeout_is_bounded() -> None:
    process = child("import time; time.sleep(10)", timeout=0.2)
    start = time.monotonic()
    with pytest.raises(SourceError) as error:
        process.run({}, lambda: None)
    assert time.monotonic() - start < 5
    assert error.value.retryable


def test_output_overflow_and_arbitrary_error_text_are_never_returned() -> None:
    with pytest.raises(SourceError) as error:
        child("print('secret' * 10000)", limit=100).run({}, lambda: None)
    assert error.value.code == "RESOURCE_LIMIT"
    assert "secret" not in str(error.value)
    with pytest.raises(SourceError) as error:
        child("print('not json with a secret')").run({}, lambda: None)
    assert "secret" not in str(error.value)


def test_lost_lease_stops_child_promptly() -> None:
    def expired():
        raise AppError("ANALYSIS_LEASE_LOST", "Expired", 409)

    start = time.monotonic()
    with pytest.raises(AppError) as error:
        child("import time; time.sleep(10)").run({}, expired)
    assert error.value.code == "ANALYSIS_LEASE_LOST"
    assert time.monotonic() - start < 5


def test_overflow_is_detected_while_process_is_still_running() -> None:
    process = child("import sys,time; print('x' * 2000, flush=True); time.sleep(10)", limit=100)
    start = time.monotonic()
    with pytest.raises(SourceError) as error:
        process.run({}, lambda: None)
    assert error.value.code == "RESOURCE_LIMIT"
    assert time.monotonic() - start < 5
