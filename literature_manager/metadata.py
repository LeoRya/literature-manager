from __future__ import annotations

import json
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .config import CROSSREF_BASE, NETWORK_TIMEOUT_SECONDS, OPENALEX_BASE, USER_AGENT
from .extractor import clean_doi


@dataclass
class OnlineMetadata:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    journal: str = ""
    year: str = ""
    publication_date: str = ""
    keywords: list[str] = field(default_factory=list)
    abstract: str = ""
    source: str = ""
    confidence: float = 0.0


def _get_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _date_parts(message: dict) -> tuple[str, str]:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        parts = ((message.get(key) or {}).get("date-parts") or [[]])[0]
        if parts:
            year = str(parts[0])
            date = "-".join(str(value).zfill(2) for value in parts[:3])
            return year, date
    return "", ""


def _from_crossref(message: dict, confidence: float = 0.98) -> OnlineMetadata:
    authors = []
    for author in message.get("author") or []:
        name = " ".join(filter(None, [author.get("given", ""), author.get("family", "")])).strip()
        if name:
            authors.append(name)
    year, publication_date = _date_parts(message)
    title_values = message.get("title") or []
    journal_values = message.get("container-title") or []
    abstract = message.get("abstract") or ""
    # Crossref abstracts frequently contain JATS tags.
    import re

    abstract = re.sub(r"<[^>]+>", " ", abstract)
    return OnlineMetadata(
        title=" ".join(title_values[0].split()) if title_values else "",
        authors=authors,
        doi=clean_doi(message.get("DOI") or ""),
        journal=" ".join(journal_values[0].split()) if journal_values else "",
        year=year,
        publication_date=publication_date,
        keywords=[str(item).strip() for item in message.get("subject") or [] if str(item).strip()],
        abstract=" ".join(abstract.split()),
        source="Crossref",
        confidence=confidence,
    )


def lookup_crossref_doi(doi: str) -> OnlineMetadata | None:
    try:
        # Keep the DOI's path separator intact; Crossref documents this as /works/{doi}.
        payload = _get_json(f"{CROSSREF_BASE}/works/{quote(clean_doi(doi), safe='/')}")
        return _from_crossref(payload["message"])
    except (HTTPError, URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError):
        return None


def lookup_crossref_title(title: str) -> OnlineMetadata | None:
    params = urlencode({"query.bibliographic": title, "rows": 3})
    try:
        payload = _get_json(f"{CROSSREF_BASE}/works?{params}")
        items = payload["message"]["items"]
    except (HTTPError, URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError):
        return None
    normalized = " ".join(title.lower().split())
    best: tuple[float, dict] | None = None
    for item in items:
        titles = item.get("title") or []
        if not titles:
            continue
        similarity = SequenceMatcher(None, normalized, " ".join(titles[0].lower().split())).ratio()
        if not best or similarity > best[0]:
            best = (similarity, item)
    if best and best[0] >= 0.82:
        return _from_crossref(best[1], confidence=min(0.95, best[0]))
    return None


def lookup_openalex_doi(doi: str) -> OnlineMetadata | None:
    normalized = clean_doi(doi)
    if not normalized:
        return None
    try:
        payload = _get_json(
            f"{OPENALEX_BASE}/works/{quote('https://doi.org/' + normalized, safe=':/')}"
        )
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    authors = []
    for authorship in payload.get("authorships") or []:
        name = ((authorship.get("author") or {}).get("display_name") or "").strip()
        if name:
            authors.append(name)
    source = (((payload.get("primary_location") or {}).get("source") or {}).get("display_name") or "")
    concepts = [
        item.get("display_name", "").strip()
        for item in (payload.get("topics") or payload.get("concepts") or [])
        if item.get("display_name")
    ]
    return OnlineMetadata(
        title=(payload.get("title") or "").strip(),
        authors=authors,
        doi=normalized,
        journal=source,
        year=str(payload.get("publication_year") or ""),
        publication_date=payload.get("publication_date") or "",
        keywords=concepts[:20],
        source="OpenAlex",
        confidence=0.94,
    )


def lookup_metadata(doi: str, title: str) -> OnlineMetadata | None:
    if doi:
        return lookup_crossref_doi(doi) or lookup_openalex_doi(doi)
    if len(title.strip()) >= 12:
        return lookup_crossref_title(title)
    return None
