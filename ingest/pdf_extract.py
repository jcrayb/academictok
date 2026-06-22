"""Fetch an open-access PDF (or HTML article page) and pull out its full text
and figure images.

PyMuPDF (``fitz``) does the heavy lifting for real PDFs: text per page, plus
embedded raster images which we size-filter (to drop logos/glyphs), normalize
to PNG with Pillow, and cap. A lot of Semantic Scholar's "open access PDF"
links, though, actually resolve to a regular HTML article page rather than a
PDF binary (Elsevier, Springer, MDPI, PMC, ...) — ``fetch_and_extract`` tries a
real PDF first and falls back to scraping the HTML article body. Everything
here is best-effort — callers fall back to the abstract when nothing could be
fetched or parsed.
"""
import io
import logging
import os
import re
from html import unescape
from urllib.parse import urljoin

import requests

import config

logger = logging.getLogger(__name__)

# A polite UA; some publishers block default python-requests.
_UA = "AcademicTok/1.0 (+research summarizer)"


def fetch_pdf(url: str) -> bytes | None:
    """Download a PDF, guarding on content-type and size. None on any failure."""
    if not url:
        return None
    try:
        resp = requests.get(
            url, headers={"User-Agent": _UA, "Accept": "application/pdf"},
            timeout=60, stream=True,
        )
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "").lower()
        if "pdf" not in ctype and not url.lower().endswith(".pdf"):
            logger.info("Skipping non-PDF content-type %r at %s", ctype, url)
            return None
        chunks, total = [], 0
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > config.PDF_MAX_BYTES:
                logger.info("PDF exceeds size cap (%d bytes): %s", total, url)
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    except requests.RequestException as e:
        logger.info("PDF fetch failed for %s: %s", url, e)
        return None


def fetch_html(url: str) -> tuple[bytes, str] | None:
    """Some publishers (Elsevier's linkinghub in particular) bounce through a
    ``<meta http-equiv="refresh">`` interstitial instead of a real HTTP
    redirect, which ``requests`` won't follow on its own — those are followed
    manually, up to a few hops, before the page is treated as final.

    Download an HTML article page, the same way ``fetch_pdf`` downloads a
    PDF (size-capped, guarded on content-type). Returns ``(html_bytes,
    final_url)`` — the final URL (after redirects) is needed to resolve the
    page's often-relative figure ``<img>`` srcs — or None on any failure.
    """
    if not url:
        return None
    for _ in range(_MAX_META_REFRESH_HOPS):
        try:
            resp = requests.get(
                url, headers={"User-Agent": _UA, "Accept": "text/html"},
                timeout=60, stream=True,
            )
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "").lower()
            if "html" not in ctype:
                logger.info("Skipping non-HTML content-type %r at %s", ctype, url)
                return None
            chunks, total = [], 0
            for chunk in resp.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > config.PDF_MAX_BYTES:
                    logger.info("HTML page exceeds size cap (%d bytes): %s", total, url)
                    return None
                chunks.append(chunk)
        except requests.RequestException as e:
            logger.info("HTML fetch failed for %s: %s", url, e)
            return None

        content = b"".join(chunks)
        target = _meta_refresh_target(content.decode(resp.encoding or "utf-8", "ignore"))
        if not target:
            return content, resp.url
        url = urljoin(resp.url, target)

    logger.info("Too many meta-refresh redirects, giving up at %s", url)
    return None


