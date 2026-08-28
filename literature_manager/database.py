from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .config import DEFAULT_DB_PATH


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """SQLite access layer with a non-destructive migration from the old CLI schema."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
        name = definition.split()[0]
        if name not in Database._columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def migrate(self) -> None:
        with self.transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS papers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    filepath TEXT NOT NULL UNIQUE,
                    title TEXT,
                    authors TEXT,
                    journal TEXT,
                    year TEXT,
                    doi TEXT,
                    keywords_auto TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL,
                    tag_name TEXT NOT NULL,
                    tag_type TEXT DEFAULT 'custom',
                    UNIQUE(paper_id, tag_name),
                    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(tag_name);
                """
            )
            additions = [
                "abstract TEXT DEFAULT ''",
                "publication_date TEXT DEFAULT ''",
                "status TEXT DEFAULT 'review'",
                "confidence REAL DEFAULT 0",
                "metadata_source TEXT DEFAULT 'legacy'",
                "updated_at TEXT DEFAULT ''",
                "is_verified INTEGER DEFAULT 0",
                "notes TEXT DEFAULT ''",
            ]
            for definition in additions:
                self._add_column(conn, "papers", definition)

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    size INTEGER DEFAULT 0,
                    mtime_ns INTEGER DEFAULT 0,
                    sha256 TEXT DEFAULT '',
                    full_text TEXT DEFAULT '',
                    page_count INTEGER DEFAULT 0,
                    has_text INTEGER DEFAULT 1,
                    exists_flag INTEGER DEFAULT 1,
                    scan_root TEXT DEFAULT '',
                    last_seen_at TEXT DEFAULT '',
                    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_paper_files_paper ON paper_files(paper_id);
                CREATE INDEX IF NOT EXISTS idx_paper_files_hash ON paper_files(sha256);

                CREATE TABLE IF NOT EXISTS scan_roots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    last_scanned_at TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    root_path TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT DEFAULT '',
                    added INTEGER DEFAULT 0,
                    updated INTEGER DEFAULT 0,
                    skipped INTEGER DEFAULT 0,
                    missing INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    message TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
                CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);
                CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);
                """
            )
            self._add_column(conn, "paper_files", "error_message TEXT DEFAULT ''")

            # Convert each legacy filepath into a normalized location once.
            legacy_rows = conn.execute(
                "SELECT id, filename, filepath FROM papers WHERE filepath IS NOT NULL"
            ).fetchall()
            for row in legacy_rows:
                path = row["filepath"]
                if not path or path.startswith("record://"):
                    continue
                p = Path(path)
                exists = int(p.exists())
                try:
                    stat = p.stat()
                    size, mtime_ns = stat.st_size, stat.st_mtime_ns
                except OSError:
                    size, mtime_ns = 0, 0
                conn.execute(
                    """
                    INSERT OR IGNORE INTO paper_files
                        (paper_id, path, filename, size, mtime_ns, exists_flag, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (row["id"], path, row["filename"] or p.name, size, mtime_ns, exists, utc_now()),
                )

    def recent_roots(self) -> list[str]:
        with self.connect() as conn:
            return [
                row["path"]
                for row in conn.execute(
                    "SELECT path FROM scan_roots WHERE enabled=1 ORDER BY last_scanned_at DESC, id DESC"
                )
            ]

    def get_paper(self, paper_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["tags"] = [
                r["tag_name"]
                for r in conn.execute(
                    "SELECT tag_name FROM tags WHERE paper_id=? AND tag_type='custom' ORDER BY tag_name",
                    (paper_id,),
                )
            ]
            result["files"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM paper_files WHERE paper_id=? ORDER BY exists_flag DESC, path",
                    (paper_id,),
                )
            ]
            return result

    def update_paper(self, paper_id: int, values: dict, tags: Sequence[str]) -> None:
        allowed = {
            "title", "authors", "doi", "journal", "year", "publication_date",
            "keywords_auto", "abstract", "notes", "status", "is_verified",
        }
        clean = {key: value for key, value in values.items() if key in allowed}
        clean["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in clean)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE papers SET {assignments} WHERE id=?",
                (*clean.values(), paper_id),
            )
            conn.execute("DELETE FROM tags WHERE paper_id=? AND tag_type='custom'", (paper_id,))
            for tag in dict.fromkeys(tag.strip() for tag in tags if tag.strip()):
                conn.execute(
                    "INSERT OR IGNORE INTO tags(paper_id, tag_name, tag_type) VALUES (?, ?, 'custom')",
                    (paper_id, tag),
                )

    def all_paper_ids(self) -> list[int]:
        with self.connect() as conn:
            return [row[0] for row in conn.execute("SELECT id FROM papers ORDER BY id")]

    def insert_imported(self, values: dict, tags: Iterable[str] = ()) -> tuple[int, bool]:
        doi = (values.get("doi") or "").strip().lower()
        title = (values.get("title") or "").strip()
        year = str(values.get("year") or "").strip()
        with self.transaction() as conn:
            existing = None
            if doi:
                existing = conn.execute("SELECT id FROM papers WHERE lower(doi)=?", (doi,)).fetchone()
            if not existing and title:
                existing = conn.execute(
                    "SELECT id FROM papers WHERE lower(trim(title))=lower(trim(?)) AND coalesce(year,'')=?",
                    (title, year),
                ).fetchone()
            if existing:
                paper_id, created = existing["id"], False
            else:
                import uuid

                virtual_path = f"record://{uuid.uuid4()}"
                cur = conn.execute(
                    """
                    INSERT INTO papers
                        (filename, filepath, title, authors, journal, year, doi, keywords_auto,
                         abstract, publication_date, status, confidence, metadata_source,
                         updated_at, is_verified, notes)
                    VALUES ('', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'review', 0.8, 'import', ?, 0, ?)
                    """,
                    (
                        virtual_path, title, values.get("authors", ""), values.get("journal", ""),
                        year, values.get("doi", ""), values.get("keywords_auto", ""),
                        values.get("abstract", ""), values.get("publication_date", ""),
                        utc_now(), values.get("notes", ""),
                    ),
                )
                paper_id, created = cur.lastrowid, True
            for tag in dict.fromkeys(str(t).strip() for t in tags if str(t).strip()):
                conn.execute(
                    "INSERT OR IGNORE INTO tags(paper_id, tag_name, tag_type) VALUES (?, ?, 'custom')",
                    (paper_id, tag),
                )
            return paper_id, created
