"""
Biomedical Paper Explorer
Initial PubMed Data Acquisition Script

Requirements:
    pip install requests

Optional environment variables:
    NCBI_EMAIL=your_email@example.com
    NCBI_API_KEY=your_ncbi_api_key
"""

import csv
import json
import os
import time
import logging
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

EMAIL = os.getenv("NCBI_EMAIL", "replace_with_your_email@example.com")
API_KEY = os.getenv("NCBI_API_KEY")

TOOL_NAME = "biomedical_paper_explorer"

# Research topics for the initial collection.
TOPICS = {
    "mass_spectrometry": '"mass spectrometry"[Title/Abstract]',
    "proteomics": 'proteomics[Title/Abstract]',
    "metabolomics": 'metabolomics[Title/Abstract]',
    "antibody_drug_conjugates":
        '"antibody-drug conjugate"[Title/Abstract] OR '
        '"antibody drug conjugate"[Title/Abstract]'
}

RESULTS_PER_TOPIC = 100
BATCH_SIZE = 50

OUTPUT_JSONL = "biomedical_papers.jsonl"
OUTPUT_CSV = "biomedical_papers.csv"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


def create_session():
    """Create HTTP session with retry support."""

    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)

    session = requests.Session()
    session.mount("https://", adapter)

    return session


SESSION = create_session()


def common_params():
    """Parameters recommended for NCBI requests."""

    params = {
        "tool": TOOL_NAME,
        "email": EMAIL
    }

    if API_KEY:
        params["api_key"] = API_KEY

    return params


def ncbi_delay():
    """
    Stay below the no-key NCBI request limit.

    With an API key this could be reduced, but keeping a
    conservative delay is appropriate for the initial MVP.
    """
    if API_KEY:
        time.sleep(0.12)
    else:
        time.sleep(0.35)


