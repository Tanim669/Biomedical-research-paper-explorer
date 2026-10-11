#!/usr/bin/env python3
"""Clean and preprocess PubMed publication records from JSONL.

Source: Clean and preprocess acquired data.docx
"""

import csv
import html
import json
import re
from datetime import datetime

INPUT_FILE = "biomedical_papers.jsonl"

OUTPUT_JSONL = "biomedical_papers_clean.jsonl"
OUTPUT_CSV = "biomedical_papers_clean.csv"


def normalize_whitespace(value):
    """
    Remove extra spaces, tabs, and line breaks.
    """

    if not value:
        return ""

    value = html.unescape(str(value))

    return re.sub(r"\s+", " ", value).strip()


def normalize_doi(doi):
    """
    Convert different DOI representations into a
    consistent DOI value.

    Example:
        https://doi.org/10.1000/test
        doi:10.1000/test

    becomes:
        10.1000/test
    """

    if not doi:
        return ""

    doi = normalize_whitespace(doi).lower()

    prefixes = [
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:"
    ]

    for prefix in prefixes:

        if doi.startswith(prefix):
            doi = doi[len(prefix):]

    return doi.strip()


def unique_preserve_order(values):
    """
    Remove duplicates while keeping the original order.
    """

    if not values:
        return []

    seen = set()
    cleaned = []

    for value in values:

        value = normalize_whitespace(value)

        if not value:
            continue

        key = value.casefold()

        if key not in seen:

            seen.add(key)
            cleaned.append(value)

    return cleaned


def normalize_pmid(value):
    """
    Ensure PMID contains digits only.
    """

    if value is None:
        return ""

    pmid = str(value).strip()

    if not pmid.isdigit():
        return ""

    return pmid


def parse_publication_date(date_value):
    """
    Extract year, month, and a normalized date.

    PubMed dates can appear in several forms, so
    preprocessing should tolerate incomplete dates.
    """

    result = {
        "publication_year": None,
        "publication_month": None,
        "publication_date_normalized": ""
    }

    if not date_value:
        return result

    value = normalize_whitespace(date_value)

    # Extract a four-digit year.
    year_match = re.search(r"\b(18|19|20|21)\d{2}\b", value)

    if year_match:
        result["publication_year"] = int(
            year_match.group()
        )

    months = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12
    }

    value_lower = value.lower()

    for name, number in months.items():

        if re.search(
            rf"\b{re.escape(name)}\b",
            value_lower
        ):

            result["publication_month"] = number
            break

    # Check numeric month, e.g. 2026-05.
    numeric_match = re.match(
        r"^\s*(\d{4})-(\d{1,2})",
        value
    )

    if numeric_match:

        result["publication_year"] = int(
            numeric_match.group(1)
        )

        month = int(
            numeric_match.group(2)
        )

        if 1 <= month <= 12:
            result["publication_month"] = month

    year = result["publication_year"]
    month = result["publication_month"]

    if year and month:

        result["publication_date_normalized"] = (
            f"{year:04d}-{month:02d}"
        )

    elif year:

        result["publication_date_normalized"] = str(
            year
        )

    return result


def create_search_text(record):
    """
    Combine the most useful fields for text indexing.
    """

    components = [
        record.get("title", ""),
        record.get("abstract", ""),
        " ".join(record.get("mesh_terms", []))
    ]

    text = " ".join(
        component
        for component in components
        if component
    )

    return normalize_whitespace(text)


def clean_record(record):
    """
    Clean a single publication record.
    """

    pmid = normalize_pmid(
        record.get("pmid")
    )

    if not pmid:
        return None

    title = normalize_whitespace(
        record.get("title")
    )

    abstract = normalize_whitespace(
        record.get("abstract")
    )

    journal = normalize_whitespace(
        record.get("journal")
    )

    doi = normalize_doi(
        record.get("doi")
    )

    authors = unique_preserve_order(
        record.get("authors", [])
    )

    mesh_terms = unique_preserve_order(
        record.get("mesh_terms", [])
    )

    publication_types = unique_preserve_order(
        record.get(
            "publication_types",
            []
        )
    )

    topics = unique_preserve_order(
        record.get("topics", [])
    )

    publication_date_raw = normalize_whitespace(
        record.get("publication_date")
    )

    date_fields = parse_publication_date(
        publication_date_raw
    )

    cleaned = {
        "pmid": pmid,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "journal": journal,

        "publication_date_raw":
            publication_date_raw,

        "publication_year":
            date_fields["publication_year"],

        "publication_month":
            date_fields["publication_month"],

        "publication_date_normalized":
            date_fields[
                "publication_date_normalized"
            ],

        "doi": doi,
        "mesh_terms": mesh_terms,
        "publication_types": publication_types,
        "topics": topics,

        "has_abstract": bool(abstract),
        "has_doi": bool(doi),

        "author_count": len(authors),
        "mesh_term_count": len(mesh_terms),

        "source": normalize_whitespace(
            record.get("source", "PubMed")
        ),

        "retrieved_at": normalize_whitespace(
            record.get("retrieved_at")
        )
    }

    cleaned["search_text"] = create_search_text(
        cleaned
    )

    return cleaned


