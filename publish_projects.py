"""
publish_projects.py — export and encrypt every project for the static dashboard.

Each project's tabs go to their own directory and are encrypted under that
project's own data key, so signing in to one project decrypts nothing belonging
to another. Nothing global is written: a shared index listing the projects would
publish their names to anyone who opens the URL, and the browser does not need
one — the Apps Script tells it which project its password unlocked, and it
fetches that directory directly.

    site/data/<project id>/index.enc.json
    site/data/<project id>/<worksheet>.enc.json

The data key comes from the control sheet rather than a repository secret, which
is what lets a new project be published without touching this repository at all.

    python publish_projects.py --out site/data                 # every project
    python publish_projects.py --out site/data --project main  # just one
"""

import argparse
import glob
import json
import logging
import os
import shutil
import sys

import projects_registry
import settings_sheet
from config_loader import load_config
from logger_setup import setup_logging_from_config

logger = logging.getLogger(__name__)


def publish_one(project: dict, config_path: str, out_root: str) -> dict:
    """Export and encrypt one project. Returns a summary dict."""
    # Imported here so --help works without the Google libraries installed.
    import encrypt_snapshot
    import export_snapshot

    # A fresh config per project: each one's Settings tab overlays it, and the
    # overlay of the previous project must not leak into the next.
    config = load_config(config_path)
    projects_registry.apply_project(config, project)

    try:
        applied = settings_sheet.apply_from_sheet(config)
        if applied:
            logger.info(f"  settings from the sheet: {len(applied)} value(s)")
    except Exception as exc:
        # A project with no Settings tab yet is normal on its first publish.
        logger.info(f"  no settings overlay ({type(exc).__name__})")

    out_dir = os.path.join(out_root, project["id"])
    # Start clean: a tab renamed in Settings would otherwise leave its old
    # encrypted file behind for the page to keep showing.
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    manifest = export_snapshot.export(config, out_dir)
    manifest_rows = sum(w.get("row_count", 0) for w in manifest["worksheets"])

    data_key = project.get("data_key") or ""
    if len(data_key) < 8:
        raise RuntimeError(
            f"project '{project['id']}' has no usable data_key in the control "
            "sheet, so its data cannot be encrypted")

    salt = os.urandom(encrypt_snapshot.SALT_BYTES)   # one per project per publish
    count = 0
    for path in sorted(glob.glob(os.path.join(out_dir, "*.json"))):
        if path.endswith(".enc.json"):
            continue
        with open(path, "rb") as fh:
            payload = encrypt_snapshot.encrypt_bytes(fh.read(), data_key, salt=salt)
        out_path = path.replace(".json", ".enc.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.remove(path)          # the cleartext must never reach the site
        count += 1

    return {"id": project["id"], "name": project.get("name", ""),
            "worksheets": len(manifest["worksheets"]), "rows": manifest_rows,
            "files": count, "dir": out_dir}


def assert_no_cleartext(out_root: str) -> None:
    """Cheap insurance against a readable copy of a sheet reaching a public URL."""
    stray = [p for p in glob.glob(os.path.join(out_root, "**", "*.json"), recursive=True)
             if not p.endswith(".enc.json")]
    if stray:
        raise RuntimeError("cleartext JSON found under " + out_root + ": " +
                           ", ".join(sorted(stray)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Publish every project's data")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--project", default=os.environ.get("PROJECT_ID") or None,
                    help="publish only this project (default: every active one)")
    args = ap.parse_args()

    config = load_config(args.config)
    setup_logging_from_config(config)

    if not projects_registry.is_enabled(config):
        sys.exit("no control spreadsheet configured (control.spreadsheet_id)")

    registry = projects_registry.registry_from_config(config)
    projects = registry.list()
    if args.project:
        projects = [p for p in projects
                    if p["id"].lower() == args.project.lower()]
        if not projects:
            sys.exit(f"no active project with id {args.project!r}")
    if not projects:
        sys.exit("the control sheet lists no active projects")

    done, failed = [], []
    for project in projects:
        logger.info(f"── {project['id']}  ({project.get('name', '')})")
        try:
            done.append(publish_one(project, args.config, args.out))
        except Exception as exc:
            # One unreachable project must not stop the others from publishing.
            logger.error(f"  FAILED: {type(exc).__name__}: {exc}")
            failed.append((project["id"], f"{type(exc).__name__}: {exc}"))

    assert_no_cleartext(args.out)

    logger.info("")
    for row in done:
        logger.info(f"published {row['id']:<20s} {row['worksheets']} tabs, "
                    f"{row['rows']} rows, {row['files']} encrypted files")
    for project_id, error in failed:
        logger.error(f"FAILED    {project_id:<20s} {error}")

    if failed:
        sys.exit(f"{len(failed)} of {len(projects)} project(s) failed to publish")


if __name__ == "__main__":
    main()
