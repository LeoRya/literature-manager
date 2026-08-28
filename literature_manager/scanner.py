from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .database import Database, utc_now
from .extractor import ExtractedDocument, clean_doi, extract_pdf
from .metadata import OnlineMetadata, lookup_metadata


ProgressCallback = Callable[[str, int, int], None]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (value or "").lower())


def _merge(local: ExtractedDocument, online: OnlineMetadata | None) -> dict:
    if online:
        keywords = list(dict.fromkeys([*online.keywords, *local.keywords]))
        return {
            "title": online.title or local.title,
            "authors": "; ".join(online.authors or local.authors),
            "doi": online.doi or local.doi,
            "journal": online.journal or local.journal,
            "year": online.year or local.year,
            "publication_date": online.publication_date or local.publication_date,
            "keywords_auto": "; ".join(keywords),
            "abstract": online.abstract or local.abstract,
            "confidence": online.confidence,
            "metadata_source": online.source,
            "status": "ready" if local.has_text else "review",
        }
    return {
        "title": local.title,
        "authors": "; ".join(local.authors),
        "doi": local.doi,
        "journal": local.journal,
        "year": local.year,
        "publication_date": local.publication_date,
        "keywords_auto": "; ".join(local.keywords),
        "abstract": local.abstract,
        "confidence": local.confidence,
        "metadata_source": "PDF",
        "status": "review",
    }


