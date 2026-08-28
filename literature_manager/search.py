from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .database import Database


FIELDS = {
    "标题": "title",
    "作者": "authors",
    "DOI": "doi",
    "发表时间": "publication_date",
    "年份": "year",
    "关键词": "keywords_auto",
    "期刊": "journal",
    "标签": "tags",
    "文件位置": "path",
    "状态": "status",
}


@dataclass
class SearchCondition:
    field: str
    values: list[str] = field(default_factory=list)
    value_logic: str = "OR"


class SearchService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _field_clause(field: str, value: str, params: list[str]) -> str:
        value = value.strip().lower()
        params.append(f"%{value}%")
        if field == "tags":
            return "EXISTS (SELECT 1 FROM tags tx WHERE tx.paper_id=p.id AND lower(tx.tag_name) LIKE ?)"
        if field == "path":
            return "EXISTS (SELECT 1 FROM paper_files fx WHERE fx.paper_id=p.id AND lower(fx.path) LIKE ?)"
        column = field if field in {
            "title", "authors", "doi", "publication_date", "year", "keywords_auto", "journal", "status"
        } else "title"
        return f"lower(coalesce(p.{column},'')) LIKE ?"

    def search(
        self,
        global_text: str = "",
        conditions: Sequence[SearchCondition] = (),
        condition_logic: str = "AND",
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[str] = []
        global_text = global_text.strip().lower()
        if global_text:
            token = f"%{global_text}%"
            params.extend([token] * 11)
            clauses.append(
                """
                (lower(coalesce(p.title,'')) LIKE ? OR lower(coalesce(p.authors,'')) LIKE ?
                 OR lower(coalesce(p.doi,'')) LIKE ? OR lower(coalesce(p.journal,'')) LIKE ?
                 OR lower(coalesce(p.year,'')) LIKE ? OR lower(coalesce(p.publication_date,'')) LIKE ?
                 OR lower(coalesce(p.keywords_auto,'')) LIKE ? OR lower(coalesce(p.abstract,'')) LIKE ?
                 OR lower(coalesce(p.notes,'')) LIKE ?
                 OR EXISTS (SELECT 1 FROM tags gt WHERE gt.paper_id=p.id AND lower(gt.tag_name) LIKE ?)
                 OR EXISTS (SELECT 1 FROM paper_files gf WHERE gf.paper_id=p.id
                            AND (lower(gf.path) LIKE ? OR lower(gf.full_text) LIKE ?)))
                """
            )
            # The last file predicate has two placeholders.
            params.append(token)

        condition_clauses = []
        for condition in conditions:
            values = [value.strip() for value in condition.values if value.strip()]
            if not values:
                continue
            parts = [self._field_clause(condition.field, value, params) for value in values]
            joiner = " AND " if condition.value_logic.upper() == "AND" else " OR "
            condition_clauses.append("(" + joiner.join(parts) + ")")
        if condition_clauses:
            joiner = " AND " if condition_logic.upper() == "AND" else " OR "
            clauses.append("(" + joiner.join(condition_clauses) + ")")

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"""
            SELECT p.*,
                   (SELECT group_concat(tag_name, '; ') FROM tags
                    WHERE paper_id=p.id AND tag_type='custom') AS custom_tags,
                   (SELECT group_concat(path, '\n') FROM paper_files
                    WHERE paper_id=p.id ORDER BY exists_flag DESC, path) AS file_paths,
                   (SELECT count(*) FROM paper_files WHERE paper_id=p.id AND exists_flag=1) AS active_files
            FROM papers p
            {where}
            ORDER BY p.is_verified DESC,
                     CASE p.status WHEN 'review' THEN 0 WHEN 'ready' THEN 1 ELSE 2 END,
                     CASE WHEN p.year GLOB '[0-9][0-9][0-9][0-9]' THEN CAST(p.year AS INTEGER) ELSE 0 END DESC,
                     lower(p.title)
        """
        with self.database.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]