def merge_records(existing, new):
    """
    Merge duplicate PMID records.

    Topic, author, MeSH, and publication-type arrays
    are combined instead of discarding information.
    """

    existing["topics"] = unique_preserve_order(
        existing.get("topics", [])
        + new.get("topics", [])
    )

    existing["authors"] = unique_preserve_order(
        existing.get("authors", [])
        + new.get("authors", [])
    )

    existing["mesh_terms"] = unique_preserve_order(
        existing.get("mesh_terms", [])
        + new.get("mesh_terms", [])
    )

    existing["publication_types"] = (
        unique_preserve_order(
            existing.get(
                "publication_types",
                []
            )
            + new.get(
                "publication_types",
                []
            )
        )
    )

    # Keep the more complete abstract.
    if (
        len(new.get("abstract", ""))
        > len(existing.get("abstract", ""))
    ):
        existing["abstract"] = new["abstract"]

    # Use DOI if the existing copy is missing one.
    if (
        not existing.get("doi")
        and new.get("doi")
    ):
        existing["doi"] = new["doi"]

    existing["has_abstract"] = bool(
        existing.get("abstract")
    )

    existing["has_doi"] = bool(
        existing.get("doi")
    )

    existing["author_count"] = len(
        existing["authors"]
    )

    existing["mesh_term_count"] = len(
        existing["mesh_terms"]
    )

    existing["search_text"] = (
        create_search_text(existing)
    )

    return existing


def load_and_clean(filename):
    """
    Read JSONL input, clean records, and remove duplicates.
    """

    records = {}

    raw_count = 0
    invalid_count = 0
    duplicate_count = 0

    with open(
        filename,
        "r",
        encoding="utf-8"
    ) as file:

        for line in file:

            line = line.strip()

            if not line:
                continue

            raw_count += 1

            try:
                raw_record = json.loads(line)

            except json.JSONDecodeError:

                invalid_count += 1
                continue

            cleaned = clean_record(
                raw_record
            )

            if cleaned is None:

                invalid_count += 1
                continue

            pmid = cleaned["pmid"]

            if pmid in records:

                duplicate_count += 1

                records[pmid] = merge_records(
                    records[pmid],
                    cleaned
                )

            else:

                records[pmid] = cleaned

    print(f"Raw records: {raw_count}")
    print(f"Invalid records: {invalid_count}")
    print(f"Duplicate records merged: {duplicate_count}")
    print(f"Clean records: {len(records)}")

    return list(records.values())


def save_jsonl(records, filename):

    with open(
        filename,
        "w",
        encoding="utf-8"
    ) as file:

        for record in records:

            file.write(
                json.dumps(
                    record,
                    ensure_ascii=False
                )
                + "\n"
            )


def save_csv(records, filename):

    fields = [
        "pmid",
        "title",
        "abstract",
        "authors",
        "journal",
        "publication_date_raw",
        "publication_year",
        "publication_month",
        "publication_date_normalized",
        "doi",
        "mesh_terms",
        "publication_types",
        "topics",
        "has_abstract",
        "has_doi",
        "author_count",
        "mesh_term_count",
        "source",
        "search_text",
        "retrieved_at"
    ]

    with open(
        filename,
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields
        )

        writer.writeheader()

        for record in records:

            row = record.copy()

            row["authors"] = "; ".join(
                record["authors"]
            )

            row["mesh_terms"] = "; ".join(
                record["mesh_terms"]
            )

            row["publication_types"] = "; ".join(
                record["publication_types"]
            )

            row["topics"] = "; ".join(
                record["topics"]
            )

            writer.writerow(row)


def main():

    print(
        "Starting biomedical data preprocessing..."
    )

    records = load_and_clean(
        INPUT_FILE
    )

    save_jsonl(
        records,
        OUTPUT_JSONL
    )

    save_csv(
        records,
        OUTPUT_CSV
    )

    print("\nPreprocessing completed.")

    print(
        f"JSONL output: {OUTPUT_JSONL}"
    )

    print(
        f"CSV output: {OUTPUT_CSV}"
    )


if __name__ == "__main__":
    main()
