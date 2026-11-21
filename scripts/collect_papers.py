"""Download open-access papers and their metadata onto a local drive.

Each paper is stored under its topic folder:

    <date>_paper/<topic>/<year>_<month>/<day>/<openalex_id>.pdf
    <date>_paper/<topic>/<year>_<month>/<day>/<openalex_id>.json

Each topic stops after 1,500 saved PDFs, then the next topic starts.

Papers are requested newest first. Progress is written to status.json in the
archive folder after each paper, so a stopped run continues from the same
search page when the command is started again.

The OpenAlex work id (for example W2899594259) is unique per paper and safe
as a Windows file name. DOI is also unique, but it contains slashes, so it is
stored inside the JSON instead of used as the file name.

Semantic Scholar's 40-character paper id is unique too, but the unauthenticated
API is rate-limited from many networks. This collector uses OpenAlex, which
answers without an API key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

import httpx

logger = logging.getLogger("collect")

_OPENALEX = "https://api.openalex.org/works"
_WORK_ID = re.compile(r"^W\d+$")
_MAX_PDF_BYTES = 40 * 1024 * 1024
_SELECT = ",".join([
    "id",
    "doi",
    "display_name",
    "publication_year",
    "publication_date",
    "authorships",
    "abstract_inverted_index",
    "primary_location",
    "best_oa_location",
    "locations",
    "open_access",
    "cited_by_count",
    "type",
    "biblio",
])

TOPICS: list[dict] = [
    {
        "name": "hydro-thermal",
        "queries": [
            "hydropower plant efficiency retrofit turbine upgrade",
            "hydroelectric turbine runner replacement capacity increase",
            "thermal power plant heat rate improvement retrofit",
            "steam turbine retrofit efficiency upgrade",
            "combined cycle power plant efficiency improvement",
            "boiler combustion optimization thermal power plant",
        ],
    },
    {
        "name": "rice-wheat",
        "queries": [
            "rice yield increase nitrogen use efficiency",
            "rice production cost reduction cultivation technology",
            "system of rice intensification yield",
            "wheat yield improvement agronomic practices",
            "wheat production cost reduction precision agriculture",
            "conservation agriculture wheat rice yield",
        ],
    },
    {
        "name": "dairy",
        "queries": [
            "dairy processing technology milk",
            "milk pasteurization membrane filtration",
            "cheese whey processing technology",
            "yogurt fermentation process optimization",
            "milk powder spray drying technology",
        ],
    },
]


def default_archive_dir() -> Path:
    return Path("D:/") / f"{date.today().isoformat()}_paper"


def work_file_id(work_id: str) -> str:
    """Return the unique OpenAlex work id, without the URL prefix."""
    short = (work_id or "").rstrip("/").rsplit("/", 1)[-1].strip()
    if _WORK_ID.fullmatch(short):
        return short
    return ""


def abstract_text(inverted: dict | None) -> str:
    if not inverted:
        return ""
    positions = [pos for indexes in inverted.values() for pos in indexes]
    if not positions:
        return ""
    words = [""] * (max(positions) + 1)
    for word, indexes in inverted.items():
        for pos in indexes:
            if 0 <= pos < len(words):
                words[pos] = word
    return " ".join(word for word in words if word)


def pdf_candidates(work: dict) -> list[str]:
    urls: list[str] = []
    for key in ("best_oa_location", "primary_location"):
        location = work.get(key) or {}
        url = (location.get("pdf_url") or "").strip()
        if url:
            urls.append(url)
    oa_url = ((work.get("open_access") or {}).get("oa_url") or "").strip()
    if oa_url:
        urls.append(oa_url)
    for location in work.get("locations") or []:
        url = (location.get("pdf_url") or "").strip()
        if url:
            urls.append(url)
    unique: list[str] = []
    for url in urls:
        if url not in unique:
            unique.append(url)
    return unique


def to_record(work: dict, pdf_url: str) -> dict:
    authors = []
    for authorship in work.get("authorships") or []:
        name = ((authorship.get("author") or {}).get("display_name") or "").strip()
        if name:
            authors.append(name)
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    biblio = work.get("biblio") or {}
    journal_parts = [source.get("display_name") or ""]
    if biblio.get("volume"):
        journal_parts.append(f"vol. {biblio['volume']}")
    if biblio.get("first_page") or biblio.get("last_page"):
        pages = biblio.get("first_page") or ""
        if biblio.get("last_page"):
            pages = f"{pages}-{biblio['last_page']}" if pages else biblio["last_page"]
        journal_parts.append(f"pp. {pages}")
    doi = (work.get("doi") or "").removeprefix("https://doi.org/")
    paper_id = work_file_id(work.get("id") or "")
    return {
        "archive_id": paper_id,
        "openalex_id": paper_id,
        "title": work.get("display_name") or "",
        "authors": authors,
        "year": work.get("publication_year") or 0,
        "publication_date": work.get("publication_date") or "",
        "venue": source.get("display_name") or "",
        "journal": ", ".join(part for part in journal_parts if part),
        "abstract": abstract_text(work.get("abstract_inverted_index")),
        "doi": doi,
        "citation_count": work.get("cited_by_count") or 0,
        "publication_type": work.get("type") or "",
        "is_open_access": bool((work.get("open_access") or {}).get("is_oa")),
        "oa_status": (work.get("open_access") or {}).get("oa_status") or "",
        "open_access_pdf_url": pdf_url,
        "external_urls": {
            "openalex": work.get("id") or "",
            "doi_url": f"https://doi.org/{doi}" if doi else "",
        },
        "source": "openalex",
    }


def _merge_lists(existing: list, extra: list) -> list:
    merged: list = []
    for item in list(existing) + list(extra):
        if item and item not in merged:
            merged.append(item)
    return merged


def write_json(path: Path, record: dict, topic: str, query: str) -> None:
    payload = dict(record)
    topics = [topic]
    queries = [query]
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
        topics = _merge_lists(previous.get("collection_topics", []), topics)
        queries = _merge_lists(previous.get("collection_queries", []), queries)
    payload["collection_topics"] = topics
    payload["collection_queries"] = queries
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class TransientError(Exception):
    """Network failed before this paper or page could be finished."""


def status_path(archive_dir: Path) -> Path:
    return archive_dir / "status.json"


def load_progress(archive_dir: Path) -> dict:
    path = status_path(archive_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    progress: dict = {}
    for topic, info in (data.get("topics") or {}).items():
        progress[topic] = dict(info.get("queries") or {})
    return progress


_PUBLISHED = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def paper_dir(topic_dir: Path, publication_date: str, year: int = 0) -> Path:
    """Return topic/<year>_<month>/<day> for a paper's publication date."""
    match = _PUBLISHED.match(publication_date or "")
    if match:
        return topic_dir / f"{match.group(1)}_{match.group(2)}" / match.group(3)
    if year:
        return topic_dir / f"{int(year):04d}_00" / "00"
    return topic_dir / "unknown" / "00"


