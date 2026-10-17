import hashlib
import secrets
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from ehtg.domain.errors import AppError
from ehtg.service import utc_now
from ehtg.storage.database import Database


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class DeviceAuth:
    def __init__(
        self,
        database: Database,
        owner_id: str,
        allowed_origins: tuple[str, ...],
        *,
        attempts_per_minute: int = 10,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = database
        self.owner_id = owner_id
        self.allowed_origins = allowed_origins
        self.attempts_per_minute = attempts_per_minute
        self.clock = clock

    def issue_code(self) -> str:
        """Trusted server operation; future bot must first check the numeric owner ID."""
        code = secrets.token_urlsafe(16)
        now = int(self.clock())
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM pairing_codes WHERE expires_at<=? OR consumed_at IS NOT NULL", (now,)
            )
            connection.execute(
                "INSERT INTO pairing_codes VALUES (?,?,?,NULL)",
                (secret_hash(code), self.owner_id, now + 300),
            )
        return code

    def _consume_attempt(self) -> None:
        window = int(self.clock()) // 60
        # A separate committed transaction counts failed exchanges too.
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM pairing_rate_limit WHERE window<?", (window - 1,))
            row = connection.execute(
                "SELECT attempts FROM pairing_rate_limit WHERE window=?", (window,)
            ).fetchone()
            if row is not None and row[0] >= self.attempts_per_minute:
                raise AppError(
                    "RATE_LIMITED", "Слишком много попыток сопряжения.", 429, retryable=True
                )
            connection.execute(
                "INSERT INTO pairing_rate_limit VALUES (?,1) "
                "ON CONFLICT(window) DO UPDATE SET attempts=attempts+1",
                (window,),
            )

    def redeem(self, code: str, name: str, origin: str) -> dict[str, str]:
        self._consume_attempt()
        if origin not in self.allowed_origins:
            raise AppError("ORIGIN_DENIED", "Origin расширения не разрешён.", 403)
        token = secrets.token_urlsafe(32)
        device_id = str(uuid4())
        with self.db.transaction() as connection:
            consumed = connection.execute(
                "UPDATE pairing_codes SET consumed_at=? WHERE code_hash=? AND owner_id=? "
                "AND expires_at>? AND consumed_at IS NULL",
                (int(self.clock()), secret_hash(code), self.owner_id, int(self.clock())),
            ).rowcount
            if consumed != 1:
                raise AppError(
                    "INVALID_PAIRING_CODE", "Код недействителен или уже использован.", 401
                )
            connection.execute(
                "INSERT INTO client_credentials VALUES (?,?,?,?,?,?,NULL)",
                (device_id, self.owner_id, name, origin, secret_hash(token), utc_now()),
            )
        return {"device_id": device_id, "token": token, "token_type": "Bearer"}

    def authenticate(self, token: str, origin: str) -> str:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT id,origin FROM client_credentials WHERE token_hash=? AND owner_id=? "
                "AND revoked_at IS NULL",
                (secret_hash(token), self.owner_id),
            ).fetchone()
        if row is None or row["origin"] != origin or origin not in self.allowed_origins:
            raise AppError("UNAUTHORIZED", "Требуется действующий токен устройства.", 401)
        return str(row["id"])

    def devices(self) -> list[dict[str, Any]]:
        with self.db.read() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id,name,origin,created_at,revoked_at FROM client_credentials "
                    "WHERE owner_id=? ORDER BY created_at,id",
                    (self.owner_id,),
                )
            ]

    def revoke(self, device_id: str) -> None:
        with self.db.transaction() as connection:
            changed = connection.execute(
                "UPDATE client_credentials SET revoked_at=COALESCE(revoked_at,?) "
                "WHERE id=? AND owner_id=?",
                (utc_now(), device_id, self.owner_id),
            ).rowcount
            if changed != 1:
                raise AppError("NOT_FOUND", "Устройство не найдено.", 404)
