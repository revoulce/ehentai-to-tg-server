from concurrent.futures import ThreadPoolExecutor

import pytest

from ehtg.auth import DeviceAuth
from ehtg.domain.errors import AppError
from ehtg.storage.database import Database

ORIGIN = "chrome-extension://" + "a" * 32


def test_pairing_is_single_use_and_hashes_secrets(database: Database) -> None:
    auth = DeviceAuth(database, "123456", (ORIGIN,))
    code = auth.issue_code()
    result = auth.redeem(code, "My browser", ORIGIN)
    assert len(code) >= 22
    assert len(result["token"]) >= 43
    assert auth.authenticate(result["token"], ORIGIN) == result["device_id"]
    with database.read() as connection:
        stored = str([tuple(row) for row in connection.execute("SELECT * FROM pairing_codes")])
        stored += str(
            [tuple(row) for row in connection.execute("SELECT * FROM client_credentials")]
        )
    assert code not in stored
    assert result["token"] not in stored
    assert "token_hash" not in str(auth.devices())
    with pytest.raises(AppError) as error:
        auth.redeem(code, "Second browser", ORIGIN)
    assert error.value.code == "INVALID_PAIRING_CODE"


def test_pairing_expires_after_five_minutes(database: Database) -> None:
    clock = [1000.0]
    auth = DeviceAuth(database, "123456", (ORIGIN,), clock=lambda: clock[0])
    code = auth.issue_code()
    clock[0] += 300
    with pytest.raises(AppError) as error:
        auth.redeem(code, "Expired", ORIGIN)
    assert error.value.code == "INVALID_PAIRING_CODE"


def test_pairing_code_is_consumed_atomically(database: Database) -> None:
    auth = DeviceAuth(database, "123456", (ORIGIN,))
    code = auth.issue_code()

    def redeem(_: int) -> str:
        try:
            return auth.redeem(code, "Browser", ORIGIN)["device_id"]
        except AppError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(redeem, range(4)))
    assert results.count("INVALID_PAIRING_CODE") == 3
    assert len(auth.devices()) == 1


def test_failed_pairing_attempts_are_limited_and_survive_restart(database: Database) -> None:
    auth = DeviceAuth(database, "123456", (ORIGIN,), attempts_per_minute=2, clock=lambda: 1000)
    for _ in range(2):
        with pytest.raises(AppError) as error:
            auth.redeem("wrong", "Browser", ORIGIN)
        assert error.value.status == 401
    restarted = DeviceAuth(database, "123456", (ORIGIN,), attempts_per_minute=2, clock=lambda: 1000)
    code = restarted.issue_code()
    with pytest.raises(AppError) as error:
        restarted.redeem(code, "Browser", ORIGIN)
    assert error.value.status == 429
    later = DeviceAuth(database, "123456", (ORIGIN,), attempts_per_minute=2, clock=lambda: 1060)
    assert later.redeem(code, "Browser", ORIGIN)["token"]


def test_revocation_does_not_affect_other_devices(database: Database) -> None:
    auth = DeviceAuth(database, "123456", (ORIGIN,))
    first = auth.redeem(auth.issue_code(), "First", ORIGIN)
    second = auth.redeem(auth.issue_code(), "Second", ORIGIN)
    auth.revoke(first["device_id"])
    auth.revoke(first["device_id"])
    with pytest.raises(AppError):
        auth.authenticate(first["token"], ORIGIN)
    assert auth.authenticate(second["token"], ORIGIN) == second["device_id"]
    with pytest.raises(AppError):
        auth.authenticate(second["token"], "https://e-hentai.org")


def test_wrong_origin_cannot_consume_valid_code(database: Database) -> None:
    auth = DeviceAuth(database, "123456", (ORIGIN,))
    code = auth.issue_code()
    with pytest.raises(AppError) as error:
        auth.redeem(code, "Website", "https://e-hentai.org")
    assert error.value.status == 403
    assert auth.redeem(code, "Browser", ORIGIN)["token"]
