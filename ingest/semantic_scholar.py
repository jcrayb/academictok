"""Semantic Scholar API client with process-wide 1 req/sec rate limiting."""
import logging
import threading
import time

import requests

import config

logger = logging.getLogger(__name__)

# Fields we request from the paper search endpoint.
PAPER_FIELDS = (
    "paperId,title,abstract,authors,year,venue,url,citationCount,openAccessPdf"
)

# Process-wide gate so we never exceed the documented 1 req/sec limit, even if
# multiple ingestion threads share this module.
_rate_lock = threading.Lock()
_last_request_ts = 0.0


def _rate_limit() -> None:
    global _last_request_ts
    with _rate_lock:
        elapsed = time.monotonic() - _last_request_ts
        wait = config.SEMANTIC_SCHOLAR_MIN_INTERVAL - elapsed
        if wait > 0:
            time.sleep(wait)
        _last_request_ts = time.monotonic()


def _headers() -> dict:
    headers = {"Accept": "application/json"}
    if config.SEMANTIC_SCHOLAR_API_KEY:
        headers["x-api-key"] = config.SEMANTIC_SCHOLAR_API_KEY
    return headers


def _get(path: str, params: dict, max_retries: int = 4) -> dict:
    """GET with rate limiting and backoff on 429/5xx. Returns parsed JSON."""
    url = f"{config.SEMANTIC_SCHOLAR_BASE}{path}"
    backoff = 5
    for attempt in range(1, max_retries + 1):
        _rate_limit()
        try:
            resp = requests.get(url, params=params, headers=_headers(), timeout=30)
        except requests.RequestException as e:
            logger.warning("S2 request error (attempt %d): %s", attempt, e)
            if attempt == max_retries:
                raise
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            logger.warning("S2 %d (attempt %d); backing off %.1fs",
                           resp.status_code, attempt, backoff)
            if attempt == max_retries:
                resp.raise_for_status()
            time.sleep(backoff)
            backoff *= 2
            continue
        resp.raise_for_status()
    return {}


def search_papers(query: str, limit: int = 20, min_citations: int = 0,
                  offset: int = 0):
    """Search for relevant papers.

    Returns a list of normalized paper dicts. Papers without an abstract are
    dropped (they can't be summarized). ``offset`` pages through the relevance
    ranking (used by field discovery to keep finding fresh papers).
    """
    data = _get(
        "/paper/search",
        {"query": query, "limit": limit, "offset": offset, "fields": PAPER_FIELDS},
    )
    out = []
    for p in data.get("data", []) or []:
        if not p.get("abstract") or not p.get("title"):
            continue
        if (p.get("citationCount") or 0) < min_citations:
            continue
        out.append(normalize(p))
    # Favor "important" papers: most-cited first.
    out.sort(key=lambda x: x["citation_count"], reverse=True)
    return out


def normalize(p: dict) -> dict:
    """Map a raw S2 paper object to our internal shape."""
    authors = [a.get("name", "") for a in (p.get("authors") or []) if a.get("name")]
    oa = p.get("openAccessPdf") or {}
    return {
        "s2_paper_id": p.get("paperId"),
        "title": p.get("title") or "",
        "abstract": p.get("abstract") or "",
        "authors": authors,
        "year": p.get("year"),
        "venue": p.get("venue") or "",
        "url": p.get("url") or "",
        "citation_count": p.get("citationCount") or 0,
        "pdf_url": (oa.get("url") or "") or None,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "stochastic optimization"
    papers = search_papers(q, limit=5)
    print(f"Found {len(papers)} papers for '{q}':")
    for p in papers:
        print(f"  [{p['citation_count']:>5}] {p['year']} — {p['title'][:80]}")
