#!/usr/bin/env python3
"""Acquire biomedical publication metadata from Europe PMC into JSONL and SQLite.

Requires: pip install requests
Example: python biomedical_full_data_acquisition.py --query 'OPEN_ACCESS:y' --target 10000

A checkpoint is written after each committed page. Keep the output directory intact
when resuming. Network access to the public Europe PMC REST API is required.
"""
import argparse
import hashlib
import json
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'


def fetch_page(session, query, cursor, page_size=1000, retries=5, timeout=60):
    params = {'query': query, 'format': 'json', 'resultType': 'core',
              'pageSize': page_size, 'cursorMark': cursor}
    for attempt in range(retries):
        try:
            response = session.get(API_URL, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(min(30, 2 ** attempt) + random.uniform(0, 0.4))


def normalize_record(item):
    if not isinstance(item, dict):
        return None
    source = str(item.get('source') or '').strip()
    source_id = str(item.get('id') or '').strip()
    title = str(item.get('title') or '').strip()
    year = item.get('pubYear')
    try:
        year = int(year)
    except (TypeError, ValueError):
        return None
    if not (1800 <= year <= datetime.now(timezone.utc).year + 2):
        return None
    if not source or not source_id or not title:
        return None
    paper_id = f'{source}:{source_id}'
    author_list = item.get('authorList') or {}
    authors = [a.get('fullName', '').strip() for a in author_list.get('author', [])
               if isinstance(a, dict) and a.get('fullName')]
    keywords = item.get('keywordList') or {}
    kw = keywords.get('keyword', []) if isinstance(keywords, dict) else []
    if not isinstance(kw, list):
        kw = [kw]
    doi = str(item.get('doi') or '').strip().lower() or None
    return {'paper_id': paper_id, 'source': source, 'source_id': source_id,
            'title': title, 'abstract': item.get('abstractText') or '',
            'authors': authors, 'journal': (item.get('journalTitle') or
            ((item.get('journalInfo') or {}).get('journal') or {}).get('title') or ''),
            'pub_year': year, 'publication_date': item.get('firstPublicationDate'),
            'doi': doi, 'pmid': item.get('pmid'), 'pmcid': item.get('pmcid'),
            'keywords': kw, 'retrieved_at': datetime.now(timezone.utc).isoformat()}


def validate_record(record):
    required = ('paper_id', 'source', 'title', 'pub_year')
    return all(record.get(field) for field in required)


def setup_database(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS papers (
        paper_id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
        title TEXT NOT NULL, abstract TEXT, authors_json TEXT NOT NULL,
        journal TEXT, pub_year INTEGER NOT NULL, publication_date TEXT,
        doi TEXT, pmid TEXT, pmcid TEXT, keywords_json TEXT NOT NULL,
        retrieved_at TEXT NOT NULL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(pub_year)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_papers_title ON papers(title)')


def insert_record(conn, r):
    conn.execute('''INSERT INTO papers
       (paper_id,source,source_id,title,abstract,authors_json,journal,pub_year,
        publication_date,doi,pmid,pmcid,keywords_json,retrieved_at)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
       ON CONFLICT(paper_id) DO UPDATE SET
          title=excluded.title, abstract=excluded.abstract,
          authors_json=excluded.authors_json, journal=excluded.journal,
          pub_year=excluded.pub_year, publication_date=excluded.publication_date,
          doi=excluded.doi, pmid=excluded.pmid, pmcid=excluded.pmcid,
          keywords_json=excluded.keywords_json,
          retrieved_at=excluded.retrieved_at''',
       (r['paper_id'], r['source'], r['source_id'], r['title'], r['abstract'],
        json.dumps(r['authors'], ensure_ascii=False), r['journal'], r['pub_year'],
        r['publication_date'], r['doi'], r['pmid'], r['pmcid'],
        json.dumps(r['keywords'], ensure_ascii=False), r['retrieved_at']))


def verify(output_dir, sample_size=20):
    db = output_dir / 'papers.sqlite'
    with sqlite3.connect(db) as conn:
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        missing = conn.execute('''SELECT COUNT(*) FROM papers WHERE paper_id IS NULL
            OR trim(paper_id)='' OR source IS NULL OR trim(source)=''
            OR title IS NULL OR trim(title)='' OR pub_year IS NULL''').fetchone()[0]
        total = conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0]
        sampled = conn.execute('SELECT paper_id,title,pub_year FROM papers ORDER BY RANDOM() LIMIT ?',
                               (sample_size,)).fetchall()
        duplicate_ids = conn.execute('''SELECT COUNT(*) FROM (
            SELECT paper_id FROM papers GROUP BY paper_id HAVING COUNT(*) > 1)''').fetchone()[0]
    report = {'db_count': total, 'sqlite_integrity': integrity,
              'missing_critical_fields': missing, 'duplicate_paper_ids': duplicate_ids,
              'sampled_records': sampled}
    (output_dir / 'integrity_report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    if integrity != 'ok' or missing or duplicate_ids:
        raise RuntimeError('Integrity verification failed; inspect integrity_report.json')
    return report


def acquire(query, target, out, page_size=1000, session=None):
    out.mkdir(parents=True, exist_ok=True)
    checkpoint = out / 'checkpoint.json'
    query_hash = hashlib.sha256(query.encode('utf-8')).hexdigest()
    cursor = '*'
    count = 0
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text(encoding='utf-8'))
        if state['query_hash'] != query_hash:
            raise ValueError('Existing checkpoint belongs to a different query; choose another output directory')
        cursor, count = state['cursor'], state['processed']
        if state.get('complete') and count < target:
            raise ValueError('Previous run reached the end of this query; use another query or output directory')
    jsonl_path = out / 'papers.jsonl'
    own_session = session is None
    if own_session:
        session = requests.Session()
    try:
        with sqlite3.connect(out / 'papers.sqlite') as conn, jsonl_path.open('a', encoding='utf-8') as f:
            setup_database(conn)
            while count < target:
                payload = fetch_page(session, query, cursor, min(page_size, target - count))
                items = (payload.get('resultList') or {}).get('result') or []
                next_cursor = payload.get('nextCursorMark')
                if not items:
                    break
                # Commit SQL before checkpoint. On unexpected crash, rerun can safely
                # upsert duplicate SQL records; JSONL may include duplicates, so readers
                # should deduplicate using paper_id.
                valid = []
                for item in items:
                    record = normalize_record(item)
                    if record and validate_record(record):
                        valid.append(record)
                with conn:
                    for record in valid:
                        insert_record(conn, record)
                for record in valid:
                    f.write(json.dumps(record, ensure_ascii=False) + '\n')
                f.flush()
                count += len(items)
                finished = not next_cursor or next_cursor == cursor
                checkpoint.write_text(json.dumps({'query_hash': query_hash, 'cursor': next_cursor,
                    'processed': count, 'complete': finished}, indent=2), encoding='utf-8')
                print(f'Processed {count} API records; stored {len(valid)} valid records from latest page')
                if finished:
                    break
                cursor = next_cursor
    finally:
        if own_session:
            session.close()
    return verify(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', default='OPEN_ACCESS:y', help='Europe PMC query syntax')
    parser.add_argument('--target', type=int, default=10000, help='Target API records (10k-50k recommended)')
    parser.add_argument('--page-size', type=int, default=1000)
    parser.add_argument('--out', type=Path, default=Path('data/raw'))
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if args.verify_only:
        result = verify(args.out)
    else:
        if args.target < 1 or not 1 <= args.page_size <= 1000:
            parser.error('target must be positive and page-size must be in 1..1000')
        result = acquire(args.query, args.target, args.out, args.page_size)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
