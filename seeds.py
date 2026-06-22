"""Seed list of curated fields ("subreddits") and their search queries.

These are inserted as `is_seed=1`. The LLM classifier may add more fields
(`is_seed=0`) during ingestion when a paper doesn't fit any existing field.
"""
from db import get_conn, now_iso


def slugify(name: str) -> str:
    """Lowercase, hyphenated slug from a field name."""
    out = []
    prev_dash = False
    for ch in name.lower().strip():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


# (name, description, query, keywords)
SEED_FIELDS = [
    (
        "Stochastic Optimization",
        "Optimization under uncertainty: stochastic programming, robust and "
        "chance-constrained methods, sample average approximation.",
        "stochastic optimization stochastic programming under uncertainty",
        ["stochastic programming", "robust optimization", "chance-constrained",
         "sample average approximation", "decision making under uncertainty",
         "SAA", "two-stage stochastic"],
    ),
    (
        "Reinforcement Learning",
        "Learning to act via reward: value-based, policy-gradient, and "
        "model-based RL, plus applications.",
        "reinforcement learning policy optimization",
        ["RL", "policy gradient", "Q-learning", "deep RL", "markov decision process",
         "MDP", "model-based RL", "value function"],
    ),
    (
        "Metal Additive Manufacturing",
        "Metal 3D printing: process modeling, microstructure, defects, and "
        "process optimization for additive manufacturing.",
        "metal additive manufacturing laser powder bed fusion",
        ["3D printing", "metal 3D printing", "laser powder bed fusion", "LPBF",
         "microstructure", "AM", "additive manufacturing defects"],
    ),
    (
        "Integer Programming",
        "Discrete optimization: branch-and-bound/cut, cutting planes, and "
        "mixed-integer programming theory and solvers.",
        "mixed integer programming branch and cut",
        ["MIP", "mixed-integer programming", "branch and bound", "branch and cut",
         "cutting planes", "discrete optimization", "combinatorial optimization"],
    ),
    (
        "Queueing Theory",
        "Mathematical modeling of waiting lines, service systems, and their "
        "performance analysis.",
        "queueing theory service systems performance analysis",
        ["queuing theory", "waiting lines", "service systems", "M/M/1",
         "performance analysis", "call centers"],
    ),
    (
        "Supply Chain Management",
        "Inventory, logistics, and network design for production and "
        "distribution systems.",
        "supply chain optimization inventory management logistics",
        ["supply chain", "inventory management", "logistics", "network design",
         "distribution systems", "operations management"],
    ),
]


def seed_fields() -> int:
    """Insert seed fields if not already present. Returns count inserted."""
    now = now_iso()
    inserted = 0
    with get_conn() as conn:
        for name, description, query, keywords in SEED_FIELDS:
            slug = slugify(name)
            cur = conn.execute(
                """INSERT OR IGNORE INTO fields
                   (slug, name, description, query, keywords, is_seed, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (slug, name, description, query, ",".join(keywords), now),
            )
            inserted += cur.rowcount
    return inserted


if __name__ == "__main__":
    from db import init_db

    init_db()
    n = seed_fields()
    print(f"Seeded {n} new field(s).")
