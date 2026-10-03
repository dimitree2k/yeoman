"""Private, explicit target journal for historical knowledge proofs."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any

from yeoman_gateway.processing.store import SCHEMA_VERSION, ProcessingStore

_MARKER_NAME = ".history-journal.json"
_FORMAT = "yeoman-history-journal-v1"


class HistoryTargetError(ValueError):
    """The requested target is not an isolated, owned history journal."""


class HistoricalJournal:
    """A marked ProcessingStore target with private historical-audience tables."""

    def __init__(
        self,
        target_home: Path,
        *,
        create: bool = True,
        protected_home: Path | None = None,
    ) -> None:
        requested = Path(target_home).expanduser()
        if not requested.is_absolute():
            requested = Path.cwd() / requested
        requested = Path(requested)
        self.target_home = requested
        self._reject_symlinks(requested)
        resolved = requested.resolve(strict=False)
        self._reject_protected_overlap(resolved, protected_home)
        self.target_home = resolved
        marker = self.target_home / _MARKER_NAME
        database = self.target_home / "data" / "processing.db"
        self._reject_symlinks(marker)
        self._reject_symlinks(database)
        for suffix in ("-wal", "-shm", "-journal"):
            self._reject_symlinks(Path(f"{database}{suffix}"))

        if self.target_home.exists() and not self.target_home.is_dir():
            raise HistoryTargetError("target home is not a directory")
        if not self.target_home.exists():
            if not create:
                raise HistoryTargetError("target journal does not exist")
            self.target_home.mkdir(parents=True, mode=0o700)

        marker_data: dict[str, Any] | None = None
        if marker.exists():
            try:
                marker_data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HistoryTargetError("history marker is unreadable") from exc
            if (
                not isinstance(marker_data, dict)
                or marker_data.get("format") != _FORMAT
                or marker_data.get("database") != "data/processing.db"
                or not isinstance(marker_data.get("journal_id"), str)
                or not marker_data["journal_id"].strip()
            ):
                raise HistoryTargetError("history marker is not recognized")
            if not database.is_file():
                raise HistoryTargetError("marked history database is missing")
            self._preflight_marked_database(database, marker_data["journal_id"])
        else:
            if not create:
                raise HistoryTargetError("target is not a marked history journal")
            try:
                contents = tuple(self.target_home.iterdir())
            except OSError as exc:
                raise HistoryTargetError("target home cannot be inspected") from exc
            if contents:
                raise HistoryTargetError("refusing an unmarked non-empty target")

        self.store = ProcessingStore(database)
        if marker_data is None:
            database.parent.chmod(0o700)
        journal_id = (
            str(marker_data["journal_id"]) if marker_data else uuid.uuid4().hex
        )
        try:
            self._initialize_auxiliary_tables(journal_id, marker_data is None)
            if marker_data is None:
                with marker.open("x", encoding="utf-8") as marker_file:
                    marker_file.write(
                        json.dumps(
                            {
                                "format": _FORMAT,
                                "database": "data/processing.db",
                                "journal_id": journal_id,
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
                marker.chmod(0o600)
        except BaseException:
            self.store.close()
            raise

    @staticmethod
    def _reject_symlinks(path: Path) -> None:
        absolute = path.absolute()
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current = current / part
            if current.is_symlink():
                raise HistoryTargetError("history target paths cannot contain symlinks")

    def _reject_protected_overlap(
        self, target: Path, protected_home: Path | None
    ) -> None:
        source_root = Path(__file__).resolve().parents[4]
        protected = {
            Path.home() / ".yeoman",
            Path.home() / "Documents" / "yeoman",
            source_root,
        }
        if protected_home is not None:
            protected.add(Path(protected_home).expanduser())
        for candidate in protected:
            candidate = candidate.resolve(strict=False)
            if (
                target == candidate
                or target.is_relative_to(candidate)
                or candidate.is_relative_to(target)
            ):
                raise HistoryTargetError("history target overlaps a protected source")

    @staticmethod
    def _preflight_marked_database(database: Path, journal_id: str) -> None:
        """Check ownership on a copied database before opening the target writable."""
        try:
            with tempfile.TemporaryDirectory(prefix="yeoman-history-preflight-") as root:
                copy = Path(root) / "processing.db"
                for suffix in ("", "-wal", "-shm", "-journal"):
                    source = Path(f"{database}{suffix}")
                    if source.exists():
                        shutil.copyfile(source, Path(f"{copy}{suffix}"))

                connection = sqlite3.connect(copy)
                try:
                    check = connection.execute("PRAGMA quick_check").fetchone()
                    if check is None or check[0] != "ok":
                        raise HistoryTargetError(
                            "marked history database failed integrity preflight"
                        )
                    tables = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    if not {"meta", "history_meta", "history_audience_proofs"}.issubset(
                        tables
                    ):
                        raise HistoryTargetError(
                            "marked history database lacks owned tables"
                        )
                    store_meta = {
                        str(row[0]): str(row[1])
                        for row in connection.execute(
                            "SELECT key, value FROM meta"
                        ).fetchall()
                    }
                    history_meta = {
                        str(row[0]): str(row[1])
                        for row in connection.execute(
                            "SELECT key, value FROM history_meta"
                        ).fetchall()
                    }
                    if store_meta.get("schema_version") != str(SCHEMA_VERSION):
                        raise HistoryTargetError(
                            "marked history database has an unsupported store schema"
                        )
                    if (
                        history_meta.get("journal_id") != journal_id
                        or history_meta.get("schema_version") != "1"
                    ):
                        raise HistoryTargetError(
                            "history marker does not match its database"
                        )
                    proof_columns = {
                        str(row[1])
                        for row in connection.execute(
                            "PRAGMA table_info(history_audience_proofs)"
                        )
                    }
                    if not {
                        "proof_id",
                        "channel",
                        "account",
                        "chat_id",
                        "status",
                        "evidence_class",
                        "members_json",
                        "source_refs_json",
                        "valid_from_ms",
                        "valid_until_ms",
                        "created_ms",
                        "actor_principal",
                        "authorization_ref",
                        "confirmation",
                        "revoked_ms",
                        "revoked_by",
                        "revocation_authorization_ref",
                    }.issubset(proof_columns):
                        raise HistoryTargetError(
                            "marked history database has an unsupported history schema"
                        )
                    schema_before = connection.execute(
                        "SELECT type, name, sql FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                    ).fetchall()
                finally:
                    connection.close()

                store = ProcessingStore(copy)
                store.close()
                connection = sqlite3.connect(copy)
                try:
                    schema_after = connection.execute(
                        "SELECT type, name, sql FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                    ).fetchall()
                finally:
                    connection.close()
                if schema_before != schema_after:
                    raise HistoryTargetError(
                        "marked history database schema is incomplete"
                    )
        except HistoryTargetError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise HistoryTargetError(
                "marked history database failed ownership preflight"
            ) from exc

    def _initialize_auxiliary_tables(self, journal_id: str, create: bool) -> None:
        with self.store._write() as connection:
            if create:
                connection.execute(
                    "CREATE TABLE history_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO history_meta (key, value) VALUES ('journal_id', ?)",
                    (journal_id,),
                )
                connection.execute(
                    "INSERT INTO history_meta (key, value) VALUES ('schema_version', '1')"
                )
                connection.execute(
                    """
                    CREATE TABLE history_audience_proofs (
                        proof_id TEXT PRIMARY KEY,
                        channel TEXT NOT NULL,
                        account TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('known','author_only')),
                        evidence_class TEXT NOT NULL,
                        members_json TEXT NOT NULL,
                        source_refs_json TEXT NOT NULL,
                        valid_from_ms INTEGER NOT NULL,
                        valid_until_ms INTEGER NOT NULL,
                        created_ms INTEGER NOT NULL,
                        actor_principal TEXT NOT NULL,
                        authorization_ref TEXT NOT NULL,
                        confirmation TEXT NOT NULL,
                        revoked_ms INTEGER,
                        revoked_by TEXT,
                        revocation_authorization_ref TEXT
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX history_audience_scope_period ON history_audience_proofs "
                    "(channel, account, chat_id, valid_from_ms, valid_until_ms)"
                )
                return

            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not {"history_meta", "history_audience_proofs"}.issubset(tables):
                raise HistoryTargetError("marked target lacks history-owned tables")
            meta = dict(
                connection.execute("SELECT key, value FROM history_meta").fetchall()
            )
            if meta.get("journal_id") != journal_id or meta.get("schema_version") != "1":
                raise HistoryTargetError("history marker does not match its database")

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> HistoricalJournal:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
