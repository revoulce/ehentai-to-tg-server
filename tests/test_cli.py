import json
import re
from pathlib import Path

import pytest

from ehtg.cli import main
from ehtg.storage.database import Database


def test_cli_migrate_pair_backup_and_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EHTG_OWNER_ID", "123456")
    monkeypatch.setenv("EHTG_CHANNEL_ID", "-1001234567890")
    monkeypatch.setenv("EHTG_DATABASE_PATH", str(tmp_path / "cli.sqlite3"))
    monkeypatch.setattr("sys.argv", ["ehtg", "migrate"])
    main()
    monkeypatch.setattr("sys.argv", ["ehtg", "pair"])
    main()
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}\n", capsys.readouterr().out)
    backup_path = tmp_path / "backup.sqlite3"
    monkeypatch.setattr("sys.argv", ["ehtg", "backup", str(backup_path)])
    main()
    with Database(backup_path).read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM pairing_codes").fetchone()[0] == 1
    openapi_path = tmp_path / "openapi.json"
    monkeypatch.setattr("sys.argv", ["ehtg", "export-openapi", str(openapi_path)])
    main()
    contract = json.loads(openapi_path.read_text(encoding="utf-8"))
    assert contract["paths"]["/api/v1/drafts/{draft_id}/publish"]["post"]["security"]
    assert "DraftResponse" in contract["components"]["schemas"]