def relocate_loose_papers(topic_dir: Path) -> None:
    """Move PDFs that sit directly in the topic folder into year_month/day."""
    if not topic_dir.exists():
        return
    for pdf in list(topic_dir.glob("*.pdf")):
        meta = pdf.with_suffix(".json")
        if not meta.exists():
            continue
        try:
            record = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        dest = paper_dir(
            topic_dir,
            record.get("publication_date") or "",
            int(record.get("year") or 0),
        )
        dest.mkdir(parents=True, exist_ok=True)
        pdf.replace(dest / pdf.name)
        meta.replace(dest / meta.name)


def coverage_for(topic_dir: Path) -> dict:
    dates: list[str] = []
    saved = 0
    if topic_dir.exists():
        for path in topic_dir.rglob("*.json"):
            saved += 1
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            published = record.get("publication_date") or ""
            if not published and record.get("year"):
                published = str(record["year"])
            if published:
                dates.append(published)
    return {
        "saved": saved,
        "newest": max(dates) if dates else "",
        "oldest": min(dates) if dates else "",
    }


def build_status(archive_dir: Path, progress: dict, per_topic: int) -> dict:
    topics: dict = {}
    for topic in TOPICS:
        info = coverage_for(archive_dir / topic["name"])
        info["queries"] = progress.get(topic["name"]) or {}
        topics[topic["name"]] = info
    return {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "sort": "publication_date:desc",
        "per_topic": per_topic,
        "topics": topics,
    }


def write_status(archive_dir: Path, progress: dict, per_topic: int) -> dict:
    status = build_status(archive_dir, progress, per_topic)
    path = status_path(archive_dir)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return status


def format_status(status: dict, per_topic: int) -> str:
    lines = ["Download status (newest papers first)"]
    totals = ["Downloaded totals by topic"]
    for topic in TOPICS:
        info = (status.get("topics") or {}).get(topic["name"]) or {}
        saved = info.get("saved") or 0
        totals.append(f"  {topic['name']}: {saved}")
        if saved == 0:
            lines.append(f"  {topic['name']}: 0/{per_topic}")
            continue
        newest = info.get("newest") or "?"
        oldest = info.get("oldest") or "?"
        span = newest if newest == oldest else f"{newest} through {oldest}"
        reached = "complete" if saved >= per_topic else "in progress"
        lines.append(f"  {topic['name']}: {saved}/{per_topic} papers, {span} ({reached})")
    lines.append(f"Last recorded: {status.get('updated_at') or ''}")
    lines.append("")
    lines.extend(totals)
    return "\n".join(lines)