_MAX_META_REFRESH_HOPS = 3
# The content attribute's own value may itself use the *other* quote char
# (e.g. ``content="2; url='/foo?a=1&b=2'"``), so its closing quote is matched
# by backreference to whichever quote opened it, not just the next quote seen.
_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=(["\'])(.*?)\1',
    re.IGNORECASE,
)


def _meta_refresh_target(html_text: str) -> str | None:
    """Pull the target URL out of a ``<meta http-equiv="refresh" content="N;
    url=...">`` tag, or None if the page doesn't have one."""
    m = _META_REFRESH_RE.search(html_text)
    if not m:
        return None
    m2 = re.search(r"url\s*=\s*['\"]?([^'\">]+)", m.group(2), re.IGNORECASE)
    return unescape(m2.group(1)) if m2 else None


def fetch_and_extract(url: str, want_text: bool = True,
                       want_figures: bool = True) -> dict:
    """Fetch a paper's full text from its open-access link and extract it,
    trying a real PDF first and falling back to scraping an HTML article page
    when that's what the link actually serves (very common: Semantic
    Scholar's "open access PDF" field often points to a publisher's HTML
    full-text page, not an actual PDF binary).

    Returns ``{"text", "figures", "downloaded"}`` — same shape as ``extract``/
    ``extract_html``, plus ``downloaded`` so callers can tell "nothing to
    extract" apart from "couldn't even fetch the page".
    """
    if not url:
        return {"text": "", "figures": [], "downloaded": False}
    pdf_bytes = fetch_pdf(url)
    if pdf_bytes:
        result = extract(pdf_bytes, want_text=want_text, want_figures=want_figures)
        result["downloaded"] = True
        return result
    html = fetch_html(url)
    if html:
        html_bytes, final_url = html
        result = extract_html(html_bytes, base_url=final_url, want_text=want_text,
                               want_figures=want_figures)
        result["downloaded"] = True
        return result
    return {"text": "", "figures": [], "downloaded": False}


def extract(pdf_bytes: bytes, want_text: bool = True,
            want_figures: bool = True) -> dict:
    """Return ``{"text": str, "figures": [ {png, width, height, page}, ... ]}``.

    ``want_text`` / ``want_figures`` skip the half that's disabled so callers
    don't pay for work they'll discard. Figures cover both embedded raster images
    and — when ``config.ENABLE_VECTOR_FIGURES`` is on — rendered vector-graphic
    regions, which is where most scientific plots and diagrams actually live.
    Raises nothing for content problems; returns whatever could be parsed.
    """
    import fitz  # imported lazily so the app boots without PyMuPDF installed

    text_parts: list[str] = []
    figures: list[dict] = []
    seen_xrefs: set[int] = set()

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        logger.info("Could not open PDF: %s", e)
        return {"text": "", "figures": []}

    with doc:
        for page_index in range(doc.page_count):
            page = doc[page_index]
            if want_text:
                try:
                    text_parts.append(page.get_text("text"))
                except Exception:  # noqa: BLE001
                    pass
            if want_figures and len(figures) < config.MAX_FIGURES_PER_PAPER:
                _collect_figures(doc, page, page_index, seen_xrefs, figures)

    text = "\n".join(text_parts).strip()
    return {"text": text, "figures": figures}


# Chrome to strip outright before pulling article text — never part of the body.
_HTML_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside",
                    "noscript", "form", "iframe", "svg")
# Article-body containers across the major publisher/repository platforms
# (Elsevier, Springer, Wiley, MDPI, PMC, Frontiers, PLOS, ...), checked in
# order — first match wins. Falls back to <main>, then the whole page.
_ARTICLE_XPATHS = (
    # Specific body containers first — sites like Springer/Nature wrap the
    # *entire* page (including "similar articles"/recommendation widgets) in
    # a single <article>, so a generic <article> match would pull in that
    # noise; their actual body class (e.g. c-article-body) doesn't.
    "//*[@id='body']",
    "//*[contains(@class, 'article-body')]",
    "//*[contains(@class, 'articleBody')]",
    "//*[contains(@class, 'fulltext-view')]",
    "//*[contains(@class, 'c-article-body')]",
    "//*[contains(@class, 'article__body')]",
    "//article",
    "//main",
)


def extract_html(html_bytes: bytes, base_url: str = "", want_text: bool = True,
                  want_figures: bool = True) -> dict:
    """Pull full text + figures out of an HTML article page — a "PDF" link
    that actually serves the full text as a regular web page rather than a PDF
    binary. Mirrors ``extract()``'s return shape so callers can use either
    fetch path interchangeably (see ``fetch_and_extract``).
    """
    import lxml.html  # imported lazily so the app boots without lxml installed

    try:
        tree = lxml.html.fromstring(html_bytes)
    except Exception as e:  # noqa: BLE001
        logger.info("Could not parse HTML: %s", e)
        return {"text": "", "figures": []}

    for tag in _HTML_STRIP_TAGS:
        for el in tree.xpath(f"//{tag}"):
            el.drop_tree()

    container = tree
    for xp in _ARTICLE_XPATHS:
        found = tree.xpath(xp)
        if found:
            container = found[0]
            break

    text = ""
    if want_text:
        text = container.text_content()
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()

    figures = _collect_html_figures(container, base_url) if want_figures else []
    return {"text": text, "figures": figures}


def _collect_html_figures(container, base_url: str) -> list[dict]:
    """Download and size-filter this page's <img> figures, same dims/format
    rules as PDF-embedded images. Best-effort: a broken image is just skipped."""
    out = []
    for img in container.xpath(".//img"):
        if len(out) >= config.MAX_FIGURES_PER_PAPER:
            break
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        blob = _fetch_image(urljoin(base_url, src))
        if blob is None:
            continue
        png = _normalize_image_bytes(blob)
        if png is None:
            continue
        out.append(png)
    return out


def _fetch_image(url: str) -> bytes | None:
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=20)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException:
        return None