def search_pubmed(query, max_results=100):
    """
    Search PubMed and return matching PMID values.
    """

    url = f"{BASE_URL}/esearch.fcgi"

    params = {
        **common_params(),
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": max_results,
        "sort": "pub date"
    }

    logging.info("Searching PubMed: %s", query)

    response = SESSION.get(
        url,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    ncbi_delay()

    data = response.json()

    pmids = data.get(
        "esearchresult",
        {}
    ).get(
        "idlist",
        []
    )

    logging.info("Found %d PMID records", len(pmids))

    return pmids


def fetch_pubmed_articles(pmids):
    """
    Retrieve detailed PubMed records using EFetch.
    """

    if not pmids:
        return None

    url = f"{BASE_URL}/efetch.fcgi"

    params = {
        **common_params(),
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml"
    }

    response = SESSION.get(
        url,
        params=params,
        timeout=60
    )

    response.raise_for_status()

    ncbi_delay()

    return response.text


def get_text(element):
    """
    Extract all text contained inside an XML element.
    Useful because PubMed titles/abstracts may contain
    nested formatting elements.
    """

    if element is None:
        return ""

    return "".join(element.itertext()).strip()


def parse_pubmed_xml(xml_text, topic):
    """
    Convert PubMed XML into normalized dictionaries.
    """

    records = []

    if not xml_text:
        return records

    root = ET.fromstring(xml_text)

    for article_record in root.findall(".//PubmedArticle"):

        citation = article_record.find("MedlineCitation")

        if citation is None:
            continue

        article = citation.find("Article")

        if article is None:
            continue

        # PMID
        pmid = get_text(citation.find("PMID"))

        if not pmid:
            continue

        # Title
        title = get_text(article.find("ArticleTitle"))

        # Abstract
        abstract_parts = []

        abstract = article.find("Abstract")

        if abstract is not None:

            for section in abstract.findall("AbstractText"):

                text = get_text(section)

                label = section.attrib.get("Label")

                if label and text:
                    text = f"{label}: {text}"

                if text:
                    abstract_parts.append(text)

        abstract_text = " ".join(abstract_parts)

        # Authors
        authors = []

        author_list = article.find("AuthorList")

        if author_list is not None:

            for author in author_list.findall("Author"):

                collective = get_text(
                    author.find("CollectiveName")
                )

                if collective:
                    authors.append(collective)
                    continue

                first_name = get_text(
                    author.find("ForeName")
                )

                last_name = get_text(
                    author.find("LastName")
                )

                full_name = " ".join(
                    part for part in [first_name, last_name]
                    if part
                )

                if full_name:
                    authors.append(full_name)

        # Journal
        journal = ""

        journal_element = article.find("Journal")

        if journal_element is not None:
            journal = get_text(
                journal_element.find("Title")
            )

        # Publication date
        publication_date = ""

        if journal_element is not None:

            pub_date = journal_element.find(
                "JournalIssue/PubDate"
            )

            if pub_date is not None:

                year = get_text(pub_date.find("Year"))
                month = get_text(pub_date.find("Month"))
                day = get_text(pub_date.find("Day"))

                if year:
                    publication_date = "-".join(
                        value for value in [year, month, day]
                        if value
                    )
                else:
                    publication_date = get_text(
                        pub_date.find("MedlineDate")
                    )

        # DOI
        doi = ""

        article_id_list = article_record.find(
            "PubmedData/ArticleIdList"
        )

        if article_id_list is not None:

            for article_id in article_id_list.findall(
                "ArticleId"
            ):

                if article_id.attrib.get("IdType") == "doi":
                    doi = get_text(article_id)
                    break

        # MeSH terms
        mesh_terms = []

        mesh_heading_list = citation.find(
            "MeshHeadingList"
        )

        if mesh_heading_list is not None:

            for mesh_heading in mesh_heading_list.findall(
                "MeshHeading"
            ):

                descriptor = get_text(
                    mesh_heading.find("DescriptorName")
                )

                if descriptor:
                    mesh_terms.append(descriptor)

        # Publication types
        publication_types = []

        publication_type_list = article.find(
            "PublicationTypeList"
        )

        if publication_type_list is not None:

            for publication_type in (
                publication_type_list.findall(
                    "PublicationType"
                )
            ):

                value = get_text(publication_type)

                if value:
                    publication_types.append(value)

        record = {
            "pmid": pmid,
            "title": title,
            "abstract": abstract_text,
            "authors": authors,
            "journal": journal,
            "publication_date": publication_date,
            "doi": doi,
            "mesh_terms": mesh_terms,
            "publication_types": publication_types,
            "source": "PubMed",
            "topic": topic,
            "retrieved_at": datetime.now(
                timezone.utc
            ).isoformat()
        }

        records.append(record)

    return records


def collect_topic(topic_name, query):
    """
    Search and retrieve all records for one configured topic.
    """

    pmids = search_pubmed(
        query,
        RESULTS_PER_TOPIC
    )

    records = []

    for start in range(
        0,
        len(pmids),
        BATCH_SIZE
    ):

        batch = pmids[
            start:start + BATCH_SIZE
        ]

        logging.info(
            "Fetching %s records %d-%d",
            topic_name,
            start + 1,
            start + len(batch)
        )

        xml_text = fetch_pubmed_articles(batch)

        batch_records = parse_pubmed_xml(
            xml_text,
            topic_name
        )

        records.extend(batch_records)

    return records


def deduplicate(records):
    """
    Remove duplicate publications using PMID.

    If the same paper appears under multiple configured topics,
    its topics are merged.
    """

    unique = {}

    for record in records:

        pmid = record["pmid"]

        if pmid not in unique:

            record["topics"] = [record["topic"]]
            del record["topic"]

            unique[pmid] = record

        else:

            topic = record["topic"]

            if topic not in unique[pmid]["topics"]:
                unique[pmid]["topics"].append(topic)

    return list(unique.values())


def save_jsonl(records, filename):
    """
    Save normalized records as JSON Lines.
    """

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
    """
    Save a simplified CSV version for manual review.
    """

    fields = [
        "pmid",
        "title",
        "abstract",
        "authors",
        "journal",
        "publication_date",
        "doi",
        "mesh_terms",
        "publication_types",
        "source",
        "topics",
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

            csv_record = record.copy()

            csv_record["authors"] = "; ".join(
                record["authors"]
            )

            csv_record["mesh_terms"] = "; ".join(
                record["mesh_terms"]
            )

            csv_record["publication_types"] = "; ".join(
                record["publication_types"]
            )

            csv_record["topics"] = "; ".join(
                record["topics"]
            )

            writer.writerow(csv_record)


def main():

    logging.info(
        "Starting Biomedical Paper Explorer acquisition"
    )

    all_records = []

    for topic_name, query in TOPICS.items():

        try:

            records = collect_topic(
                topic_name,
                query
            )

            all_records.extend(records)

            logging.info(
                "%s: collected %d records",
                topic_name,
                len(records)
            )

        except Exception:
            logging.exception(
                "Failed to collect topic: %s",
                topic_name
            )

    records = deduplicate(all_records)

    save_jsonl(
        records,
        OUTPUT_JSONL
    )

    save_csv(
        records,
        OUTPUT_CSV
    )

    logging.info(
        "Acquisition complete."
    )

    logging.info(
        "Unique publications: %d",
        len(records)
    )

    logging.info(
        "JSONL output: %s",
        OUTPUT_JSONL
    )

    logging.info(
        "CSV output: %s",
        OUTPUT_CSV
    )


if __name__ == "__main__":
    main()