def query_state(progress: dict, topic: str, query: str) -> dict:
    topic_queries = progress.setdefault(topic, {})
    return topic_queries.setdefault(
        query,
        {"cursor": "*", "finished_ids": [], "collected": 0, "exhausted": False},
    )


async def search_page(
    client: httpx.AsyncClient,
    query: str,
    per_page: int,
    cursor: str,
) -> tuple[list[dict], str | None]:
    response = await client.get(
        _OPENALEX,
        params={
            "search": query,
            "filter": "from_publication_date:2015-01-01,is_oa:true",
            "sort": "publication_date:desc",
            "per-page": per_page,
            "cursor": cursor or "*",
            "select": _SELECT,
        },
        timeout=40,
    )
    response.raise_for_status()
    payload = response.json()
    nxt = (payload.get("meta") or {}).get("next_cursor") or None
    return payload.get("results") or [], nxt


def remove_empty_dirs(path: Path, stop: Path) -> None:
    """Delete path and empty parents, but never delete the topic folder itself."""
    current = path
    while current != stop and current.exists() and current.is_dir():
        if any(current.iterdir()):
            return
        parent = current.parent
        current.rmdir()
        current = parent


async def download_pdf(client: httpx.AsyncClient, urls: list[str]) -> tuple[bytes, str] | None:
    """Return PDF bytes, or None when this paper's file cannot be saved."""
    if not urls:
        return None
    for url in urls[:4]:
        try:
            response = await client.get(url, timeout=25)
        except httpx.HTTPError as exc:
            logger.warning("PDF request failed: %s (%s)", url, exc)
            continue
        if response.status_code != 200:
            logger.warning("PDF HTTP %s: %s", response.status_code, url)
            continue
        data = response.content
        if data and len(data) <= _MAX_PDF_BYTES and b"%PDF" in data[:1024]:
            return data, url
        logger.warning("Response is not a PDF: %s", url)
    return None


