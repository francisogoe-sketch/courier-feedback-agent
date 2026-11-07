"""
build_master_data.py
--------------------
Automates ingestion of courier survey partitions from Google Drive / incoming drops,
merges them into the persistent all-time Parquet base dataset, deduplicates records,
and regenerates analytical summary files.
"""

import os
import glob
import re
import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# PATH DEFINITIONS
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Historical Parquet baseline (created in data/base/)
BASE_PARQUET = os.path.join(REPO_ROOT, "data", "base", "courier_survey_all_data.parquet")

# Incoming folder where new survey CSV drops are placed
INCOMING_DIR = os.path.join(REPO_ROOT, "data", "incoming")

# Outputs
OUTPUT_SUMMARY_CSV = os.path.join(REPO_ROOT, "data", "outputs", "weekly_courier_survey_summary.csv")
OUTPUT_MASTER_CSV = os.path.join(REPO_ROOT, "weekly_courier_survey_all_data.csv")
LEGACY_TRANSCRIPTS_CSV = os.path.join(REPO_ROOT, "csat_all_transcripts_all.csv")


def load_historical_base():
    """Loads the cumulative Parquet base dataset."""
    if os.path.exists(BASE_PARQUET):
        print(f"Loading historical base dataset from: {BASE_PARQUET}")
        df = pd.read_parquet(BASE_PARQUET)
        df['response_datetime'] = pd.to_datetime(df['response_datetime'])
        print(f"Loaded {len(df):,} historical rows from Parquet base.")
        return df
    elif os.path.exists(OUTPUT_MASTER_CSV):
        print(f"Parquet not found. Loading baseline CSV fallback: {OUTPUT_MASTER_CSV}")
        df = pd.read_csv(OUTPUT_MASTER_CSV)
        df['response_datetime'] = pd.to_datetime(df['response_datetime'])
        return df
    else:
        print("No historical base found. Initializing empty dataset.")
        return pd.DataFrame()


def parse_filename(filepath):
    """
    Extracts metadata from filenames matching:
    '{Region} {Platform} {Date Range} {Question}.csv'
    Example: 'UK iOS Oct 26 - Nov 1 Q1.csv'
    """
    fname = os.path.basename(filepath)
    # Ignore gold courier reference lists and base outputs
    if "gold" in fname.lower():
        return None

    match = re.search(r"([A-Z]{2})\s+(iOS|And)\s+.*?(Q[123])", fname, re.IGNORECASE)
    if not match:
        return None

    region, platform_raw, question = match.groups()
    platform = "Android" if platform_raw.lower() == "and" else "iOS"
    return region.upper(), platform, question.upper(), fname


def process_csv_file(filepath):
    """Reads and normalizes an individual survey CSV partition."""
    meta = parse_filename(filepath)
    if not meta:
        return None

    region, platform, question, fname = meta

    try:
        df = pd.read_csv(filepath)
    except Exception as e:
        print(f"Error reading {fname}: {e}")
        return None

    if df.empty:
        return None

    df.columns = [c.strip() for c in df.columns]

    # Map column names
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if "courier" in cl:
            col_map[c] = "courier_id"
        elif "visitor" in cl:
            col_map[c] = "Visitor ID"
        elif cl == "date":
            col_map[c] = "response_datetime"
        elif cl == "response":
            col_map[c] = "response_original"

    df = df.rename(columns=col_map)

    # Validate essential columns
    for req in ["courier_id", "Visitor ID", "response_datetime", "response_original"]:
        if req not in df.columns:
            print(f"Skipping {fname}: Missing column '{req}'")
            return None

    df["region"] = region
    df["platform"] = platform
    df["question"] = question
    df["source_file"] = fname
    df["response_english"] = df["response_original"]
    df["response_datetime"] = pd.to_datetime(df["response_datetime"])

    return df


def generate_weekly_summary(df):
    """Generates aggregate weekly CSAT summary metrics matching weekly_courier_survey_summary.csv."""
    records = []
    grouped = df.groupby(["week_number", "week_start", "week_end", "region", "platform"])

    for (wn, w_start, w_end, reg, plat), grp in grouped:
        q1 = grp[grp["question"] == "Q1"]
        q2 = grp[grp["question"] == "Q2"]
        q3 = grp[grp["question"] == "Q3"]

        q1_num = pd.to_numeric(q1["response_original"], errors="coerce")
        total_q1 = len(q1_num.dropna())

        q2_resp = q2["response_original"].astype(str).str.strip().str.lower()
        yes_count = (q2_resp == "yes").sum()
        no_count = (q2_resp == "no").sum()
        total_q2 = yes_count + no_count

        records.append({
            "week_number": wn,
            "week_start": w_start,
            "week_end": w_end,
            "region": reg,
            "platform": plat,
            "unique_couriers": grp["courier_id"].nunique(),
            "total_rows": len(grp),
            "q1_responses": total_q1,
            "q1_avg_rating": round(q1_num.mean(), 2) if total_q1 > 0 else np.nan,
            "q1_rating_1_count": (q1_num == 1).sum(),
            "q1_rating_2_count": (q1_num == 2).sum(),
            "q1_rating_3_count": (q1_num == 3).sum(),
            "q1_rating_4_count": (q1_num == 4).sum(),
            "q1_rating_5_count": (q1_num == 5).sum(),
            "q1_pct_positive_4_5": round(((q1_num >= 4).sum() / total_q1) * 100, 1) if total_q1 > 0 else np.nan,
            "q1_pct_negative_1_2": round(((q1_num <= 2).sum() / total_q1) * 100, 1) if total_q1 > 0 else np.nan,
            "q2_responses": total_q2,
            "q2_yes_count": yes_count,
            "q2_no_count": no_count,
            "q2_pct_yes": round((yes_count / total_q2) * 100, 1) if total_q2 > 0 else np.nan,
            "q3_comments_total": len(q3),
            "q3_comments_non_blank": q3["response_original"].dropna().str.strip().ne("").sum(),
            "week_note": ""
        })

    summary_df = pd.DataFrame(records).sort_values(["week_number", "region", "platform"])
    return summary_df