def _collect_figures(doc, page, page_index: int, seen_xrefs: set, out: list):
    """Append this page's figures (raster, then vector) to ``out`` in place.

    Stops at the per-paper cap. Vector regions are deduped against raster images
    already captured on the same page, so a figure isn't grabbed twice (once as
    an embedded image and once as the vector drawing wrapped around it).
    """
    cap = config.MAX_FIGURES_PER_PAPER
    taken = []  # fitz.Rect of figures already captured on this page

    # Embedded raster images (photos, screenshots, pre-rasterized plots).
    for img in page.get_images(full=True):
        if len(out) >= cap:
            return
        xref = img[0]
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        png = _normalize_image(doc, xref)
        if png is None:
            continue
        out.append({**png, "page": page_index + 1})
        taken.extend(_safe_image_rects(page, xref))

    # Vector graphics (most plots/diagrams in scientific PDFs). Cluster the page's
    # drawing operators into figure-sized regions and rasterize each to PNG.
    if not config.ENABLE_VECTOR_FIGURES:
        return
    try:
        clusters = page.cluster_drawings()
    except Exception:  # noqa: BLE001 - guard older/edge PyMuPDF builds
        return
    for rect in clusters:
        if len(out) >= cap:
            return
        if _overlaps_any(rect, taken):
            continue
        rendered = _render_region(page, rect)
        if rendered is None:
            continue
        out.append({**rendered, "page": page_index + 1})
        taken.append(rect)


def _passes_dims(w: int, h: int) -> bool:
    """Shared size + aspect filter: drop logos, glyphs, rules and banners."""
    if w < config.MIN_FIGURE_DIM or h < config.MIN_FIGURE_DIM:
        return False
    ratio = w / h if h else 999
    return 1 / 6 <= ratio <= 6


def _safe_image_rects(page, xref: int) -> list:
    """Bounding box(es) of an embedded image on the page; [] if unknown."""
    try:
        return list(page.get_image_rects(xref))
    except Exception:  # noqa: BLE001
        return []


def _overlaps_any(rect, others, thresh: float = 0.5) -> bool:
    """True if ``rect`` overlaps any of ``others`` by >thresh of the smaller area."""
    a = rect.width * rect.height
    for o in others:
        inter = rect & o
        if inter.is_empty:
            continue
        smaller = min(a, o.width * o.height)
        if smaller > 0 and (inter.width * inter.height) / smaller > thresh:
            return True
    return False


def _render_region(page, rect) -> dict | None:
    """Rasterize a page region (a vector-figure bounding box) to PNG bytes.

    Drops near-full-page regions (page backgrounds/frames) and anything failing
    the shared size/aspect filters.
    """
    page_rect = page.rect
    page_area = page_rect.width * page_rect.height
    if page_area and (rect.width * rect.height) / page_area > 0.9:
        return None
    clip = rect & page_rect  # keep the clip within the page bounds
    if clip.is_empty:
        return None
    try:
        pix = page.get_pixmap(clip=clip, dpi=config.FIGURE_RENDER_DPI, alpha=False)
    except Exception:  # noqa: BLE001
        return None
    if not _passes_dims(pix.width, pix.height):
        return None
    return {"png": pix.tobytes("png"), "width": pix.width, "height": pix.height}


def _normalize_image(doc, xref: int) -> dict | None:
    """Decode one PDF-embedded image to RGB PNG bytes, or None if too small/bad."""
    try:
        raw = doc.extract_image(xref)
    except Exception:  # noqa: BLE001
        return None
    blob = raw.get("image")
    if not blob:
        return None
    return _normalize_image_bytes(blob)


def _normalize_image_bytes(blob: bytes) -> dict | None:
    """Decode raw image bytes (PDF-embedded or downloaded from an HTML page)
    to a size-filtered RGB PNG, or None if too small/bad/undecodable."""
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(blob))
        im.load()
    except Exception:  # noqa: BLE001
        return None

    w, h = im.size
    if not _passes_dims(w, h):
        return None

    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, format="PNG", optimize=True)
    return {"png": out.getvalue(), "width": w, "height": h}


def save_figures(s2_paper_id: str, figures: list[dict]) -> list[dict]:
    """Write figure PNGs under MEDIA_DIR/figures and return metadata rows.

    Each returned dict: ``{filename, page, width, height, ord}`` (relative
    filename, served at /media/figures/<filename>).
    """
    out_dir = os.path.join(config.MEDIA_DIR, "figures")
    os.makedirs(out_dir, exist_ok=True)
    safe_id = "".join(c for c in s2_paper_id if c.isalnum())[:48] or "paper"
    rows = []
    for i, fig in enumerate(figures):
        filename = f"{safe_id}_{i}.png"
        with open(os.path.join(out_dir, filename), "wb") as fh:
            fh.write(fig["png"])
        rows.append({
            "filename": filename,
            "page": fig.get("page"),
            "width": fig.get("width"),
            "height": fig.get("height"),
            "ord": i,
        })
    return rows
