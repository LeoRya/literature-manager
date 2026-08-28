from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz


DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
YEAR_PATTERN = re.compile(r"\b((?:19|20)\d{2})\b")


def clean_doi(value: str | None) -> str:
    if not value:
        return ""
    match = DOI_PATTERN.search(value)
    if not match:
        return ""
    return match.group(0).rstrip(".,;:)]}>").lower()


def split_authors(value: str) -> list[str]:
    if not value:
        return []
    value = value.replace(" and ", ";").replace("\n", ";")
    separator = ";" if ";" in value else ","
    return [part.strip() for part in value.split(separator) if part.strip()]


def _valid_title(value: str, filename: str) -> bool:
    candidate = " ".join((value or "").split()).strip()
    if len(candidate) < 8:
        return False
    generic = {"untitled", "microsoft word", "document", filename.lower(), Path(filename).stem.lower()}
    return candidate.lower() not in generic and not candidate.lower().startswith("doi:")


def _title_from_first_page(text: str, filename: str) -> str:
    lines = [" ".join(line.split()).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    skip = re.compile(
        r"^(arxiv:|doi:|https?://|received |accepted |published |copyright|©|physical review|"
        r"journal of|proceedings of|vol\.|volume |issn|article|research article)",
        re.I,
    )
    candidates: list[str] = []
    for line in lines[:35]:
        if skip.search(line) or DOI_PATTERN.search(line):
            continue
        if YEAR_PATTERN.fullmatch(line) or len(line) < 12 or len(line) > 320:
            continue
        if line.count(",") >= 3 and len(line.split()) < 20:
            continue
        candidates.append(line)
        if len(candidates) >= 4:
            break
    if not candidates:
        return Path(filename).stem
    title = candidates[0]
    if len(title) < 55 and len(candidates) > 1 and not candidates[1].endswith((',', ';')):
        combined = f"{title} {candidates[1]}"
        if len(combined) <= 260:
            title = combined
    return title if _valid_title(title, filename) else Path(filename).stem


def _extract_keywords(text: str) -> list[str]:
    for pattern in (
        r"(?:keywords?|index terms)\s*[:—-]\s*([^\n]{2,500})",
        r"关键词\s*[:：]\s*([^\n]{2,500})",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            raw = match.group(1).strip().rstrip(".")
            return [item.strip() for item in re.split(r"[;,；，]", raw) if item.strip()][:30]
    return []


@dataclass
class ExtractedDocument:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    journal: str = ""
    year: str = ""
    publication_date: str = ""
    keywords: list[str] = field(default_factory=list)
    abstract: str = ""
    full_text: str = ""
    page_count: int = 0
    has_text: bool = True
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)


def extract_pdf(path: str | Path) -> ExtractedDocument:
    path = Path(path)
    result = ExtractedDocument()
    try:
        document = fitz.open(path)
    except Exception as exc:
        raise ValueError(f"无法打开 PDF：{exc}") from exc

    try:
        result.page_count = document.page_count
        metadata = document.metadata or {}
        chunks: list[str] = []
        for page in document:
            try:
                chunks.append(page.get_text("text", sort=True))
            except Exception:
                chunks.append("")
        result.full_text = "\n".join(chunks).strip()
        result.has_text = len(re.sub(r"\s+", "", result.full_text)) >= 80
        if not result.has_text:
            result.warnings.append("扫描件或无文字层")

        first_page = chunks[0] if chunks else ""
        meta_title = " ".join((metadata.get("title") or "").split())
        result.title = meta_title if _valid_title(meta_title, path.name) else _title_from_first_page(first_page, path.name)
        result.authors = split_authors(metadata.get("author") or "")
        result.doi = clean_doi("\n".join([metadata.get("subject") or "", first_page, result.full_text[:30000]]))
        result.keywords = _extract_keywords(result.full_text[:50000])

        year_match = YEAR_PATTERN.search(
            " ".join([metadata.get("creationDate") or "", first_page[:12000], path.stem])
        )
        result.year = year_match.group(1) if year_match else ""

        subject = " ".join((metadata.get("subject") or "").split())
        if subject and not DOI_PATTERN.search(subject) and len(subject) < 240:
            result.journal = subject

        score = 0.15
        score += 0.3 if _valid_title(result.title, path.name) else 0
        score += 0.2 if result.doi else 0
        score += 0.1 if result.authors else 0
        score += 0.1 if result.year else 0
        score += 0.1 if result.has_text else 0
        result.confidence = min(score, 0.85)
        return result
    finally:
        document.close()
