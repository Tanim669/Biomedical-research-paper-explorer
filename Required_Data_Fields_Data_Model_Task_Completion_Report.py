#!/usr/bin/env python3
"""Biomedical Research Paper Explorer: relational schema and JSON-record validation.

Based on the 'Define Required Data Fields and Data Model' design report.

Examples:
  python biomedical_research_data_model.py --database biomedical_papers.db
  python biomedical_research_data_model.py --database biomedical_papers.db --demo
  python biomedical_research_data_model.py --database biomedical_papers.db --input papers.jsonl

JSON input may be a JSON array, a single JSON object, or JSONL records.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS papers (
 paper_id TEXT PRIMARY KEY,
 title TEXT NOT NULL CHECK(length(trim(title)) > 0),
 abstract TEXT,
 publication_year INTEGER NOT NULL CHECK(publication_year BETWEEN 1500 AND 2200),
 publication_date TEXT,
 journal TEXT NOT NULL CHECK(length(trim(journal)) > 0),
 doi TEXT UNIQUE COLLATE NOCASE,
 pmid TEXT UNIQUE,
 pmcid TEXT UNIQUE,
 article_type TEXT,
 language TEXT,
 full_text_available INTEGER CHECK(full_text_available IN (0,1)),
 source TEXT NOT NULL CHECK(length(trim(source)) > 0),
 source_url TEXT,
 retrieved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authors (
 author_id INTEGER PRIMARY KEY AUTOINCREMENT,
 full_name TEXT NOT NULL UNIQUE,
 first_name TEXT,
 last_name TEXT,
 orcid TEXT,
 affiliation TEXT
);
CREATE TABLE IF NOT EXISTS paper_authors (
 paper_id TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
 author_id INTEGER NOT NULL REFERENCES authors(author_id),
 author_order INTEGER NOT NULL CHECK(author_order >= 1),
 corresponding_author INTEGER DEFAULT 0 CHECK(corresponding_author IN (0,1)),
 PRIMARY KEY (paper_id, author_id),
 UNIQUE(paper_id, author_order)
);
CREATE TABLE IF NOT EXISTS keywords (
 keyword_id INTEGER PRIMARY KEY AUTOINCREMENT,
 keyword TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS paper_keywords (
 paper_id TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
 keyword_id INTEGER NOT NULL REFERENCES keywords(keyword_id),
 PRIMARY KEY(paper_id, keyword_id)
);
CREATE TABLE IF NOT EXISTS mesh_terms (
 mesh_id INTEGER PRIMARY KEY AUTOINCREMENT,
 term TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS paper_mesh_terms (
 paper_id TEXT NOT NULL REFERENCES papers(paper_id) ON DELETE CASCADE,
 mesh_id INTEGER NOT NULL REFERENCES mesh_terms(mesh_id),
 PRIMARY KEY(paper_id, mesh_id)
);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(publication_year);
CREATE INDEX IF NOT EXISTS idx_papers_journal ON papers(journal);
CREATE INDEX IF NOT EXISTS idx_papers_article_type ON papers(article_type);
CREATE INDEX IF NOT EXISTS idx_papers_language ON papers(language);
CREATE INDEX IF NOT EXISTS idx_papers_source ON papers(source);
CREATE INDEX IF NOT EXISTS idx_paper_authors_author ON paper_authors(author_id);
CREATE INDEX IF NOT EXISTS idx_paper_keywords_keyword ON paper_keywords(keyword_id);
CREATE INDEX IF NOT EXISTS idx_paper_mesh_terms_mesh ON paper_mesh_terms(mesh_id);
"""


def normalize_doi(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)\s*", "", str(value).strip(), flags=re.I)
    return doi.lower() or None