async def collect(
    archive_dir: Path,
    per_topic: int,
    topic_names: set[str] | None,
    max_queries: int | None,
) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    for partial in archive_dir.rglob("*.pdf.part"):
        partial.unlink(missing_ok=True)
    for topic in TOPICS:
        topic_dir = archive_dir / topic["name"]
        relocate_loose_papers(topic_dir)
        remove_empty_dirs(topic_dir, topic_dir)
        for directory in sorted(topic_dir.rglob("*"), reverse=True) if topic_dir.exists() else []:
            if directory.is_dir():
                remove_empty_dirs(directory, topic_dir)
    progress = load_progress(archive_dir)
    counts = {"saved": 0, "updated": 0, "failed": 0}
    headers = {
        "User-Agent": "academic-paper-scraper/1.0 (local archive)",
        "Accept": "application/json",
    }
    pdf_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,*/*",
    }

    def checkpoint() -> None:
        status = write_status(archive_dir, progress, per_topic)
        logger.info("\n%s", format_status(status, per_topic))

    logger.info("\n%s", format_status(build_status(archive_dir, progress, per_topic), per_topic))
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=headers) as search_client:
            async with httpx.AsyncClient(follow_redirects=True, headers=pdf_headers) as pdf_client:
                for topic in TOPICS:
                    if topic_names and topic["name"] not in topic_names:
                        continue
                    queries = topic["queries"][:max_queries] if max_queries else topic["queries"]
                    topic_dir = archive_dir / topic["name"]
                    topic_dir.mkdir(parents=True, exist_ok=True)
                    saved_pdfs = {pdf.stem: pdf for pdf in topic_dir.rglob("*.pdf")}
                    logger.info("Topic %s (%d/%d)", topic["name"], len(saved_pdfs), per_topic)
                    if len(saved_pdfs) >= per_topic:
                        logger.info("Topic %s already has %d papers", topic["name"], len(saved_pdfs))
                        continue
                    for query in queries:
                        if len(saved_pdfs) >= per_topic:
                            logger.info("Topic %s reached %d", topic["name"], per_topic)
                            break
                        state = query_state(progress, topic["name"], query)
                        if state.get("exhausted"):
                            logger.info("Skip finished query: %s", query)
                            continue
                        logger.info("Query: %s", query)
                        while len(saved_pdfs) < per_topic and not state.get("exhausted"):
                            page_size = min(25, per_topic - len(saved_pdfs))
                            try:
                                works, nxt = await search_page(
                                    search_client,
                                    query,
                                    page_size,
                                    state.get("cursor") or "*",
                                )
                            except httpx.HTTPError as exc:
                                raise TransientError(f"Search failed for {query}: {exc}") from exc
                            if not works:
                                state["exhausted"] = True
                                state["finished_ids"] = []
                                checkpoint()
                                break
                            logger.info("Found %d open-access works", len(works))
                            finished = set(state.get("finished_ids") or [])
                            page_done = True
                            for work in works:
                                if len(saved_pdfs) >= per_topic:
                                    page_done = False
                                    break
                                paper_id = work_file_id(work.get("id") or "")
                                if not paper_id or paper_id in finished:
                                    continue
                                published = str(work.get("publication_date") or work.get("publication_year") or "")
                                dest_dir = paper_dir(
                                    topic_dir,
                                    work.get("publication_date") or "",
                                    int(work.get("publication_year") or 0),
                                )
                                pdf_path = dest_dir / f"{paper_id}.pdf"
                                json_path = dest_dir / f"{paper_id}.json"
                                title = (work.get("display_name") or "")[:80]
                                existing = saved_pdfs.get(paper_id)
                                if existing and existing.exists():
                                    existing_json = existing.with_suffix(".json")
                                    if existing != pdf_path:
                                        dest_dir.mkdir(parents=True, exist_ok=True)
                                        existing.replace(pdf_path)
                                        if existing_json.exists():
                                            existing_json.replace(json_path)
                                        saved_pdfs[paper_id] = pdf_path
                                    record = to_record(work, "")
                                    if json_path.exists():
                                        previous = json.loads(json_path.read_text(encoding="utf-8"))
                                        record["open_access_pdf_url"] = previous.get("open_access_pdf_url") or ""
                                    write_json(json_path, record, topic["name"], query)
                                    counts["updated"] += 1
                                    logger.info("updated %s %s | %s", published, paper_id, title)
                                else:
                                    downloaded = await download_pdf(pdf_client, pdf_candidates(work))
                                    if downloaded is None:
                                        counts["failed"] += 1
                                        logger.info("failed %s %s | %s", published, paper_id, title)
                                        remove_empty_dirs(dest_dir, topic_dir)
                                    else:
                                        dest_dir.mkdir(parents=True, exist_ok=True)
                                        pdf_bytes, used_url = downloaded
                                        partial = pdf_path.with_suffix(".pdf.part")
                                        partial.write_bytes(pdf_bytes)
                                        partial.replace(pdf_path)
                                        write_json(
                                            json_path,
                                            to_record(work, used_url),
                                            topic["name"],
                                            query,
                                        )
                                        saved_pdfs[paper_id] = pdf_path
                                        counts["saved"] += 1
                                        logger.info(
                                            "saved %s %s | %s (%d/%d)",
                                            published,
                                            paper_id,
                                            title,
                                            len(saved_pdfs),
                                            per_topic,
                                        )
                                finished.add(paper_id)
                                state["finished_ids"] = sorted(finished)
                                state["collected"] = state.get("collected", 0) + 1
                                checkpoint()
                            if page_done:
                                state["cursor"] = nxt or "*"
                                state["finished_ids"] = []
                                if not nxt:
                                    state["exhausted"] = True
                                checkpoint()
                            await asyncio.sleep(1.0)
                            if state.get("exhausted") or not page_done:
                                break
    except (TransientError, KeyboardInterrupt) as exc:
        logger.error("Stopped. Run the same command again to continue. (%s)", exc)
    finally:
        checkpoint()

    logger.info(
        "Done. saved=%d updated=%d failed=%d dir=%s",
        counts["saved"],
        counts["updated"],
        counts["failed"],
        archive_dir,
    )


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Archive open-access papers locally.")
    parser.add_argument("--dest", type=Path, default=default_archive_dir())
    parser.add_argument("--per-topic", type=int, default=1500)
    parser.add_argument(
        "--topic",
        action="append",
        default=[],
        help="Limit to a topic name. Repeat to select more than one.",
    )
    parser.add_argument(
        "--max-queries",
        type=int,
        default=0,
        help="Use only the first N queries of each topic. 0 means all.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print how far the archive has been downloaded, then exit.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.status:
        progress = load_progress(args.dest)
        print(format_status(build_status(args.dest, progress, args.per_topic), args.per_topic))
        return
    topic_names = set(args.topic) if args.topic else None
    asyncio.run(collect(args.dest, args.per_topic, topic_names, args.max_queries or None))


if __name__ == "__main__":
    main()
