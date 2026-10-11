#!/usr/bin/env python3
"""Basic keyword search for Biomedical Research Paper Explorer.

Search publication titles and abstracts with case-insensitive, literal term
matching. Each matched term contributes 2 points in the title or 1 point in
the abstract. Terms combine by OR, as specified in the project report.

Usage:
  pip install pandas
  python biomedical_keyword_search.py --input cleaned_papers.csv --query "mass spectrometry"
  python biomedical_keyword_search.py --input cleaned_papers.jsonl --query "proteomics" --limit 10
"""
import argparse
import json
import time
from pathlib import Path

import pandas as pd


def load_papers(path: Path) -> pd.DataFrame:
    """Read CSV, JSON, or JSONL publication records into a DataFrame."""
    extension = path.suffix.lower()
    if extension == ".csv":
        frame = pd.read_csv(path)
    elif extension == ".jsonl":
        frame = pd.read_json(path, lines=True)
    elif extension == ".json":
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if isinstance(data, dict) and isinstance(data.get("papers"), list):
            data = data["papers"]
        if not isinstance(data, list):
            raise ValueError("JSON must be an array of records or {'papers': [...]}.")
        frame = pd.DataFrame(data)
    else:
        raise ValueError("Input must be CSV, JSON, or JSONL")
    if "paper_id" not in frame.columns:
        if "paper_key" in frame.columns:
            frame = frame.rename(columns={"paper_key": "paper_id"})
        else:
            raise ValueError("Dataset requires a paper_id column (or paper_key).")
    for column in ("title", "abstract"):
        if column not in frame.columns:
            frame[column] = ""
    return frame


def search_papers(query: str, dataframe: pd.DataFrame, limit: int = 20) -> list[dict]:
    """Return ranked paper records for an OR search of whitespace-separated terms."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    keywords = query.lower().strip().split()
    if not keywords or limit == 0:
        return []
    required = {"paper_id", "title", "abstract"}
    absent = required.difference(dataframe.columns)
    if absent:
        raise ValueError(f"Missing data columns: {sorted(absent)}")
    results = dataframe.copy()
    results["score"] = 0
    titles = results["title"].fillna("").astype(str).str.lower()
    abstracts = results["abstract"].fillna("").astype(str).str.lower()
    for keyword in keywords:
        results["score"] += titles.str.contains(keyword, regex=False).astype(int) * 2
        results["score"] += abstracts.str.contains(keyword, regex=False).astype(int)
    results = results.loc[results["score"] > 0]
    results = results.sort_values("score", ascending=False, kind="stable")
    records = results[["paper_id", "title", "abstract", "score"]].head(limit)
    records = records.astype(object).where(pd.notna(records), None)
    return records.to_dict("records")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Paper records: CSV, JSON, or JSONL")
    parser.add_argument("--query", required=True, help="Search terms")
    parser.add_argument("--limit", type=int, default=20, help="Maximum number of results")
    parser.add_argument("--output", type=Path, help="Optional JSON output file")
    args = parser.parse_args()
    papers = load_papers(args.input)
    start = time.perf_counter()
    matches = search_papers(args.query, papers, args.limit)
    elapsed_ms = (time.perf_counter() - start) * 1000
    payload = {"query": args.query, "records_searched": len(papers),
               "matches_returned": len(matches), "elapsed_ms": round(elapsed_ms, 3),
               "results": matches}
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    print(encoded)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
