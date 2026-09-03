"""
settings_sheet.py — Use a "Settings" tab in the spreadsheet as the config.

The published dashboard is a static page with no credentials, so it can display
settings but can never write them. Putting the settings in the spreadsheet
solves that from the other side: the sheet is the place you edit, the browser
only shows what it currently says, and each run reads it and overlays it onto
config.yaml before anything else happens.

config.yaml stays the schema and the defaults; the sheet overrides individual
values. A blank cell means "leave the default alone", so a half-filled tab is
valid and a new setting added to the code does not need a sheet edit to work.

A bad value never stops a run. It is logged and the default is kept — a typo in
a spreadsheet cell should not take the automation down at 6am.

    python settings_sheet.py --seed                      # create/refresh the tab
    python settings_sheet.py --show                      # print what it overrides
    python settings_sheet.py --apply-to config.yaml      # write the overrides in
"""

import argparse
import logging
import sys

import projects_registry
from config_loader import load_config
from logger_setup import setup_logging_from_config
from web.settings import SCHEMA, get_path, set_path

logger = logging.getLogger(__name__)

WORKSHEET = "Settings"
HEADER = ["Group", "Setting", "Value", "Type", "Options", "Description"]

# Column the operator edits; everything else is documentation.
VALUE_COLUMN = "Value"
KEY_COLUMN = "Setting"

_TRUE = {"true", "yes", "y", "1", "on", "enabled"}
_FALSE = {"false", "no", "n", "0", "off", "disabled"}


def _fields() -> list:
    """Every schema field, flattened, with its group name."""
    return [(group["group"], field)
            for group in SCHEMA for field in group["fields"]]


def _render(value, kind: str) -> str:
    if value is None:
        return ""
    if kind == "bool":
        return "TRUE" if value else "FALSE"
    if kind in ("multiselect", "keywords"):
        return ", ".join(str(v) for v in value)
    return str(value)


def _options(field: dict) -> str:
    kind = field["type"]
    if kind == "select":
        return " | ".join(field.get("options", []))
    if kind == "multiselect":
        return " | ".join(
            o["value"] if isinstance(o, dict) else str(o)
            for o in field.get("options", []))
    if kind in ("int", "float"):
        low, high = field.get("min"), field.get("max")
        if low is not None and high is not None:
            return f"{low} – {high}"
    if kind == "bool":
        return "TRUE | FALSE"
    return ""


def parse_value(raw: str, field: dict):
    """Coerce a cell into the type config.yaml expects.

    Raises ValueError with a message aimed at whoever typed the cell.
    """
    text = (raw or "").strip()
    if not text:
        return None                      # blank = keep the default

    kind = field["type"]
    if kind == "bool":
        lowered = text.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ValueError(f"expected TRUE or FALSE, got {text!r}")

    if kind in ("int", "float"):
        try:
            number = int(text) if kind == "int" else float(text)
        except ValueError:
            raise ValueError(f"expected a number, got {text!r}") from None
        low, high = field.get("min"), field.get("max")
        if low is not None and number < low:
            raise ValueError(f"{number} is below the minimum {low}")
        if high is not None and number > high:
            raise ValueError(f"{number} is above the maximum {high}")
        return number

    if kind == "select":
        allowed = field.get("options", [])
        if allowed and text not in allowed:
            raise ValueError(f"{text!r} is not one of: {', '.join(allowed)}")
        return text

    if kind in ("multiselect", "keywords"):
        chosen = [part.strip() for part in text.split(",") if part.strip()]
        allowed = [o["value"] if isinstance(o, dict) else str(o)
                   for o in field.get("options", [])]
        if allowed:
            unknown = [c for c in chosen if c not in allowed]
            if unknown:
                raise ValueError(
                    f"unknown value(s) {', '.join(unknown)}; allowed: {', '.join(allowed)}")
        return chosen

    return text


# ── Reading ──────────────────────────────────────────────────────────────────

def read_overrides(store, worksheet: str = WORKSHEET) -> tuple:
    """Return ({path: value}, [problem strings]) from the Settings tab.

    A missing tab is not an error — it simply means nothing is overridden.
    """
    try:
        rows = store.load_all_rows(worksheet)
    except Exception as exc:
        logger.info(f"no '{worksheet}' tab ({type(exc).__name__}); using config.yaml as-is")
        return {}, []

    if not rows or len(rows) < 2:
        return {}, []

    header = [h.strip() for h in rows[0]]
    if KEY_COLUMN not in header or VALUE_COLUMN not in header:
        return {}, [f"'{worksheet}' needs both a '{KEY_COLUMN}' and a '{VALUE_COLUMN}' column"]

    key_at, value_at = header.index(KEY_COLUMN), header.index(VALUE_COLUMN)
    known = {field["path"]: field for _, field in _fields()}

    overrides, problems = {}, []
    for line, row in enumerate(rows[1:], start=2):
        path = (row[key_at].strip() if key_at < len(row) else "")
        if not path:
            continue
        field = known.get(path)
        if field is None:
            problems.append(f"row {line}: unknown setting '{path}' — ignored")
            continue
        raw = row[value_at] if value_at < len(row) else ""
        try:
            value = parse_value(raw, field)
        except ValueError as exc:
            problems.append(f"row {line} ({path}): {exc} — keeping the current value")
            continue
        if value is not None:
            overrides[path] = value
    return overrides, problems


