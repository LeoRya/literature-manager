from __future__ import annotations

import csv
import re
from pathlib import Path

from .database import Database


EXPORT_FIELDS = [
    "title", "authors", "doi", "journal", "year", "publication_date", "keywords_auto",
    "abstract", "tags", "notes", "file_paths",
]


def _export_rows(database: Database, paper_ids: list[int] | None = None) -> list[dict]:
    ids = paper_ids or database.all_paper_ids()
    rows = []
    for paper_id in ids:
        paper = database.get_paper(paper_id)
        if not paper:
            continue
        paper["tags"] = "; ".join(paper["tags"])
        paper["file_paths"] = " | ".join(item["path"] for item in paper["files"])
        rows.append(paper)
    return rows


def export_csv(database: Database, path: str | Path, paper_ids: list[int] | None = None) -> int:
    rows = _export_rows(database, paper_ids)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _bib_key(row: dict, index: int) -> str:
    family = re.sub(r"\W+", "", (row.get("authors") or "paper").split(";")[0].split()[-1])
    return f"{family or 'paper'}{row.get('year') or 'nd'}_{index}"


def export_bibtex(database: Database, path: str | Path, paper_ids: list[int] | None = None) -> int:
    rows = _export_rows(database, paper_ids)
    chunks = []
    for index, row in enumerate(rows, start=1):
        fields = {
            "title": row.get("title", ""),
            "author": (row.get("authors") or "").replace(";", " and"),
            "journal": row.get("journal", ""), "year": row.get("year", ""),
            "doi": row.get("doi", ""), "keywords": row.get("keywords_auto", ""),
            "abstract": row.get("abstract", ""), "note": row.get("notes", ""),
        }
        lines = [f"@article{{{_bib_key(row, index)},"]
        for key, value in fields.items():
            value = str(value or "").replace("\n", " ").replace("{", "\\{").replace("}", "\\}")
            if value:
                lines.append(f"  {key} = {{{value}}},")
        lines.append("}")
        chunks.append("\n".join(lines))
    Path(path).write_text("\n\n".join(chunks) + "\n", encoding="utf-8")
    return len(rows)


def export_ris(database: Database, path: str | Path, paper_ids: list[int] | None = None) -> int:
    rows = _export_rows(database, paper_ids)
    chunks = []
    for row in rows:
        lines = ["TY  - JOUR", f"TI  - {row.get('title', '')}"]
        for author in (row.get("authors") or "").split(";"):
            if author.strip():
                lines.append(f"AU  - {author.strip()}")
        mapping = {"JO": "journal", "PY": "year", "DO": "doi", "AB": "abstract", "N1": "notes"}
        for code, key in mapping.items():
            if row.get(key):
                lines.append(f"{code}  - {row[key]}")
        for keyword in (row.get("keywords_auto") or "").split(";"):
            if keyword.strip():
                lines.append(f"KW  - {keyword.strip()}")
        lines.append("ER  -")
        chunks.append("\n".join(lines))
    Path(path).write_text("\n\n".join(chunks) + "\n", encoding="utf-8")
    return len(rows)


def _insert(database: Database, values: dict, tags: list[str]) -> bool:
    _, created = database.insert_imported(values, tags)
    return created


def import_csv(database: Database, path: str | Path) -> tuple[int, int]:
    created = matched = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            tags = re.split(r"[;|]", row.pop("tags", "") or "")
            if _insert(database, row, tags):
                created += 1
            else:
                matched += 1
    return created, matched


def import_bibtex(database: Database, path: str | Path) -> tuple[int, int]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    created = matched = 0
    for entry in re.split(r"(?=@\w+\s*\{)", text):
        if not entry.lstrip().startswith("@"):
            continue
        fields: dict[str, str] = {}
        for match in re.finditer(r"(?ms)^\s*(\w+)\s*=\s*[\{\"](.*?)[\}\"]\s*,?\s*$", entry):
            fields[match.group(1).lower()] = " ".join(match.group(2).split())
        values = {
            "title": fields.get("title", ""), "authors": fields.get("author", "").replace(" and ", "; "),
            "journal": fields.get("journal", ""), "year": fields.get("year", ""),
            "doi": fields.get("doi", ""), "keywords_auto": fields.get("keywords", ""),
            "abstract": fields.get("abstract", ""), "notes": fields.get("note", ""),
        }
        tags = re.split(r"[;,]", fields.get("tags", ""))
        if values["title"] and _insert(database, values, tags):
            created += 1
        elif values["title"]:
            matched += 1
    return created, matched


def import_ris(database: Database, path: str | Path) -> tuple[int, int]:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    created = matched = 0
    for block in re.split(r"(?m)^ER  -\s*$", text):
        fields: dict[str, list[str]] = {}
        for line in block.splitlines():
            match = re.match(r"^([A-Z0-9]{2})  -\s?(.*)$", line)
            if match:
                fields.setdefault(match.group(1), []).append(match.group(2).strip())
        title = (fields.get("TI") or fields.get("T1") or [""])[0]
        if not title:
            continue
        values = {
            "title": title, "authors": "; ".join(fields.get("AU", [])),
            "journal": (fields.get("JO") or fields.get("JF") or [""])[0],
            "year": (fields.get("PY") or fields.get("Y1") or [""])[0][:4],
            "doi": (fields.get("DO") or [""])[0], "keywords_auto": "; ".join(fields.get("KW", [])),
            "abstract": (fields.get("AB") or [""])[0], "notes": "\n".join(fields.get("N1", [])),
        }
        if _insert(database, values, []):
            created += 1
        else:
            matched += 1
    return created, matched


def import_file(database: Database, path: str | Path) -> tuple[int, int]:
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        return import_csv(database, path)
    if suffix in {".bib", ".bibtex"}:
        return import_bibtex(database, path)
    if suffix == ".ris":
        return import_ris(database, path)
    raise ValueError(f"不支持的导入格式：{suffix}")


def export_file(database: Database, path: str | Path, paper_ids: list[int] | None = None) -> int:
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        return export_csv(database, path, paper_ids)
    if suffix in {".bib", ".bibtex"}:
        return export_bibtex(database, path, paper_ids)
    if suffix == ".ris":
        return export_ris(database, path, paper_ids)
    raise ValueError(f"不支持的导出格式：{suffix}")
