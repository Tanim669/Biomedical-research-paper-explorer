#!/usr/bin/env python3
"""Load preprocessed biomedical paper records into SQLite and verify integrity.
 
Usage:
    python load_papers.py --input cleaned_papers.csv --db papers.db
    python load_papers.py --input cleaned_papers.jsonl --db papers.db
 
Fields: title (required), abstract, authors, year, keywords, journal, doi.
Authors/keywords may be JSON arrays or semicolon-delimited strings in CSV.
JSON/JSONL accepts string or arrays. Repeated imports are idempotent.
"""
import argparse
import csv
import hashlib
import json
import sqlite3
from pathlib import Path
 
FIELDS = ('title', 'abstract', 'authors', 'year', 'keywords', 'journal', 'doi')
 
 
def records_from_file(path):
    suffix = path.suffix.lower()
    if suffix == '.csv':
        with path.open('r', encoding='utf-8-sig', newline='') as f:
            yield from csv.DictReader(f)
    elif suffix == '.jsonl':
        with path.open('r', encoding='utf-8-sig') as f:
            for n, line in enumerate(f, 1):
                if line.strip():
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        raise ValueError(f'Line {n} is not an object')
                    yield obj
    elif suffix == '.json':
        with path.open('r', encoding='utf-8-sig') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('papers'), list):
            data = data['papers']
        if not isinstance(data, list):
            raise ValueError('JSON must contain a list of paper objects or {"papers": [...]}')
        yield from data
    else:
        raise ValueError('Input format must be .csv, .json, or .jsonl')
 
 
def text(value):
    return '' if value is None else str(value).strip()
 
 
def string_list(value):
    if value is None or value == '':
        return []
    if isinstance(value, list):
        return [text(v) for v in value if text(v)]
    if isinstance(value, str):
        value = value.strip()
        if value.startswith('['):
            parsed = json.loads(value)
            if not isinstance(parsed, list):
                raise ValueError('Expected JSON array')
            return [text(v) for v in parsed if text(v)]
        return [x.strip() for x in value.split(';') if x.strip()]
    raise ValueError('Authors and keywords must be lists or strings')
 
 
def normalize(raw, row_number):
    if not isinstance(raw, dict):
        raise ValueError(f'Row {row_number}: expected object')
    title = text(raw.get('title'))
    if not title:
        raise ValueError(f'Row {row_number}: missing required title')
    year_raw = raw.get('year')
    year = None if year_raw is None or text(year_raw) == '' else int(year_raw)
    if year is not None and not 1800 <= year <= 2100:
        raise ValueError(f'Row {row_number}: year out of bounds')
    doi = text(raw.get('doi')).lower()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if doi.startswith(prefix):
            doi = doi[len(prefix):].strip()
    authors = string_list(raw.get('authors'))
    keywords = string_list(raw.get('keywords'))
    # This deterministic key also handles papers without DOIs.
    fingerprint = ('doi:' + doi) if doi else 'titleyear:' + title.casefold() + '|' + str(year)
    paper_key = hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()
    return (paper_key, title, text(raw.get('abstract')), json.dumps(authors, ensure_ascii=False),
            year, json.dumps(keywords, ensure_ascii=False), text(raw.get('journal')), doi or None)
 
 
def initialize(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS papers (
            paper_key TEXT PRIMARY KEY,
            title TEXT NOT NULL CHECK(length(trim(title)) > 0),
            abstract TEXT NOT NULL DEFAULT '',
            authors TEXT NOT NULL DEFAULT '[]',
            year INTEGER CHECK(year BETWEEN 1800 AND 2100),
            keywords TEXT NOT NULL DEFAULT '[]',
            journal TEXT NOT NULL DEFAULT '',
            doi TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);
        CREATE INDEX IF NOT EXISTS idx_papers_journal ON papers(journal);
    ''')
 
 
def run(input_path, db_path):
    source = list(records_from_file(input_path))
    normalized = [normalize(raw, i) for i, raw in enumerate(source, 1)]
    unique = {}
    doi_to_key = {}
    for item in normalized:
        if item[7] and item[7] in doi_to_key and doi_to_key[item[7]] != item[0]:
            raise ValueError('Conflicting DOI keys')
        if item[7]:
            doi_to_key[item[7]] = item[0]
        unique[item[0]] = item  # last duplicate wins
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            initialize(conn)
            before = conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0]
            conn.executemany('''
                INSERT INTO papers(paper_key,title,abstract,authors,year,keywords,journal,doi)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(paper_key) DO UPDATE SET
                    title=excluded.title, abstract=excluded.abstract,
                    authors=excluded.authors, year=excluded.year,
                    keywords=excluded.keywords, journal=excluded.journal, doi=excluded.doi
            ''', unique.values())
            after = conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0]
            keys = list(unique)
            # Check all incoming records are queryable with their expected fields.
            for key, record in unique.items():
                actual = conn.execute('''SELECT paper_key,title,abstract,authors,year,keywords,journal,doi
                                         FROM papers WHERE paper_key=?''', (key,)).fetchone()
                if actual != record:
                    raise AssertionError(f'Data mismatch for paper key {key}')
            integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
            fk = conn.execute('PRAGMA foreign_key_check').fetchall()
            if integrity != 'ok' or fk:
                raise AssertionError(f'DB integrity failed: {integrity}; foreign keys: {fk}')
            for (authors, keywords) in conn.execute('SELECT authors,keywords FROM papers'):
                if not isinstance(json.loads(authors), list) or not isinstance(json.loads(keywords), list):
                    raise AssertionError('Stored author/keyword values are not JSON arrays')
            example = conn.execute('SELECT title,year,journal FROM papers ORDER BY title LIMIT 3').fetchall()
        return {'input_rows': len(source), 'unique_input_papers': len(unique),
                'duplicate_input_rows': len(source) - len(unique), 'existing_before': before,
                'rows_after': after, 'matched_loaded_records': len(unique),
                'sqlite_integrity_check': integrity, 'query_sample': example}
    finally:
        conn.close()
 
 
def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True, help='Cleaned CSV, JSON, or JSONL dataset')
    p.add_argument('--db', type=Path, default=Path('biomedical_papers.db'))
    p.add_argument('--report', type=Path, help='Write verification report as JSON')
    args = p.parse_args()
    result = run(args.input, args.db)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
 
if __name__ == '__main__':
    main()
