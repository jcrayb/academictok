"""Classify a paper into an existing field or propose a new one, via the LLM."""
import logging

from ingest.ollama_client import generate_json
from seeds import slugify

logger = logging.getLogger(__name__)

SYSTEM = (
    "You assign academic papers to research fields (like subreddits). You "
    "strongly prefer assigning a paper to an existing field. You only propose a "
    "new field when the paper genuinely does not belong to any existing one. You "
    "also list any other existing fields the paper clearly fits, so it shows up "
    "in each of them."
)

PROMPT = """Assign this paper to a primary research field, and list any other \
existing fields it also clearly belongs to.

Paper title: {title}
Summary: {summary}

Existing fields (slug — name: description):
{field_list}

Rules:
- Choose ONE primary field: STRONGLY prefer an existing field. Only create a new \
field if none reasonably fits.
- A new field should be a broad research area (like "Stochastic Optimization"), \
not a narrow paper-specific topic.
- "also_fields" is a list of OTHER existing field slugs (from the list above) the \
paper genuinely also fits — interdisciplinary papers belong to several. Use [] \
when the paper fits only its primary field. Never invent slugs here and never \
repeat the primary field.

Return a JSON object:
- If the primary is an existing field: {{"field_slug": "<existing-slug>", \
"also_fields": ["<existing-slug>", ...]}}
- If a new primary field is needed: {{"new_field": {{"name": "<Field Name>", \
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


def classify(paper: dict, summary: dict, fields: list) -> dict:
    """Decide the field(s) for a paper.

    `fields` is a list of dicts with keys: slug, name, description.
    Returns a dict with a primary assignment plus ``secondary_slugs`` (existing
    fields the paper also fits, possibly empty):
      - {"field_slug": slug, "secondary_slugs": [...]} for an existing primary, or
      - {"new_field": {"slug", "name", "description", "keywords"},
         "secondary_slugs": [...]}.
    """
    field_list = "\n".join(
        f"- {f['slug']} — {f['name']}: {f.get('description') or ''}" for f in fields
    )
    prompt = PROMPT.format(
        title=paper["title"],
        summary=summary["body"],
        field_list=field_list or "(none yet)",
    )
    result = generate_json(prompt, system=SYSTEM, temperature=0.1)

    existing_slugs = {f["slug"] for f in fields}

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
    field_list = "\n".join(
        f"- {f['slug']} — {f['name']}: {f.get('description') or ''}" for f in others
    )
    prompt = SECONDARY_PROMPT.format(
        primary_name=primary_name,
        title=paper["title"],
        summary=summary["body"],
        field_list=field_list,
    )
    result = generate_json(prompt, system=SECONDARY_SYSTEM, temperature=0.1)
    existing_slugs = {f["slug"] for f in others}
    return _secondary_slugs(result, existing_slugs, primary_slug)