def apply_overrides(config: dict, overrides: dict) -> list:
    """Overlay onto a loaded config. Returns human-readable change lines."""
    changed = []
    for path, value in overrides.items():
        current = get_path(config, path)
        if current != value:
            set_path(config, path, value)
            changed.append(f"{path}: {current!r} -> {value!r}")
    return changed


def apply_from_sheet(config: dict, store=None, worksheet: str = WORKSHEET) -> list:
    """Read the tab and apply it to *config* in place. Never raises."""
    try:
        if store is None:
            # The Web App when this machine has a project password and no
            # Google key; the Sheets API when it has the key.
            import remote_store
            store = remote_store.store_for(config)
        overrides, problems = read_overrides(store, worksheet)
    except Exception as exc:
        logger.warning(f"could not read '{worksheet}': {type(exc).__name__}: {exc} "
                       "— continuing with config.yaml")
        return []

    for problem in problems:
        logger.warning(f"[{worksheet}] {problem}")
    changed = apply_overrides(config, overrides)
    for line in changed:
        logger.info(f"[{worksheet}] {line}")
    if not changed:
        logger.info(f"[{worksheet}] no overrides applied")
    return changed


# ── Seeding ──────────────────────────────────────────────────────────────────

def seed(store, config: dict, worksheet: str = WORKSHEET) -> int:
    """Create or refresh the tab, keeping any Value the operator already typed."""
    existing = {}
    try:
        rows = store.load_all_rows(worksheet)
        if rows and KEY_COLUMN in rows[0] and VALUE_COLUMN in rows[0]:
            key_at = rows[0].index(KEY_COLUMN)
            value_at = rows[0].index(VALUE_COLUMN)
            for row in rows[1:]:
                if key_at < len(row) and row[key_at].strip():
                    existing[row[key_at].strip()] = (
                        row[value_at] if value_at < len(row) else "")
    except Exception:
        pass                                  # tab does not exist yet

    table = [HEADER]
    for group, field in _fields():
        path = field["path"]
        # Keep what is already typed; otherwise show the value config.yaml has,
        # so the tab opens as an accurate picture rather than a blank form.
        value = existing.get(path)
        if value is None:
            value = _render(get_path(config, path), field["type"])
        table.append([group, path, value, field["type"],
                      _options(field), field.get("help", "")])

    # Through the store, so seeding works on a machine that has only a project
    # password. The bold header goes with it — replace_tab freezes row 1, which
    # is the part that matters, and colour is not worth a credentials
    # requirement.
    store.replace_tab(worksheet, table)
    return len(table) - 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage the Settings worksheet")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--worksheet", default=WORKSHEET)
    ap.add_argument("--seed", action="store_true", help="create/refresh the tab")
    ap.add_argument("--show", action="store_true", help="print what it overrides")
    ap.add_argument("--apply-to", dest="apply_to",
                    help="write the overrides into this config file (used by CI)")
    projects_registry.add_project_argument(ap)
    args = ap.parse_args()

    config = load_config(args.config)

    projects_registry.resolve(config, args.project)
    setup_logging_from_config(config)
    import remote_store
    store = remote_store.store_for(config)

    if args.seed:
        count = seed(store, config, args.worksheet)
        logger.info(f"'{args.worksheet}' now lists {count} settings")
        return

    overrides, problems = read_overrides(store, args.worksheet)
    for problem in problems:
        logger.warning(problem)
    if not overrides:
        logger.info(f"'{args.worksheet}' overrides nothing")
    for path, value in sorted(overrides.items()):
        marker = "=" if get_path(config, path) == value else "->"
        logger.info(f"  {path} {marker} {value!r}")

    if args.apply_to:
        # Round-trip so the ~120 comments in config.yaml survive; they are the
        # operator's documentation of what each key does.
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)
        with open(args.apply_to, encoding="utf-8") as fh:
            live = yaml.load(fh)
        changed = apply_overrides(live, overrides)
        with open(args.apply_to, "w", encoding="utf-8") as fh:
            yaml.dump(live, fh)
        for line in changed:
            logger.info(f"applied: {line}")
        logger.info(f"{len(changed)} setting(s) applied to {args.apply_to}")
    elif args.show:
        applied = apply_overrides(config, overrides)
        logger.info(f"{len(applied)} setting(s) would change")


if __name__ == "__main__":
    main()