def build_and_update_master():
    """Main workflow to merge new survey data into the historical base."""
    # 1. Load historical base
    base_df = load_historical_base()

    # Clean any internal duplicates within base itself first to get true unique baseline
    if not base_df.empty:
        base_df = base_df.drop_duplicates(
            subset=["courier_id", "question", "response_datetime"],
            keep="last"
        )
    initial_base_rows = len(base_df)
    print(f"Verified unique historical records: {initial_base_rows:,}")

    # 2. Check for newly downloaded files in incoming directory or root
    search_dirs = [INCOMING_DIR, REPO_ROOT]
    new_csvs = []
    for d in search_dirs:
        if os.path.exists(d):
            for f in glob.glob(os.path.join(d, "*.csv")):
                bname = os.path.basename(f)
                if not any(kw in bname.lower() for kw in ["summary", "master", "transcripts", "gold", "weekly_courier_survey_all_data"]):
                    new_csvs.append(f)

    new_dfs = []
    for f in new_csvs:
        parsed = process_csv_file(f)
        if parsed is not None and not parsed.empty:
            new_dfs.append(parsed)

    # 3. Combine base and new files
    if new_dfs:
        print(f"Processing {len(new_dfs)} new partition file(s)...")
        incoming_combined = pd.concat(new_dfs, ignore_index=True)
        full_df = pd.concat([base_df, incoming_combined], ignore_index=True)
    else:
        print("No new raw CSV files found. Validating and refreshing base data.")
        full_df = base_df.copy()

    if full_df.empty:
        print("Error: No data available to build.")
        return

    # 4. Deduplicate on composite primary key
    full_df = full_df.drop_duplicates(
        subset=["courier_id", "question", "response_datetime"],
        keep="last"
    )

    # 5. Calculate chronological week bounds (Monday to Sunday)
    full_df["response_datetime"] = pd.to_datetime(full_df["response_datetime"])
    full_df["week_start"] = (
        full_df["response_datetime"].dt.to_period("W-SUN").dt.start_time.dt.strftime("%Y-%m-%d")
    )
    full_df["week_end"] = (
        full_df["response_datetime"].dt.to_period("W-SUN").dt.end_time.dt.strftime("%Y-%m-%d")
    )

    # Assign sequential week numbering (Week 1 to latest)
    unique_weeks = sorted(full_df["week_start"].unique())
    week_map = {w: i + 1 for i, w in enumerate(unique_weeks)}
    full_df["week_number"] = full_df["week_start"].map(week_map)

    # Sort dataset chronologically
    full_df = full_df.sort_values(["response_datetime", "region", "platform"]).reset_index(drop=True)

    # 6. Safety check: Total records must never drop below historical unique base
    if len(full_df) < initial_base_rows:
        raise ValueError(
            f"SAFETY ABORT: Resulting dataset ({len(full_df):,} rows) has fewer records "
            f"than base ({initial_base_rows:,} rows)! Update halted."
        )

    rows_added = len(full_df) - initial_base_rows
    print(f"Merge & Deduplication complete: {rows_added:,} new rows added.")
    print(f"Total dataset size: {len(full_df):,} rows across {len(unique_weeks)} weeks.")

    # 7. Save updated base Parquet (Primary source of truth)
    os.makedirs(os.path.dirname(BASE_PARQUET), exist_ok=True)
    full_df.to_parquet(BASE_PARQUET, index=False)
    print(f"Successfully saved updated base dataset to: {BASE_PARQUET}")

    # 8. Generate and save summary CSV
    summary_df = generate_weekly_summary(full_df)
    os.makedirs(os.path.dirname(OUTPUT_SUMMARY_CSV), exist_ok=True)
    summary_df.to_csv(OUTPUT_SUMMARY_CSV, index=False)
    print(f"Successfully saved summary metrics to: {OUTPUT_SUMMARY_CSV}")

    # 9. Sync backwards-compatible CSV export
    full_df.to_csv(OUTPUT_MASTER_CSV, index=False)
    print(f"Synced master export: {OUTPUT_MASTER_CSV}")


if __name__ == "__main__":
    build_and_update_master()