def validate_record(record: dict[str, Any]) -> dict[str, Any]:
    """Validate core requirements and normalize a record without dropping optional fields."""
    if not isinstance(record, dict):
        raise ValueError("Each publication must be a JSON object")
    out = dict(record)
    for name in ("paper_id", "title", "journal", "source", "retrieved_at"):
        if not isinstance(out.get(name), str) or not out[name].strip():
            raise ValueError(f"Required nonempty text field: {name}")
        out[name] = out[name].strip()
    try:
        year = int(out["publication_year"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("publication_year must be an integer") from exc
    if year < 1500 or year > 2200:
        raise ValueError("publication_year outside permitted range 1500–2200")
    out["publication_year"] = year
    if not isinstance(out.get("authors"), list) or not out["authors"]:
        raise ValueError("authors must be a nonempty ordered list")
    for author in out["authors"]:
        if not (isinstance(author, str) and author.strip()) and not (
            isinstance(author, dict) and isinstance(author.get("full_name"), str) and author["full_name"].strip()
        ):
            raise ValueError("authors must contain nonempty names or author objects")
    for key in ("keywords", "mesh_terms"):
        if out.get(key) is not None and not isinstance(out[key], list):
            raise ValueError(f"{key} must be a list if present")
    try:
        dt = datetime.fromisoformat(out["retrieved_at"].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("retrieved_at must include a timezone")
    except ValueError as exc:
        raise ValueError("retrieved_at must be an ISO 8601 timestamp with timezone") from exc
    out["doi"] = normalize_doi(out.get("doi"))
    if out.get("full_text_available") is not None and not isinstance(out["full_text_available"], bool):
        raise ValueError("full_text_available must be a boolean")
    return out


def create_database(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def add_terms(conn: sqlite3.Connection, paper_id: str, terms: list[Any], table: str, join_table: str,
              term_col: str, id_col: str) -> None:
    for term in dict.fromkeys(str(t).strip() for t in terms if str(t).strip()):
        conn.execute(f"INSERT OR IGNORE INTO {table}({term_col}) VALUES (?)", (term,))
        term_id = conn.execute(f"SELECT {id_col} FROM {table} WHERE {term_col} = ? COLLATE NOCASE", (term,)).fetchone()[0]
        conn.execute(f"INSERT INTO {join_table}(paper_id,{id_col}) VALUES (?,?)", (paper_id, term_id))


def insert_paper(conn: sqlite3.Connection, raw: dict[str, Any]) -> None:
    rec = validate_record(raw)
    columns = ("paper_id", "title", "abstract", "publication_year", "publication_date", "journal", "doi", "pmid",
               "pmcid", "article_type", "language", "full_text_available", "source", "source_url", "retrieved_at")
    conn.execute(f"INSERT INTO papers({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                 tuple(rec.get(key) for key in columns))
    for order, author in enumerate(rec["authors"], 1):
        author_obj = {"full_name": author} if isinstance(author, str) else author
        name = author_obj["full_name"].strip()
        conn.execute("INSERT OR IGNORE INTO authors(full_name,first_name,last_name,orcid,affiliation) VALUES (?,?,?,?,?)",
                     (name, author_obj.get("first_name"), author_obj.get("last_name"), author_obj.get("orcid"), author_obj.get("affiliation")))
        author_id = conn.execute("SELECT author_id FROM authors WHERE full_name=?", (name,)).fetchone()[0]
        conn.execute("INSERT INTO paper_authors(paper_id,author_id,author_order,corresponding_author) VALUES (?,?,?,?)",
                     (rec["paper_id"], author_id, order, int(bool(author_obj.get("corresponding_author", False)))))
    add_terms(conn, rec["paper_id"], rec.get("keywords") or [], "keywords", "paper_keywords", "keyword", "keyword_id")
    add_terms(conn, rec["paper_id"], rec.get("mesh_terms") or [], "mesh_terms", "paper_mesh_terms", "term", "mesh_id")


def read_records(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        with path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else [data]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="biomedical_papers.db", help="SQLite database output")
    parser.add_argument("--input", type=Path, help="Optional .json or .jsonl publication records")
    parser.add_argument("--demo", action="store_true", help="Insert a clearly labeled fictional example")
    args = parser.parse_args()
    if args.demo and args.input:
        parser.error("Choose either --demo or --input")
    records = []
    if args.demo:
        records = [{"paper_id": "paper_000001", "title": "Example biomedical research paper",
                    "abstract": "Structured abstract text...", "authors": ["Author One", "Author Two"],
                    "publication_year": 2025, "journal": "Example Journal", "doi": "10.1234/example.001",
                    "pmid": "12345678", "keywords": ["metabolomics", "mass spectrometry"],
                    "mesh_terms": ["Metabolomics"], "article_type": "Research Article", "source": "Europe PMC",
                    "retrieved_at": "2026-09-20T00:00:00Z"}]
    elif args.input:
        records = read_records(args.input)
    with sqlite3.connect(args.database) as conn:
        create_database(conn)
        with conn:
            for rec in records:
                insert_paper(conn, rec)
        print("SQLite integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
        print("Papers:", conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        print("Sample query:", conn.execute("SELECT paper_id,title,publication_year FROM papers ORDER BY paper_id LIMIT 3").fetchall())
    print("Database ready:", args.database)


if __name__ == "__main__":
    main()
