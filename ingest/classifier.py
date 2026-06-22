"""Classify a paper into an existing field or propose a new one, via the LLM."""
import logging
import re

import config
from ingest.ollama_client import generate_json
from seeds import slugify

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")
# Common words that overlap with nearly every field/paper and so add noise
# rather than signal when scoring relevance.
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "based", "by", "for", "from", "in",
    "into", "is", "it", "its", "new", "of", "on", "or", "study", "that",
    "the", "their", "this", "to", "using", "via", "with",
}


def _tokens(text):
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS}


def _select_relevant_fields(paper: dict, summary: dict, fields: list,
                             max_fields: int) -> list:
    """Rank fields by word/keyword overlap with the paper's title+summary, and
    keep only the most relevant ``max_fields``. Sending all fields once the
    list grows into the hundreds can blow the prompt past OLLAMA_NUM_CTX,
    silently truncating it (the model may never even see the paper's title or
    summary) — this keeps the classifier prompt bounded as the field list grows.
    """
    if len(fields) <= max_fields:
        return fields
    paper_words = _tokens(paper.get("title", "")) | _tokens(summary.get("body", ""))
    scored = []
    for f in fields:
        kw_words = {kw.lower() for kw in (f.get("keywords") or [])}
        name_desc_words = _tokens(f.get("name", "")) | _tokens(f.get("description", ""))
        # Curated keywords are the strongest relevance signal, so they count double.
        score = (2 * len(paper_words & kw_words)
                 + len(paper_words & (name_desc_words - kw_words)))
        scored.append((score, f))
    scored.sort(key=lambda sf: sf[0], reverse=True)
    return [f for _, f in scored[:max_fields]]

SYSTEM = (
    "You assign academic papers to research fields (like subreddits). Secondary "
    "fields exist precisely so a paper can still surface under a related "
    "existing field even when its primary is a new, more specific one. You also "
    "list any other existing fields the paper clearly fits, so it shows up in "
    "each of them."
)

PROMPT = """Assign this paper to a primary research field, and list any other \
existing fields it also clearly belongs to.

Paper title: {title}
Summary: {summary}

Existing fields (slug — name: description):
{field_list}

Rules:
- Choose ONE primary field, existing or new.
{sensitivity_rule}
- A new field should be a broad research area (like "Stochastic Optimization"), \
not a narrow paper-specific topic. Never create a new field that's just a \
narrower variant of an existing one (e.g. "organic-zinc-battery-chemistry" when \
"battery-chemistry" already exists) — reuse the broader existing field instead.
- "also_fields" is a list of OTHER existing field slugs (from the list above) the \
paper genuinely also fits — interdisciplinary papers belong to several, and this \
is also how a paper with a new primary field still surfaces under a related \
existing field. Use [] when the paper fits only its primary field. Never invent \
slugs here and never repeat the primary field.

Return a JSON object:
- If an existing field is the best fit: {{"field_slug": "<existing-slug>", \
"also_fields": ["<existing-slug>", ...]}}
- If a new field is the best fit: {{"new_field": {{"name": "<Field Name>", \
"description": "<one sentence>", "keywords": ["<keyword>", ...]}}, \
"also_fields": ["<existing-slug>", ...]}}
  "keywords" is 5-10 lowercase search terms (synonyms, abbreviations, related \
  techniques) someone might type to find this field.

Return only the JSON object."""


def _secondary_slugs(result, existing_slugs, primary_slug):
    """Clean the model's ``also_fields`` into known, distinct secondary slugs."""
    raw = result.get("also_fields") or result.get("also") or []
    if not isinstance(raw, list):
        return []
    out = []
    for s in raw:
        slug = (s or "").strip() if isinstance(s, str) else ""
        if slug and slug in existing_slugs and slug != primary_slug and slug not in out:
            out.append(slug)
    return out


def _keywords(new_field_result):
    raw = new_field_result.get("keywords") or []
    if not isinstance(raw, list):
        return []
    return [k.strip().lower() for k in raw if isinstance(k, str) and k.strip()]


def _sensitivity_rule(sensitivity: float) -> str:
    """Turn a 0.0-1.0 new-field sensitivity into a natural-language rule for
    the prompt. 0 = never create a new field; 1 = create one liberally."""
    if sensitivity <= 0.0:
        return (
            "- NEVER create a new field. Always choose whichever existing field "
            "is the closest match, even if the fit feels loose or only partial."
        )
    if sensitivity <= 0.25:
        return (
            "- Be very reluctant to create a new field. Only do it when the "
            "paper's research area is clearly missing from the existing list "
            "entirely — not just narrower or more specific than an existing "
            "field. When in doubt, reuse the closest existing (broader) field."
        )
    if sensitivity <= 0.5:
        return (
            "- Strongly prefer an existing field. Only create a new one when no "
            "existing field reasonably covers the paper's research area — a "
            "paper being a narrower sub-topic of an existing field is NOT enough "
            "reason to create a new one; reuse the broader existing field instead."
        )
    if sensitivity <= 0.75:
        return (
            "- Choose whichever — existing or new — most accurately describes "
            "the paper's actual research area. Don't force-fit an existing field "
            "that's only a loose or adjacent match just to avoid creating a new "
            "one, but don't split off a new field for a minor variant of an "
            "existing one either."
        )
    return (
        "- Prefer precision: create a new field whenever an existing field is "
        "only an approximate or adjacent match, even if it's a fairly narrow "
        "sub-area, rather than reusing a broader existing field."
    )