class LibraryScanner:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def discover(root: str | Path) -> list[Path]:
        root = Path(root).expanduser().resolve()
        return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf")

    def scan(
        self,
        root: str | Path,
        online: bool = True,
        progress: ProgressCallback | None = None,
    ) -> dict[str, int | str]:
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            raise ValueError(f"文件夹不存在：{root_path}")
        pdfs = self.discover(root_path)
        summary: dict[str, int | str] = {
            "root": str(root_path), "total": len(pdfs), "added": 0, "updated": 0,
            "skipped": 0, "missing": 0, "failed": 0,
        }
        started = utc_now()
        with self.database.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO scan_runs(root_path, started_at) VALUES (?, ?)",
                (str(root_path), started),
            )
            run_id = cursor.lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO scan_roots(path, last_scanned_at) VALUES (?, '')",
                (str(root_path),),
            )

        seen: set[str] = set()
        errors: list[str] = []
        for index, path in enumerate(pdfs, start=1):
            path_string = str(path)
            seen.add(path_string)
            stat = None
            digest = ""
            if progress:
                progress(path.name, index, len(pdfs))
            try:
                stat = path.stat()
                with self.database.connect() as conn:
                    existing_file = conn.execute(
                        "SELECT * FROM paper_files WHERE path=?", (path_string,)
                    ).fetchone()
                if (
                    existing_file
                    and existing_file["size"] == stat.st_size
                    and existing_file["mtime_ns"] == stat.st_mtime_ns
                    and existing_file["exists_flag"] == 1
                    and existing_file["sha256"]
                ):
                    with self.database.transaction() as conn:
                        conn.execute(
                            "UPDATE paper_files SET last_seen_at=?, scan_root=? WHERE id=?",
                            (utc_now(), str(root_path), existing_file["id"]),
                        )
                    summary["skipped"] = int(summary["skipped"]) + 1
                    continue

                digest = file_sha256(path)
                with self.database.connect() as conn:
                    same_hash = conn.execute(
                        "SELECT * FROM paper_files WHERE sha256=? ORDER BY exists_flag DESC LIMIT 1",
                        (digest,),
                    ).fetchone()
                if same_hash and (not existing_file or same_hash["paper_id"] != existing_file["paper_id"]):
                    self._save_file_location(
                        same_hash["paper_id"], path, stat.st_size, stat.st_mtime_ns, digest,
                        "", same_hash["page_count"], same_hash["has_text"], root_path,
                    )
                    summary["added"] = int(summary["added"]) + 1
                    continue

                extracted = extract_pdf(path)
                online_metadata = lookup_metadata(extracted.doi, extracted.title) if online else None
                values = _merge(extracted, online_metadata)
                # A known path always belongs to its existing record. This is especially
                # important when migrating the legacy database, whose files have no hash yet.
                paper_id = existing_file["paper_id"] if existing_file else self._match_paper(values, digest)
                created = paper_id is None
                if created:
                    paper_id = self._insert_paper(path, values)
                else:
                    self._update_automatic_metadata(paper_id, path, values)
                self._save_file_location(
                    paper_id, path, stat.st_size, stat.st_mtime_ns, digest,
                    extracted.full_text, extracted.page_count, extracted.has_text, root_path,
                )
                self._sync_auto_tags(paper_id, values)
                summary["added" if created else "updated"] = int(summary["added" if created else "updated"]) + 1
            except Exception as exc:
                summary["failed"] = int(summary["failed"]) + 1
                errors.append(f"{path.name}: {exc}")
                try:
                    self._record_failure(path, root_path, str(exc), stat, digest)
                except Exception as record_exc:
                    errors.append(f"{path.name}（失败记录也未保存）: {record_exc}")

        with self.database.transaction() as conn:
            rows = conn.execute(
                "SELECT id, path FROM paper_files WHERE scan_root=? AND exists_flag=1",
                (str(root_path),),
            ).fetchall()
            for row in rows:
                if row["path"] not in seen:
                    conn.execute("UPDATE paper_files SET exists_flag=0 WHERE id=?", (row["id"],))
                    summary["missing"] = int(summary["missing"]) + 1
            finished = utc_now()
            conn.execute(
                "UPDATE scan_roots SET last_scanned_at=? WHERE path=?",
                (finished, str(root_path)),
            )
            conn.execute(
                """
                UPDATE scan_runs SET finished_at=?, added=?, updated=?, skipped=?, missing=?, failed=?, message=?
                WHERE id=?
                """,
                (
                    finished, summary["added"], summary["updated"], summary["skipped"],
                    summary["missing"], summary["failed"], "\n".join(errors[:30]), run_id,
                ),
            )
        summary["errors"] = "\n".join(errors)
        return summary

    def _match_paper(self, values: dict, digest: str) -> int | None:
        with self.database.connect() as conn:
            doi = clean_doi(values.get("doi"))
            if doi:
                row = conn.execute(
                    "SELECT id FROM papers WHERE lower(doi)=? ORDER BY is_verified DESC LIMIT 1", (doi,)
                ).fetchone()
                if row:
                    return row["id"]
            row = conn.execute(
                "SELECT paper_id FROM paper_files WHERE sha256=? LIMIT 1", (digest,)
            ).fetchone()
            if row:
                return row["paper_id"]
            normalized = normalize_title(values.get("title", ""))
            year = str(values.get("year") or "")
            if normalized and len(normalized) >= 12:
                for row in conn.execute("SELECT id, title, year FROM papers WHERE coalesce(year,'')=?", (year,)):
                    if normalize_title(row["title"] or "") == normalized:
                        return row["id"]
        return None

    def _insert_paper(self, path: Path, values: dict) -> int:
        with self.database.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO papers
                    (filename, filepath, title, authors, journal, year, doi, keywords_auto,
                     abstract, publication_date, status, confidence, metadata_source,
                     updated_at, is_verified, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '')
                """,
                (
                    path.name, str(path), values["title"] or path.stem, values["authors"],
                    values["journal"], values["year"], clean_doi(values["doi"]),
                    values["keywords_auto"], values["abstract"], values["publication_date"],
                    values["status"], values["confidence"], values["metadata_source"], utc_now(),
                ),
            )
            return cur.lastrowid

    def _update_automatic_metadata(self, paper_id: int, path: Path, values: dict) -> None:
        with self.database.transaction() as conn:
            row = conn.execute("SELECT is_verified, filepath FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not row:
                return
            if not row["is_verified"]:
                conn.execute(
                    """
                    UPDATE papers SET title=?, authors=?, journal=?, year=?, doi=?, keywords_auto=?,
                        abstract=?, publication_date=?, status=?, confidence=?, metadata_source=?,
                        filename=?, filepath=?, updated_at=? WHERE id=?
                    """,
                    (
                        values["title"] or path.stem, values["authors"], values["journal"], values["year"],
                        clean_doi(values["doi"]), values["keywords_auto"], values["abstract"],
                        values["publication_date"], values["status"], values["confidence"],
                        values["metadata_source"], path.name, str(path), utc_now(), paper_id,
                    ),
                )

    def _save_file_location(
        self, paper_id: int, path: Path, size: int, mtime_ns: int, digest: str,
        full_text: str, page_count: int, has_text: bool, root: Path,
    ) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO paper_files
                    (paper_id, path, filename, size, mtime_ns, sha256, full_text, page_count,
                     has_text, exists_flag, scan_root, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    paper_id=excluded.paper_id, filename=excluded.filename, size=excluded.size,
                    mtime_ns=excluded.mtime_ns, sha256=excluded.sha256,
                    full_text=CASE WHEN excluded.full_text<>'' THEN excluded.full_text ELSE paper_files.full_text END,
                    page_count=excluded.page_count, has_text=excluded.has_text, exists_flag=1,
                    scan_root=excluded.scan_root, last_seen_at=excluded.last_seen_at,
                    error_message=''
                """,
                (
                    paper_id, str(path), path.name, size, mtime_ns, digest, full_text,
                    page_count, int(has_text), str(root), utc_now(),
                ),
            )

    def _record_failure(self, path: Path, root: Path, message: str, stat, digest: str) -> None:
        path_string = str(path)
        with self.database.transaction() as conn:
            existing = conn.execute(
                "SELECT paper_id FROM paper_files WHERE path=?", (path_string,)
            ).fetchone()
            if existing:
                paper_id = existing["paper_id"]
                verified = conn.execute(
                    "SELECT is_verified FROM papers WHERE id=?", (paper_id,)
                ).fetchone()
                if verified and not verified["is_verified"]:
                    conn.execute(
                        "UPDATE papers SET status='error', updated_at=? WHERE id=?",
                        (utc_now(), paper_id),
                    )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO papers
                        (filename, filepath, title, authors, journal, year, doi, keywords_auto,
                         status, confidence, metadata_source, updated_at, is_verified, notes)
                    VALUES (?, ?, ?, '', '', '', '', '', 'error', 0, 'PDF error', ?, 0, '')
                    """,
                    (path.name, path_string, path.stem, utc_now()),
                )
                paper_id = cur.lastrowid
            size = stat.st_size if stat is not None else 0
            mtime_ns = stat.st_mtime_ns if stat is not None else 0
            conn.execute(
                """
                INSERT INTO paper_files
                    (paper_id, path, filename, size, mtime_ns, sha256, full_text, page_count,
                     has_text, exists_flag, scan_root, last_seen_at, error_message)
                VALUES (?, ?, ?, ?, ?, ?, '', 0, 0, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size, mtime_ns=excluded.mtime_ns, sha256=excluded.sha256,
                    has_text=0, exists_flag=excluded.exists_flag, scan_root=excluded.scan_root,
                    last_seen_at=excluded.last_seen_at, error_message=excluded.error_message
                """,
                (
                    paper_id, path_string, path.name, size, mtime_ns, digest,
                    int(path.exists()), str(root), utc_now(), message[:2000],
                ),
            )

    def _sync_auto_tags(self, paper_id: int, values: dict) -> None:
        tags = []
        tags.extend(item.strip() for item in values.get("keywords_auto", "").split(";") if item.strip())
        if values.get("year"):
            tags.append(str(values["year"]))
        with self.database.transaction() as conn:
            for tag in dict.fromkeys(tags):
                conn.execute(
                    "INSERT OR IGNORE INTO tags(paper_id, tag_name, tag_type) VALUES (?, ?, 'auto')",
                    (paper_id, tag),
                )
