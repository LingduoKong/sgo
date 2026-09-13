"""
Load, filter, and convert personas from the Nemotron-Personas-USA dataset.

Generic loader — filters and field mapping are configurable via CLI args or
as a library. Returns a list of evaluator-ready profile dicts.

Usage:
    # Filter by any combination of fields
    uv run python scripts/persona_loader.py \
      --filters '{"sex": "Female", "state": "IL", "age_min": 25, "age_max": 50}' \
      --limit 100 \
      --output data/filtered.json

    # As a library
    from persona_loader import load_personas, filter_personas, to_profile
"""

import json
import random
import argparse
from pathlib import Path
from datasets import load_from_disk

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "nemotron"

# All narrative fields in the dataset, in order of richness
NARRATIVE_FIELDS = [
    "persona", "cultural_background", "professional_persona",
    "career_goals_and_ambitions", "hobbies_and_interests",
    "sports_persona", "arts_persona", "travel_persona", "culinary_persona",
    "skills_and_expertise",
]


def load_personas(data_dir=None):
    """Load dataset from disk. Run setup_data.py first if not cached."""
    data_dir = Path(data_dir or DEFAULT_DATA_DIR)
    if not (data_dir / "dataset_info.json").exists():
        raise FileNotFoundError(
            f"Dataset not found at {data_dir}. Run: uv run python scripts/setup_data.py"
        )
    return load_from_disk(str(data_dir))


def filter_personas(ds, filters: dict, limit: int = None, seed: int = 42):
    """
    Filter dataset by arbitrary field conditions.

    Generic keys are exact matches on their corresponding dataset columns.
    ``state`` and ``city`` also work as generic aliases across the country
    variants' geography columns. Sex is always compared exactly so native
    values such as Japan's 男/女 are preserved.
    """
    random.seed(seed)

    age_min = filters.get("age_min", 0)
    age_max = filters.get("age_max", 200)
    sex = filters.get("sex")
    state = filters.get("state")
    city = filters.get("city")
    marital = filters.get("marital_status")
    education = filters.get("education_level")
    occupation = filters.get("occupation")

    if isinstance(marital, str):
        marital = [marital]
    if isinstance(education, str):
        education = [education]

    columns = set(getattr(ds, "column_names", []))
    state_columns = [name for name in ("state", "region", "departement") if name in columns]
    city_columns = [name for name in ("city", "commune", "prefecture", "district", "area", "planning_area", "municipality") if name in columns]
    handled = {"sex", "age_min", "age_max", "state", "city", "marital_status", "education_level", "occupation"}

    def exact_match(value, expected):
        if isinstance(expected, (list, tuple, set)):
            return value in expected
        return value == expected

    def matches(row):
        if sex and not exact_match(row.get("sex"), sex):
            return False
        age = row.get("age")
        if ("age_min" in filters or "age_max" in filters) and age is None:
            return False
        if age is not None and not (age_min <= age <= age_max):
            return False
        if state and not any(exact_match(row.get(name), state) for name in state_columns):
            return False
        if city and not any(str(item).lower() in str(row.get(name) or "").lower() for item in (city if isinstance(city, list) else [city]) for name in city_columns):
            return False
        if marital and row.get("marital_status") not in marital:
            return False
        if education and row.get("education_level") not in education:
            return False
        if occupation and not any(str(item).lower() in str(row.get("occupation") or "").lower() for item in (occupation if isinstance(occupation, list) else [occupation])):
            return False
        for key, expected in filters.items():
            if key in handled:
                continue
            if key not in columns or not exact_match(row.get(key), expected):
                return False
        return True

    # Tiny/interactive datasets do not need worker processes, and single
    # process filtering is safer in restricted server environments.
    if len(ds) < 10_000:
        filtered = ds.filter(matches)
    else:
        filtered = ds.filter(matches, num_proc=4)

    if limit and len(filtered) > limit:
        indices = random.sample(range(len(filtered)), limit)
        filtered = filtered.select(indices)

    return filtered


def build_persona_text(row: dict) -> str:
    """Combine all narrative dimensions into a single rich description."""
    parts = []
    labels = ["", "Background", "Career", "Ambitions", "Hobbies",
              "Sports", "Arts", "Travel", "Food", "Skills"]
    for label, field in zip(labels, NARRATIVE_FIELDS):
        val = row.get(field)
        if val:
            parts.append(f"{label}: {val}" if label else val)
    return " ".join(parts)


def extract_name(row: dict) -> str:
    """Extract name from the first narrative field that starts with a name."""
    for field in NARRATIVE_FIELDS:
        text = row.get(field, "")
        if text:
            words = text.split()
            if len(words) >= 2 and words[0][0].isupper() and words[1][0].isupper():
                return f"{words[0]} {words[1]}".rstrip(",.")
    return "Unknown"


def parse_json_list(raw) -> list:
    try:
        out = json.loads(raw) if isinstance(raw, str) else raw
        return out if isinstance(out, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def to_profile(row: dict, user_id: int, dataset: str = None) -> dict:
    """Convert a Nemotron row into a generic evaluator profile dict."""
    name = extract_name(row)
    hobbies = parse_json_list(row.get("hobbies_and_interests_list", "[]"))
    skills = parse_json_list(row.get("skills_and_expertise_list", "[]"))

    return {
        "user_id": user_id,
        "name": name,
        "persona": build_persona_text(row),
        "age": row.get("age", 30),
        "sex": row.get("sex", ""),
        "city": row.get("city") or row.get("commune") or row.get("prefecture") or row.get("district") or row.get("planning_area") or row.get("municipality") or row.get("area", ""),
        "state": row.get("state") or row.get("region") or row.get("departement", ""),
        "country": row.get("country") or row.get("国") or dataset or "USA",
        "education_level": row.get("education_level", ""),
        "marital_status": row.get("marital_status", ""),
        "occupation": (row.get("occupation") or "").replace("_", " ").title(),
        "interests": hobbies + skills,
        "source_uuid": row.get("uuid", ""),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--filters", type=json.loads, default={})
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default="data/filtered.json")
    args = parser.parse_args()

    ds = load_personas()
    print(f"Loaded {len(ds)} total personas")

    filtered = filter_personas(ds, args.filters, limit=args.limit, seed=args.seed)
    print(f"Filtered: {len(filtered)} personas")

    profiles = [to_profile(row, i) for i, row in enumerate(filtered)]
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)
    print(f"Saved to {args.output}")
