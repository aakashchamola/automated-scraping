import logging
import os

import pandas as pd

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ["Company", "Role", "Location", "Platform", "Job Link"]
OPTIONAL_COLUMNS = ["Keyword"]
OUTPUT_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


def _prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    prepared = df.copy()
    for col in OUTPUT_COLUMNS:
        if col not in prepared.columns:
            prepared[col] = ""

    for col in OUTPUT_COLUMNS:
        prepared[col] = prepared[col].fillna("").astype(str).str.strip()

    return prepared[OUTPUT_COLUMNS]


def load_existing(filepath: str) -> pd.DataFrame:
    """Load existing jobs CSV if present; return empty DataFrame otherwise."""
    if os.path.exists(filepath):
        try:
            df = pd.read_csv(filepath)
            logger.info(f"Loaded {len(df)} existing records from '{filepath}'")
            return df
        except Exception as exc:
            logger.warning(f"Could not read existing file '{filepath}': {exc}")
    return pd.DataFrame()


def deduplicate(new_df: pd.DataFrame, existing_df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge new jobs with existing history and remove duplicates.

    Deduplication key: 'Job Link' (platform-unique URL).
    Falls back to ('Company', 'Role') if Job Link is missing.
    """
    existing_prepared = _prepare_df(existing_df) if not existing_df.empty else pd.DataFrame(columns=OUTPUT_COLUMNS)
    new_prepared = _prepare_df(new_df)

    combined = pd.concat([existing_prepared, new_prepared], ignore_index=True)
    before = len(combined)

    with_link = combined[combined["Job Link"] != ""].copy()
    without_link = combined[combined["Job Link"] == ""].copy()

    with_link = with_link.drop_duplicates(
        subset=["Platform", "Job Link"],
        keep="last",
    )
    without_link = without_link.drop_duplicates(
        subset=["Platform", "Company", "Role"],
        keep="last",
    )

    result = pd.concat([with_link, without_link], ignore_index=True)
    after = len(result)
    new_count = after - len(existing_df)
    logger.info(
        f"Deduplication: {before - after} duplicates removed | "
        f"{new_count} new jobs added | {after} total records"
    )
    return result[OUTPUT_COLUMNS]


def save(df: pd.DataFrame, filepath: str) -> None:
    """Save DataFrame to CSV, creating parent directories as needed."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    try:
        prepared = _prepare_df(df)
        prepared.to_csv(filepath, index=False, encoding="utf-8")
        logger.info(f"Saved {len(df)} records to '{filepath}'")
    except Exception as exc:
        logger.error(f"Failed to save '{filepath}': {exc}")
        raise
