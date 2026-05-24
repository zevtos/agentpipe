"""
_profile.py — YAML loader, validator, and ProfileConfig dataclass for
ultrasearch v2 profiles.

Note: this module is named `_profile.py` (not `profile.py`) to avoid
shadowing the stdlib `profile` module when `scripts/` is prepended to
sys.path. The stdlib `cProfile` imports `profile` as `_pyprofile` during
its module init, so a local `profile.py` on sys.path breaks any later
`torch._dynamo` / `sentence_transformers` import chain that touches
`cProfile`.

Each profile lives at `profiles/<name>.yaml` next to the skill root. This
module parses one, validates it against `profiles/_schema.json`, and returns
an immutable `ProfileConfig` dataclass the rest of the pipeline imports.

Validation strategy: we prefer the `jsonschema` package if available (it
honors all of Draft 2020-12), but fall back to a hand-rolled validator
that covers the subset of constraints we actually use — required keys,
types, enum/pattern checks, min/max ranges, and additionalProperties=false
at the top level. This keeps the profile loader self-contained for v1
installs that haven't lazy-installed Stage 1 deps yet.

CLI: `python3 _profile.py [name]` — dumps the loaded ProfileConfig as JSON.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from _common import skill_dir, log


class ConfigError(Exception):
    """Raised when a profile YAML is missing, malformed, or fails validation."""


# ---------- dataclasses ----------

@dataclass(frozen=True)
class SourceRef:
    """One entry under a profile's `sources` list."""
    id: str
    weight: float
    config: dict


@dataclass(frozen=True)
class ProfileConfig:
    """Frozen, validated representation of a profile YAML."""
    name: str
    description: str
    sources: tuple[SourceRef, ...]
    scoring_ref: str           # 'scoring/<file>.py:<funcname>'
    output_template: str       # filename in templates/
    output_alt: tuple[str, ...]
    deps: tuple[str, ...]
    recursive_capable: bool
    flat_defaults: dict = field(default_factory=dict)
    recursive_defaults: dict = field(default_factory=dict)


# ---------- YAML I/O ----------

def _load_yaml(path: Path) -> dict:
    """Parse a YAML file. Raises ConfigError on missing file or parse error."""
    if not path.is_file():
        raise ConfigError(f"profile YAML not found: {path}")
    try:
        import yaml  # PyYAML — already a transitive dep of multiple v1 packages
    except ImportError as e:
        raise ConfigError(
            "pyyaml is required to load profile YAML files. "
            "Install via `pip install pyyaml` or run through ensure_env.py."
        ) from e
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as e:
        raise ConfigError(f"failed to parse {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level YAML must be a mapping, got {type(data).__name__}")
    return data


def _load_schema(profiles_dir: Path) -> dict:
    """Load the JSON Schema once; cached on the module via a closure dict."""
    schema_path = profiles_dir / "_schema.json"
    if not schema_path.is_file():
        raise ConfigError(f"schema not found: {schema_path}")
    try:
        with schema_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"failed to read schema {schema_path}: {e}") from e


# ---------- validation ----------

def _have_jsonschema() -> bool:
    try:
        import jsonschema  # noqa: F401
        return True
    except ImportError:
        return False


def validate_profile(data: dict, schema: dict | None = None) -> None:
    """Validate `data` against the profile schema. Raises ConfigError on failure.

    If `jsonschema` is installed, uses the Draft 2020-12 validator. Otherwise
    runs a hand-rolled subset validator that mirrors the schema constraints.
    """
    if schema is None:
        schema = _load_schema(skill_dir() / "profiles")

    if _have_jsonschema():
        import jsonschema
        from jsonschema import Draft202012Validator
        try:
            Draft202012Validator(schema).validate(data)
            return
        except jsonschema.ValidationError as e:
            path = "/".join(str(p) for p in e.absolute_path) or "<root>"
            raise ConfigError(f"profile validation failed at {path}: {e.message}") from e

    # Hand-rolled fallback (covers what _schema.json actually asserts).
    _validate_manual(data, schema)


def _validate_manual(data: dict, schema: dict) -> None:
    """Minimal validator: required keys, types, patterns, ranges,
    additionalProperties=false at top level. Sufficient for our schema."""
    required = schema.get("required", [])
    missing = [k for k in required if k not in data]
    if missing:
        raise ConfigError(f"missing required keys: {missing}")

    if schema.get("additionalProperties") is False:
        allowed = set(schema.get("properties", {}).keys())
        extra = [k for k in data.keys() if k not in allowed]
        if extra:
            raise ConfigError(f"unknown top-level keys: {extra}")

    props = schema.get("properties", {})
    for key, sub in props.items():
        if key not in data:
            continue
        _validate_value(key, data[key], sub)


