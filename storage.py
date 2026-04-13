import logging
import os

import pandas as pd

logger = logging.getLogger(__name__)


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
    if existing_df.empty:
        dedup_key = "Job Link" if "Job Link" in new_df.columns else ["Company", "Role"]
        before = len(new_df)
        result = new_df.drop_duplicates(subset=dedup_key, keep="first")
        logger.info(
            f"Deduplication (within run): {before - len(result)} duplicates removed"
        )
        return result

    combined = pd.concat([existing_df, new_df], ignore_index=True)
    before = len(combined)
    dedup_key = "Job Link" if "Job Link" in combined.columns else ["Company", "Role"]
    combined.drop_duplicates(subset=dedup_key, keep="first", inplace=True)
    after = len(combined)
    new_count = after - len(existing_df)
    logger.info(
        f"Deduplication: {before - after} duplicates removed | "
        f"{new_count} new jobs added | {after} total records"
    )
    return combined


def save(df: pd.DataFrame, filepath: str) -> None:
    """Save DataFrame to CSV, creating parent directories as needed."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    try:
        df.to_csv(filepath, index=False, encoding="utf-8")
        logger.info(f"Saved {len(df)} records to '{filepath}'")
    except Exception as exc:
        logger.error(f"Failed to save '{filepath}': {exc}")
        raise
