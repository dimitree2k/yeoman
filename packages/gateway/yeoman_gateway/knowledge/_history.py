"""Private, explicit target journal for historical knowledge proofs."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

from yeoman_gateway.knowledge.authority import EvidenceAudience
from yeoman_gateway.knowledge.models import SourceRef
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgeSources
from yeoman_gateway.processing.store import SCHEMA_VERSION, ProcessingStore

_MARKER_NAME = ".history-journal.json"
_FORMAT = "yeoman-history-journal-v1"
_HISTORY_SCHEMA_VERSION = "2"


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
                        or history_meta.get("schema_version") not in {"1", "2"}
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
                    "INSERT INTO history_meta (key, value) VALUES ('schema_version', ?)",
                    (_HISTORY_SCHEMA_VERSION,),
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
                self._create_rebuild_tables(connection)
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
            if meta.get("journal_id") != journal_id or meta.get("schema_version") not in {"1", "2"}:
                raise HistoryTargetError("history marker does not match its database")
            if meta.get("schema_version") == "1":
                self._create_rebuild_tables(connection)
                connection.execute(
                    "UPDATE history_meta SET value = ? WHERE key = 'schema_version'",
                    (_HISTORY_SCHEMA_VERSION,),
                )

    @staticmethod
    def _create_rebuild_tables(connection: sqlite3.Connection) -> None:
        schema = """
            CREATE TABLE IF NOT EXISTS history_event_details (
              event_id TEXT NOT NULL,
              revision TEXT NOT NULL,
              normalized_json TEXT NOT NULL,
              semantic_kind TEXT NOT NULL,
              semantic_direction TEXT NOT NULL,
              provenance_class TEXT NOT NULL,
              retention_status TEXT NOT NULL,
              text_hash TEXT,
              denied INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (event_id, revision)
            );
            CREATE TABLE IF NOT EXISTS history_event_copies (
              copy_id INTEGER PRIMARY KEY,
              event_id TEXT NOT NULL,
              revision TEXT NOT NULL,
              source_id TEXT NOT NULL,
              source_hash TEXT,
              locator_json TEXT NOT NULL,
              source_kind TEXT NOT NULL,
              semantic_kind TEXT NOT NULL,
              semantic_direction TEXT NOT NULL,
              provenance_class TEXT NOT NULL,
              source_authority TEXT,
              channel TEXT,
              account TEXT,
              chat_id TEXT,
              native_id TEXT,
              text_hash TEXT,
              text_value TEXT,
              disposition TEXT NOT NULL,
              copy_json TEXT NOT NULL,
              UNIQUE (event_id, revision, source_id, locator_json)
            );
            CREATE INDEX IF NOT EXISTS history_copies_native
              ON history_event_copies(channel, account, chat_id, native_id);
            CREATE TABLE IF NOT EXISTS history_event_aliases (
              source_event_id TEXT NOT NULL,
              source_revision TEXT NOT NULL,
              canonical_event_id TEXT NOT NULL,
              canonical_revision TEXT NOT NULL,
              source_id TEXT NOT NULL,
              locator_json TEXT NOT NULL,
              status TEXT NOT NULL,
              PRIMARY KEY (source_event_id, source_revision, source_id, locator_json)
            );
            CREATE INDEX IF NOT EXISTS history_alias_target
              ON history_event_aliases(canonical_event_id, canonical_revision);
            CREATE TABLE IF NOT EXISTS history_source_proofs (
              event_id TEXT NOT NULL,
              revision TEXT NOT NULL,
              source_id TEXT NOT NULL,
              locator_json TEXT NOT NULL,
              author_principal TEXT,
              channel TEXT,
              chat_id TEXT,
              occurred_ms INTEGER,
              audience_status TEXT NOT NULL,
              audience_members_json TEXT NOT NULL,
              snapshot_id TEXT,
              policy_revision TEXT,
              revoked_at_ms INTEGER,
              revoking_event_id TEXT,
              eligible INTEGER NOT NULL,
              denial_reason TEXT,
              PRIMARY KEY (event_id, revision, source_id, locator_json)
            );
            CREATE TABLE IF NOT EXISTS history_denials (
              denial_id INTEGER PRIMARY KEY,
              event_id TEXT,
              revision TEXT,
              source_id TEXT NOT NULL,
              locator_json TEXT NOT NULL,
              channel TEXT,
              account TEXT,
              chat_id TEXT,
              native_id TEXT,
              reason TEXT NOT NULL,
              UNIQUE (event_id, revision, source_id, locator_json, reason)
            );
            CREATE TABLE IF NOT EXISTS history_unresolved_refs (
              unresolved_id INTEGER PRIMARY KEY,
              event_id TEXT NOT NULL,
              revision TEXT NOT NULL,
              source_id TEXT NOT NULL,
              locator_json TEXT NOT NULL,
              target_json TEXT NOT NULL,
              reason TEXT NOT NULL,
              UNIQUE (event_id, revision, source_id, locator_json, target_json, reason)
            );
            CREATE TABLE IF NOT EXISTS history_name_observations (
              observation_id INTEGER PRIMARY KEY,
              event_id TEXT NOT NULL,
              revision TEXT NOT NULL,
              source_id TEXT NOT NULL,
              locator_json TEXT NOT NULL,
              occurred_ms INTEGER,
              observed_ms INTEGER,
              time_certainty TEXT NOT NULL,
              raw_identifier TEXT,
              name TEXT NOT NULL,
              channel TEXT,
              account TEXT,
              chat_id TEXT,
              provenance_class TEXT NOT NULL,
              UNIQUE (event_id, revision, source_id, locator_json, name)
            );
            """
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(statement)

    def write_rebuild_records(
        self,
        *,
        event_details: list[dict[str, Any]],
        copies: list[dict[str, Any]],
        aliases: list[dict[str, Any]],
        source_proofs: list[dict[str, Any]],
        denials: list[dict[str, Any]],
        unresolved_refs: list[dict[str, Any]],
        name_observations: list[dict[str, Any]],
        report: dict[str, Any],
        finalize_report: Callable[[sqlite3.Connection, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Write rebuild-only lineage and set completion inside one target transaction."""
        statements = {
            "history_event_details": (
              "INSERT OR IGNORE INTO history_event_details VALUES (?,?,?,?,?,?,?,?,?)",
                ("event_id", "revision", "normalized_json", "semantic_kind", "semantic_direction", "provenance_class", "retention_status", "text_hash", "denied"),
            ),
            "history_event_copies": (
                "INSERT OR IGNORE INTO history_event_copies (event_id,revision,source_id,source_hash,locator_json,source_kind,semantic_kind,semantic_direction,provenance_class,source_authority,channel,account,chat_id,native_id,text_hash,text_value,disposition,copy_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("event_id", "revision", "source_id", "source_hash", "locator_json", "source_kind", "semantic_kind", "semantic_direction", "provenance_class", "source_authority", "channel", "account", "chat_id", "native_id", "text_hash", "text_value", "disposition", "copy_json"),
            ),
            "history_event_aliases": (
                "INSERT OR IGNORE INTO history_event_aliases VALUES (?,?,?,?,?,?,?)",
                ("source_event_id", "source_revision", "canonical_event_id", "canonical_revision", "source_id", "locator_json", "status"),
            ),
            "history_source_proofs": (
                "INSERT OR REPLACE INTO history_source_proofs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("event_id", "revision", "source_id", "locator_json", "author_principal", "channel", "chat_id", "occurred_ms", "audience_status", "audience_members_json", "snapshot_id", "policy_revision", "revoked_at_ms", "revoking_event_id", "eligible", "denial_reason"),
            ),
            "history_denials": (
                "INSERT OR IGNORE INTO history_denials (event_id,revision,source_id,locator_json,channel,account,chat_id,native_id,reason) VALUES (?,?,?,?,?,?,?,?,?)",
                ("event_id", "revision", "source_id", "locator_json", "channel", "account", "chat_id", "native_id", "reason"),
            ),
            "history_unresolved_refs": (
                "INSERT OR IGNORE INTO history_unresolved_refs (event_id,revision,source_id,locator_json,target_json,reason) VALUES (?,?,?,?,?,?)",
                ("event_id", "revision", "source_id", "locator_json", "target_json", "reason"),
            ),
            "history_name_observations": (
                "INSERT OR IGNORE INTO history_name_observations (event_id,revision,source_id,locator_json,occurred_ms,observed_ms,time_certainty,raw_identifier,name,channel,account,chat_id,provenance_class) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("event_id", "revision", "source_id", "locator_json", "occurred_ms", "observed_ms", "time_certainty", "raw_identifier", "name", "channel", "account", "chat_id", "provenance_class"),
            ),
        }
        records = {
            "history_event_details": event_details,
            "history_event_copies": copies,
            "history_event_aliases": aliases,
            "history_source_proofs": source_proofs,
            "history_denials": denials,
            "history_unresolved_refs": unresolved_refs,
            "history_name_observations": name_observations,
        }
        with self.store._write() as connection:
            for table, rows in records.items():
                sql, columns = statements[table]
                if rows:
                    connection.executemany(
                        sql,
                        [tuple(row.get(column) for column in columns) for row in rows],
                    )
            if finalize_report is not None:
                report = finalize_report(connection, report)
            connection.execute(
                "INSERT OR REPLACE INTO history_meta (key,value) VALUES ('build_status','complete')"
            )
            connection.execute(
                "INSERT OR REPLACE INTO history_meta (key,value) VALUES ('build_report_json',?)",
                (json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")),),
            )
        return report

    def rebuild_state(self) -> dict[str, Any]:
        with self.store._lock:
            meta = dict(self.store._conn.execute("SELECT key,value FROM history_meta").fetchall())
        report: dict[str, Any] = {}
        try:
            decoded = json.loads(meta.get("build_report_json", "{}"))
            if isinstance(decoded, dict):
                report = decoded
        except json.JSONDecodeError:
            pass
        return {"complete": meta.get("build_status") == "complete", **report}

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> HistoricalJournal:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class HistorySourceAuthority(RuntimeKnowledgeSources):
    """Source proof adapter whose permissions come only from rebuilt proof rows."""

    def __init__(self, journal: HistoricalJournal) -> None:
        super().__init__(processing_store=journal.store)
        self.journal = journal

    def _proofs(self, event_id: str, revision: int) -> list[sqlite3.Row]:
        with self.journal.store._lock:
            return self.journal.store._conn.execute(
                "SELECT * FROM history_source_proofs WHERE event_id=? AND revision=?",
                (str(event_id), str(revision)),
            ).fetchall()

    def _usable_source(self, event_id: str, revision: int) -> SourceRef | None:
        with self.journal.store._lock:
            aliases = self.journal.store._conn.execute(
                "SELECT DISTINCT a.canonical_event_id,a.canonical_revision,a.source_id,"
                "a.locator_json,"
                "c.channel,c.account,c.chat_id,c.native_id "
                "FROM history_event_aliases a LEFT JOIN history_event_copies c "
                "ON c.event_id=a.canonical_event_id AND c.revision=a.canonical_revision "
                "AND c.source_id=a.source_id AND c.locator_json=a.locator_json "
                "WHERE a.source_event_id=? AND a.source_revision=?",
                (str(event_id), str(revision)),
            ).fetchall()
        if aliases:
            targets = {
                (str(row["canonical_event_id"]), str(row["canonical_revision"]))
                for row in aliases
            }
            scopes = {
                (row["channel"], row["account"], row["chat_id"], row["native_id"])
                for row in aliases
            }
            if len(targets) != 1 or len(scopes) != 1:
                return None
            target_id, target_revision = next(iter(targets))
            channel, account, chat_id, native_id = next(iter(scopes))
            alias_refs = {
                (str(row["source_id"]), str(row["locator_json"])) for row in aliases
            }
        else:
            target_id, target_revision = str(event_id), str(revision)
            alias_refs = set()
            with self.journal.store._lock:
                direct_scopes = self.journal.store._conn.execute(
                    "SELECT DISTINCT channel,account,chat_id,native_id "
                    "FROM history_event_copies "
                    "WHERE event_id=? AND revision=?",
                    (target_id, target_revision),
                ).fetchall()
            scopes = {
                (row["channel"], row["account"], row["chat_id"], row["native_id"])
                for row in direct_scopes
            }
            if len(scopes) != 1:
                return None
            channel, account, chat_id, native_id = next(iter(scopes))
        if not all(
            isinstance(item, str) and item
            for item in (channel, account, chat_id, native_id)
        ):
            return None
        rows = self._proofs(event_id, revision)
        if not rows or any(not int(row["eligible"]) or row["revoked_at_ms"] is not None for row in rows):
            return None
        proven_refs = {
            (str(row["source_id"]), str(row["locator_json"])) for row in rows
        }
        if alias_refs and not alias_refs.intersection(proven_refs):
            return None
        identities = {
            (row["author_principal"], row["channel"], row["chat_id"], row["occurred_ms"])
            for row in rows
        }
        if len(identities) != 1:
            return None
        author, channel, chat_id, occurred = next(iter(identities))
        if not author or not channel or not chat_id or occurred in (None, 0):
            return None
        current = self.journal.store.get_event_source_authority(
            target_id, int(target_revision)
        )
        if (
            current is None
            or current.get("revoked_at_ms") is not None
            or current.get("author_principal") != author
            or current.get("source_channel") != channel
            or current.get("source_chat_id") != chat_id
            or current.get("occurred_at_ms") != occurred
        ):
            return None
        scope = (str(channel), str(account), str(chat_id))
        if self.current_retention_denied(
            target_id, int(target_revision), scope=scope, native_id=native_id
        ):
            return None
        try:
            return SourceRef(event_id, int(revision), str(channel), str(chat_id), str(author), int(occurred))
        except (TypeError, ValueError):
            return None

    def verify_source_ref(self, event_id: str, revision: int) -> SourceRef | None:
        return self._usable_source(str(event_id), int(revision))

    def verify_source(self, source: SourceRef) -> bool:
        return self._usable_source(source.event_id, source.revision) == source

    def _source_scope(
        self, event_id: str, revision: int
    ) -> tuple[str, str, str] | None:
        with self.journal.store._lock:
            rows = self.journal.store._conn.execute(
                "SELECT DISTINCT c.channel,c.account,c.chat_id "
                "FROM history_event_aliases a JOIN history_event_copies c "
                "ON c.event_id=a.canonical_event_id AND c.revision=a.canonical_revision "
                "AND c.source_id=a.source_id AND c.locator_json=a.locator_json "
                "WHERE a.source_event_id=? AND a.source_revision=? "
                "AND c.disposition<>'denied'",
                (str(event_id), str(revision)),
            ).fetchall()
            if not rows:
                rows = self.journal.store._conn.execute(
                    "SELECT DISTINCT channel,account,chat_id FROM history_event_copies "
                    "WHERE event_id=? AND revision=? AND disposition<>'denied'",
                    (str(event_id), str(revision)),
                ).fetchall()
        scopes = {
            (row["channel"], row["account"], row["chat_id"]) for row in rows
        }
        if len(scopes) != 1:
            return None
        scope = next(iter(scopes))
        return scope if all(isinstance(item, str) and item for item in scope) else None

    def _compatible_source_keys(
        self,
        event_id: str,
        revision: int,
        *,
        scope: tuple[str, str, str],
        native_id: str | None = None,
    ) -> tuple[set[tuple[str, int]], str | None] | None:
        """Expand only aliases sharing the exact transport-native identity."""
        seed = (str(event_id), revision)
        with self.journal.store._lock:
            connection = self.journal.store._conn
            identities = connection.execute(
                "SELECT DISTINCT c.native_id FROM history_event_copies c "
                "WHERE c.event_id=? AND c.revision=? AND c.channel=? AND c.account=? "
                "AND c.chat_id=? AND c.disposition<>'denied' "
                "UNION SELECT DISTINCT c.native_id FROM history_event_aliases a "
                "JOIN history_event_copies c ON c.event_id=a.canonical_event_id "
                "AND c.revision=a.canonical_revision AND c.source_id=a.source_id "
                "AND c.locator_json=a.locator_json "
                "WHERE a.source_event_id=? AND a.source_revision=? AND c.channel=? "
                "AND c.account=? AND c.chat_id=? AND c.disposition<>'denied'",
                (seed[0], str(revision), *scope, seed[0], str(revision), *scope),
            ).fetchall()
            native_ids = {
                str(row["native_id"])
                for row in identities
                if isinstance(row["native_id"], str) and row["native_id"]
            }
            if native_id is not None and (
                not isinstance(native_id, str)
                or not native_id
                or (native_ids and native_id not in native_ids)
            ):
                return None
            if native_id is None and len(native_ids) > 1:
                return None
            native_id = native_id or next(iter(native_ids), None)
            keys = {seed}
            if native_id is None:
                return keys, None
            pending = [seed]
            while pending:
                current_id, current_revision = pending.pop()
                rows = connection.execute(
                    "SELECT DISTINCT a.source_event_id,a.source_revision,"
                    "a.canonical_event_id,a.canonical_revision "
                    "FROM history_event_aliases a JOIN history_event_copies c "
                    "ON c.event_id=a.canonical_event_id AND c.revision=a.canonical_revision "
                    "AND c.source_id=a.source_id AND c.locator_json=a.locator_json "
                    "WHERE ((a.source_event_id=? AND a.source_revision=?) OR "
                    "(a.canonical_event_id=? AND a.canonical_revision=?)) "
                    "AND c.channel=? AND c.account=? AND c.chat_id=? AND c.native_id=? "
                    "AND c.disposition<>'denied'",
                    (
                        current_id,
                        str(current_revision),
                        current_id,
                        str(current_revision),
                        *scope,
                        native_id,
                    ),
                ).fetchall()
                for row in rows:
                    try:
                        related = {
                            (str(row["source_event_id"]), int(row["source_revision"])),
                            (str(row["canonical_event_id"]), int(row["canonical_revision"])),
                        }
                    except (TypeError, ValueError):
                        return None
                    for key in related - keys:
                        keys.add(key)
                        pending.append(key)
            return keys, native_id

    def _event_matches_native_identity(
        self,
        event_id: str,
        revision: int,
        *,
        scope: tuple[str, str, str],
        native_id: str,
    ) -> bool:
        with self.journal.store._lock:
            connection = self.journal.store._conn
            row = connection.execute(
                "SELECT source_message_id FROM events WHERE event_id=? AND revision=? "
                "AND channel=? AND account=? AND chat_id=?",
                (event_id, revision, *scope),
            ).fetchone()
            if row is None:
                return False
            current_native = row["source_message_id"]
            if current_native == native_id:
                return True
            if current_native != event_id:
                return False
            aliases = connection.execute(
                "SELECT DISTINCT c.native_id FROM history_event_aliases a "
                "JOIN history_event_copies c ON c.event_id=a.canonical_event_id "
                "AND c.revision=a.canonical_revision AND c.source_id=a.source_id "
                "AND c.locator_json=a.locator_json "
                "WHERE a.source_event_id=? AND a.source_revision=? "
                "AND c.channel=? AND c.account=? AND c.chat_id=? "
                "AND c.disposition<>'denied'",
                (event_id, str(revision), *scope),
            ).fetchall()
        identities = {
            str(alias["native_id"])
            for alias in aliases
            if isinstance(alias["native_id"], str) and alias["native_id"]
        }
        return identities == {native_id}

    def current_retention_denied(
        self,
        event_id: str,
        revision: int,
        *,
        scope: tuple[str, str, str],
        native_id: str | None = None,
    ) -> bool:
        """Check current canonical and compatible alias purge markers."""
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or len(scope) != 3
            or any(not isinstance(item, str) or not item for item in scope)
        ):
            return True
        compatible = self._compatible_source_keys(
            event_id, revision, scope=scope, native_id=native_id
        )
        if compatible is None:
            return True
        keys, native_id = compatible
        with self.journal.store._lock:
            connection = self.journal.store._conn
            for current_id, current_revision in keys:
                sql = (
                    "SELECT 1 FROM events WHERE event_id=? AND revision=? "
                    "AND channel=? AND account=? AND chat_id=? "
                    "AND payload_purged_ms IS NOT NULL"
                )
                params: tuple[object, ...] = (current_id, current_revision, *scope)
                if connection.execute(sql + " LIMIT 1", params).fetchone():
                    if native_id is None or self._event_matches_native_identity(
                        current_id,
                        current_revision,
                        scope=scope,
                        native_id=native_id,
                    ):
                        return True
        return False

    def current_source_denied(
        self,
        event_id: str,
        revision: int,
        *,
        scope: tuple[str, str, str],
        native_id: str | None = None,
    ) -> bool:
        """Check current retention and revocation negatives over exact aliases."""
        compatible = self._compatible_source_keys(
            event_id, revision, scope=scope, native_id=native_id
        )
        if compatible is None:
            return True
        keys, native_id = compatible
        if native_id is None or self.current_retention_denied(
            event_id, revision, scope=scope, native_id=native_id
        ):
            return True
        with self.journal.store._lock:
            for current_id, current_revision in keys:
                if not self._event_matches_native_identity(
                    current_id,
                    current_revision,
                    scope=scope,
                    native_id=native_id,
                ):
                    continue
                current = self.journal.store._conn.execute(
                    "SELECT revoked_at_ms FROM event_source_authority "
                    "WHERE event_id=? AND revision=?",
                    (current_id, current_revision),
                ).fetchone()
                if current is not None and current["revoked_at_ms"] is not None:
                    return True
                if self.journal.store._conn.execute(
                    "SELECT 1 FROM history_denials WHERE event_id=? AND revision=? LIMIT 1",
                    (current_id, str(current_revision)),
                ).fetchone():
                    return True
        return False

    def source_revoked(self, source: SourceRef) -> bool:
        proofs = self._proofs(source.event_id, source.revision)
        scope = self._source_scope(source.event_id, source.revision)
        if scope is None or self.current_source_denied(
            source.event_id, source.revision, scope=scope
        ):
            return True
        return any(row["revoked_at_ms"] is not None for row in proofs)

    def evidence_audience(self, source: SourceRef, *, basis: str) -> EvidenceAudience | None:
        del basis
        if not self.verify_source(source):
            return None
        with self.journal.store._lock:
            aliases = self.journal.store._conn.execute(
                "SELECT DISTINCT canonical_event_id,canonical_revision "
                "FROM history_event_aliases WHERE source_event_id=? AND source_revision=?",
                (source.event_id, str(source.revision)),
            ).fetchall()
            details: sqlite3.Row | None = None
            if aliases:
                targets = {
                    (str(row["canonical_event_id"]), str(row["canonical_revision"]))
                    for row in aliases
                }
                if len(targets) != 1:
                    return None
                canonical_id, canonical_revision = next(iter(targets))
            else:
                canonical_id, canonical_revision = source.event_id, str(source.revision)
            details = self.journal.store._conn.execute(
                "SELECT normalized_json FROM history_event_details "
                "WHERE event_id=? AND revision=?",
                (canonical_id, canonical_revision),
            ).fetchone()
        if details is None:
            return None
        try:
            event = json.loads(str(details["normalized_json"]))
        except (TypeError, json.JSONDecodeError):
            return EvidenceAudience.unknown()
        if not isinstance(event, dict):
            return EvidenceAudience.unknown()
        from ._history_audience import HistoryAudience

        proof = HistoryAudience(self.journal).resolve(event)
        if proof.status == "author_only":
            return EvidenceAudience.author_only(snapshot_id=proof.proof_id)
        if proof.status == "known" and proof.members:
            return EvidenceAudience.known(proof.members, snapshot_id=proof.proof_id)
        return EvidenceAudience.unknown()
