"""Summarize a paper into a doomscroll-friendly post via the local LLM."""
import logging

import config
from ingest.ollama_client import generate_json, generate_text

logger = logging.getLogger(__name__)

SYSTEM = (
    "You are an editor for a feed of bite-sized academic paper summaries that "
    "researchers scroll through for fun. You write punchy, accurate, jargon-light "
    "summaries that make papers approachable without dumbing them down."
)

PROMPT = """Summarize the following paper as a feed post.

Title: {title}
Venue: {venue} ({year})
Abstract: {abstract}

Return a JSON object with exactly these keys:
- "title": a short, catchy headline for the post (max ~12 words, no clickbait, \
faithful to the paper).
- "body": 2-4 sentences explaining what the paper does and why it matters, \
written for a curious researcher outside the subfield.

Return only the JSON object."""


def summarize(paper: dict, model: str | None = None) -> dict:
    """Given a normalized paper dict, return {title, body, model}.

    ``model`` overrides the default (e.g. the small fast model for live
    discovery); the returned ``model`` records which one actually wrote it.
    """
    prompt = PROMPT.format(
        title=paper["title"],
        venue=paper.get("venue") or "n/a",
        year=paper.get("year") or "n/a",
        abstract=paper["abstract"],
    )
    result = generate_json(prompt, system=SYSTEM, temperature=0.3, model=model)
    title = (result.get("title") or "").strip()
    body = (result.get("body") or "").strip()
    if not title or not body:
        raise ValueError("Summarizer returned empty title or body")
    return {"title": title, "body": body, "model": model or config.OLLAMA_MODEL}


DETAIL_SYSTEM = (
    "You are an expert research summarizer. You write thorough, accurate, "
    "well-structured explainers of academic papers for a graduate-level reader. "
    "You stay faithful to the source and never fabricate specific numbers."
)

# Shared instructions for both the abstract-only and full-text variants. The
# {equations} slot is filled per-paper with one of the guidance blocks below.
_DETAIL_TASK = """Cover, in clear prose with short paragraphs:
1. The problem and why it matters.
2. The core idea, approach, and methodology.
3. The main results and findings.
4. Practical implications and how the work could be used.
5. Notable limitations or open questions (only if reasonably inferable).

Write 5-8 short paragraphs. {equations}

Return a JSON object with one key:
- "long_body": the full summary, paragraphs separated by blank lines (\\n\\n), \
with any LaTeX kept verbatim inside the delimiters.

Return only the JSON object."""

# Equation guidance swapped into {equations} by topic. Both forbid fabrication;
# the math variant treats equations as the substance rather than a garnish.
_EQ_DEFAULT = (
    "Where the source presents key equations — a loss function, model "
    "definition, update rule, core theorem, etc. — reproduce them so the reader "
    "sees the actual math, using MathJax/LaTeX delimiters: inline math as "
    "\\( ... \\) and display math on its own line as $$ ... $$. Favour including "
    "the one or two equations central to the method over leaving them out, but "
    "copy them faithfully from the source and never invent notation, symbols, or "
    "numbers that aren't present. If the source genuinely contains no equations, "
    "don't add any."
)
_EQ_MATH = (
    "This is a mathematics or applied-mathematics paper, so equations ARE the "
    "content — treat them as first-class, not an optional garnish. Reproduce the "
    "central definitions, theorems, lemmas, and the key steps of important "
    "derivations using MathJax/LaTeX delimiters: inline math as \\( ... \\) and "
    "display math on its own line as $$ ... $$. Be generous and precise: the "
    "reader should come away seeing the actual mathematical objects and "
    "relationships, not just prose about them. Copy every symbol, subscript, and "
    "operator faithfully from the source, and never invent notation or numbers "
    "that aren't present."
)

# Substring cues for equation-heavy topics. Kept lowercase; matched against the
# query/field/title. "math" also covers mathematics/mathematical/applied math.
_MATH_TERMS = (
    "math", "algebra", "geometr", "topolog", "manifold", "tensor",
    "number theory", "combinator", "graph theory", "category theory",
    "set theory", "measure theory", "functional analysis", "numerical analysis",
    "real analysis", "complex analysis", "harmonic analysis", "probability",
    "statistic", "stochastic", "optimization", "differential equation",
    "partial differential", "dynamical system", "theorem", "fluid dynamics",
    "control theory", "information theory", "game theory", "cryptograph",
    "operator algebra", "quantum", "relativity", "theoretical physics",
    "mathematical physics", "pde",
)


def _is_mathy(text: str) -> bool:
    """Heuristic: does this topic look math / applied-math (equation-heavy)?"""
    t = (text or "").lower()
    return any(term in t for term in _MATH_TERMS)

DETAIL_PROMPT_ABSTRACT = """Write a thorough, detailed summary of this paper \
for a reader who wants to understand it without reading the full text.

Title: {title}
Venue: {venue} ({year})
Abstract: {abstract}

""" + _DETAIL_TASK

DETAIL_PROMPT_FULLTEXT = """Write a thorough, detailed summary of this paper \
grounded in its full text below. Prefer concrete details (methods, datasets, \
key results, equations) that appear in the text over generic statements.

Title: {title}
Venue: {venue} ({year})

--- BEGIN PAPER TEXT ---
{full_text}
--- END PAPER TEXT ---

""" + _DETAIL_TASK


def detailed_summary(paper: dict, full_text: str | None = None,
                     topic_hint: str = "") -> str:
    """Generate a long, thorough summary string for a paper.

    When ``full_text`` (from the open-access PDF) is given, the summary is
    grounded in it; otherwise it falls back to the abstract. ``topic_hint`` (the
    search query or field name) steers the equation guidance: math / applied-math
    papers get a much stronger "show the equations" instruction.
    """
    mathy = _is_mathy(f"{topic_hint} {paper.get('title', '')} {paper.get('venue', '')}")
    equations = _EQ_MATH if mathy else _EQ_DEFAULT
    if full_text and full_text.strip():
        prompt = DETAIL_PROMPT_FULLTEXT.format(
            title=paper["title"],
            venue=paper.get("venue") or "n/a",
            year=paper.get("year") or "n/a",
            full_text=full_text[: config.PDF_TEXT_CHAR_LIMIT],
            equations=equations,
        )
    else:
        prompt = DETAIL_PROMPT_ABSTRACT.format(
            title=paper["title"],
            venue=paper.get("venue") or "n/a",
            year=paper.get("year") or "n/a",
            abstract=paper["abstract"],
            equations=equations,
        )
    result = generate_json(prompt, system=DETAIL_SYSTEM, temperature=0.3)
    long_body = (result.get("long_body") or "").strip()
    if not long_body:
        raise ValueError("Detailed summarizer returned empty long_body")
    return long_body


CAPTION_SYSTEM = (
    "You write one-sentence captions describing figures from academic papers. "
    "Be concrete and faithful; if the figure is unclear, say what it appears to show."
)


def caption_figure(image_png: bytes, paper_title: str) -> str | None:
    """Caption a figure with the configured vision model. None if disabled/failed.

    No-op unless config.VISION_MODEL is set to a vision-capable Ollama model.
    """
    if not config.VISION_MODEL:
        return None
    prompt = (
        f"This figure is from the paper titled \"{paper_title}\". "
        "In one sentence, describe what the figure shows."
    )
    try:
        text = generate_text(
            prompt, system=CAPTION_SYSTEM, images=[image_png],
            model=config.VISION_MODEL, temperature=0.2,
        )
        return text.strip() or None
    except Exception as e:  # noqa: BLE001 - captioning is optional
        logger.warning("Figure captioning failed: %s", e)
        return None