def _validate_value(path: str, val: Any, sub: dict) -> None:
    """Validate a single value against its sub-schema."""
    expected_type = sub.get("type")
    if expected_type:
        if not _type_matches(val, expected_type):
            raise ConfigError(
                f"{path}: expected type {expected_type!r}, got {type(val).__name__}"
            )

    if expected_type == "string":
        if "pattern" in sub and not re.match(sub["pattern"], val):
            raise ConfigError(f"{path}: value {val!r} does not match pattern {sub['pattern']!r}")
        if "minLength" in sub and len(val) < sub["minLength"]:
            raise ConfigError(f"{path}: string shorter than minLength {sub['minLength']}")
        if "maxLength" in sub and len(val) > sub["maxLength"]:
            raise ConfigError(f"{path}: string longer than maxLength {sub['maxLength']}")
    elif expected_type == "number" or expected_type == "integer":
        if "minimum" in sub and val < sub["minimum"]:
            raise ConfigError(f"{path}: value {val} below minimum {sub['minimum']}")
        if "maximum" in sub and val > sub["maximum"]:
            raise ConfigError(f"{path}: value {val} above maximum {sub['maximum']}")
    elif expected_type == "array":
        if "minItems" in sub and len(val) < sub["minItems"]:
            raise ConfigError(f"{path}: array shorter than minItems {sub['minItems']}")
        if "maxItems" in sub and len(val) > sub["maxItems"]:
            raise ConfigError(f"{path}: array longer than maxItems {sub['maxItems']}")
        if (sub.get("uniqueItems")
                and all(isinstance(x, str) for x in val)
                and len(val) != len(set(val))):
            raise ConfigError(f"{path}: array items must be unique")
        if "items" in sub:
            for i, item in enumerate(val):
                _validate_value(f"{path}[{i}]", item, sub["items"])
    elif expected_type == "object":
        sub_required = sub.get("required", [])
        sub_props = sub.get("properties", {})
        missing = [k for k in sub_required if k not in val]
        if missing:
            raise ConfigError(f"{path}: missing required keys {missing}")
        if sub.get("additionalProperties") is False:
            allowed = set(sub_props.keys())
            extra = [k for k in val.keys() if k not in allowed]
            if extra:
                raise ConfigError(f"{path}: unknown keys {extra}")
        for k, v in val.items():
            if k in sub_props:
                _validate_value(f"{path}.{k}", v, sub_props[k])


def _type_matches(val: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(val, str)
    if expected == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if expected == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if expected == "boolean":
        return isinstance(val, bool)
    if expected == "array":
        return isinstance(val, list)
    if expected == "object":
        return isinstance(val, dict)
    if expected == "null":
        return val is None
    return True


# ---------- public API ----------

def load_profile(name: str, profiles_dir: Path | None = None) -> ProfileConfig:
    """Load <name>.yaml from `profiles_dir` (defaults to skill_dir()/'profiles'),
    validate it, and return a frozen ProfileConfig.

    Raises ConfigError on any failure (missing file, parse error, validation
    failure, or name/filename mismatch)."""
    if profiles_dir is None:
        profiles_dir = skill_dir() / "profiles"

    if not name or not re.match(r"^[a-z][a-z0-9_]*$", name):
        raise ConfigError(f"invalid profile name: {name!r}")

    path = profiles_dir / f"{name}.yaml"
    data = _load_yaml(path)
    schema = _load_schema(profiles_dir)
    validate_profile(data, schema)

    if data["name"] != name:
        raise ConfigError(
            f"{path}: profile name {data['name']!r} does not match filename stem {name!r}"
        )

    sources = tuple(
        SourceRef(
            id=s["id"],
            weight=float(s.get("weight", 1.0)),
            config=dict(s.get("config") or {}),
        )
        for s in data["sources"]
    )

    cfg = ProfileConfig(
        name=data["name"],
        description=data["description"],
        sources=sources,
        scoring_ref=data["scoring"],
        output_template=data["output_template"],
        output_alt=tuple(data.get("output_alt") or []),
        deps=tuple(data.get("deps") or []),
        recursive_capable=bool(data.get("recursive_capable", False)),
        flat_defaults=dict(data.get("flat_defaults") or {}),
        recursive_defaults=dict(data.get("recursive_defaults") or {}),
    )
    log(f"loaded profile {cfg.name!r} ({len(cfg.sources)} sources, "
        f"template={cfg.output_template}, recursive={cfg.recursive_capable})")
    return cfg


def list_profiles(profiles_dir: Path | None = None) -> list[str]:
    """Return sorted profile names available in `profiles_dir`. Skips files
    starting with '_' (e.g. _schema.json)."""
    if profiles_dir is None:
        profiles_dir = skill_dir() / "profiles"
    if not profiles_dir.is_dir():
        return []
    names = []
    for p in profiles_dir.glob("*.yaml"):
        if p.name.startswith("_"):
            continue
        names.append(p.stem)
    return sorted(names)


# ---------- CLI ----------

def _dataclass_to_jsonable(cfg: ProfileConfig) -> dict:
    """Convert ProfileConfig to a JSON-serializable dict (tuples → lists,
    nested SourceRef dataclasses → dicts)."""
    d = asdict(cfg)
    # asdict() turns SourceRef dataclasses into dicts, but leaves tuples as
    # tuples. Walk and normalize.
    def _norm(x):
        if isinstance(x, tuple):
            return [_norm(i) for i in x]
        if isinstance(x, list):
            return [_norm(i) for i in x]
        if isinstance(x, dict):
            return {k: _norm(v) for k, v in x.items()}
        return x
    return _norm(d)


if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "academic"
    try:
        cfg = load_profile(name)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(_dataclass_to_jsonable(cfg), indent=2, ensure_ascii=False))