def classify(paper: dict, summary: dict, fields: list, model: str | None = None,
             sensitivity: float | None = None) -> dict:
    """Decide the field(s) for a paper.

    `fields` is a list of dicts with keys: slug, name, description.
    ``model`` overrides config.OLLAMA_MODEL (e.g. the fast model for bulk
    reclassification). ``sensitivity`` overrides config.NEW_FIELD_SENSITIVITY
    (0.0 = never create a new field, 1.0 = create one liberally).
    Returns a dict with a primary assignment plus ``secondary_slugs`` (existing
    fields the paper also fits, possibly empty):
      - {"field_slug": slug, "secondary_slugs": [...]} for an existing primary, or
      - {"new_field": {"slug", "name", "description", "keywords"},
         "secondary_slugs": [...]}.
    """
    if sensitivity is None:
        sensitivity = config.NEW_FIELD_SENSITIVITY
    shown_fields = _select_relevant_fields(paper, summary, fields,
                                            config.CLASSIFIER_MAX_FIELDS)
    field_list = "\n".join(
        f"- {f['slug']} — {f['name']}: {f.get('description') or ''}" for f in shown_fields
    )
    prompt = PROMPT.format(
        title=paper["title"],
        summary=summary["body"],
        field_list=field_list or "(none yet)",
        sensitivity_rule=_sensitivity_rule(sensitivity),
    )
    result = generate_json(prompt, system=SYSTEM, temperature=0.1, model=model)

    existing_slugs = {f["slug"] for f in shown_fields}

    slug = (result.get("field_slug") or "").strip()
    if slug and slug in existing_slugs:
        return {
            "field_slug": slug,
            "secondary_slugs": _secondary_slugs(result, existing_slugs, slug),
        }

    new = result.get("new_field") or {}
    name = (new.get("name") or "").strip()
    if name:
        new_slug = slugify(name)
        # De-duplicate against existing fields by slug (case/punct-insensitive).
        if new_slug in existing_slugs:
            return {
                "field_slug": new_slug,
                "secondary_slugs": _secondary_slugs(result, existing_slugs, new_slug),
            }
        return {
            "new_field": {
                "slug": new_slug,
                "name": name,
                "description": (new.get("description") or "").strip(),
                "keywords": _keywords(new),
            },
            # A brand-new field has no slug among existing yet; secondaries are
            # still drawn only from the existing set.
            "secondary_slugs": _secondary_slugs(result, existing_slugs, new_slug),
        }

    # Fallback: if the model returned a slug that doesn't exist but looks like a
    # field name, or returned nothing usable, treat it as a new field by slug.
    if slug:
        new_slug = slugify(slug)
        return {
            "new_field": {
                "slug": new_slug,
                "name": slug.replace("-", " ").title(),
                "description": "",
                "keywords": [],
            },
            "secondary_slugs": _secondary_slugs(result, existing_slugs, new_slug),
        }

    raise ValueError("Classifier returned no usable field assignment")


SECONDARY_SYSTEM = (
    "You assign academic papers to research fields (like subreddits). A paper "
    "already has a primary field; you only decide which OTHER existing fields it "
    "also clearly fits, so it shows up there too. You are conservative: most "
    "papers fit only their primary field."
)

SECONDARY_PROMPT = """This paper's primary field is already "{primary_name}". \
Decide whether it also clearly fits any OTHER existing fields below, so it \
surfaces there too.

Paper title: {title}
Summary: {summary}

Other existing fields (slug — name: description):
{field_list}

Rules:
- Only include a field if the paper genuinely, clearly belongs there too — \
interdisciplinary papers belong to several, but most papers fit only their \
primary field.
- Use [] when the paper fits only its primary field.
- Never invent slugs; only use slugs from the list above.

Return a JSON object: {{"also_fields": ["<existing-slug>", ...]}}
Return only the JSON object."""


KEYWORDS_SYSTEM = (
    "You generate search keywords for an academic research field, so users can "
    "find it via a search bar."
)

KEYWORDS_PROMPT = """Generate search keywords for this research field, so users \
can find it by typing related terms into a search bar.

Field name: {name}
Description: {description}

Return 5-10 lowercase keywords: synonyms, abbreviations, and closely related \
techniques or topics someone might type to find this field.

Return a JSON object: {{"keywords": ["<keyword>", ...]}}
Return only the JSON object."""


def generate_keywords(name: str, description: str) -> list:
    """Generate search keywords for an existing field that lacks them (used by
    the keyword backfill script for seed/older fields)."""
    prompt = KEYWORDS_PROMPT.format(name=name, description=description or "")
    result = generate_json(prompt, system=KEYWORDS_SYSTEM, temperature=0.2)
    return _keywords(result)


def classify_secondary(paper: dict, summary: dict, fields: list, primary_slug: str,
                        primary_name: str) -> list:
    """Decide which OTHER existing fields a paper (with a primary already
    assigned) also clearly fits. Returns a list of secondary slugs (possibly
    empty). Used by the secondary-field backfill script.
    """
    others = [f for f in fields if f["slug"] != primary_slug]
    if not others:
        return []
    shown_fields = _select_relevant_fields(paper, summary, others,
                                            config.CLASSIFIER_MAX_FIELDS)
    field_list = "\n".join(
        f"- {f['slug']} — {f['name']}: {f.get('description') or ''}" for f in shown_fields
    )
    prompt = SECONDARY_PROMPT.format(
        primary_name=primary_name,
        title=paper["title"],
        summary=summary["body"],
        field_list=field_list,
    )
    result = generate_json(prompt, system=SECONDARY_SYSTEM, temperature=0.1)
    existing_slugs = {f["slug"] for f in shown_fields}
    return _secondary_slugs(result, existing_slugs, primary_slug)
