#!/usr/bin/env python3
"""
rebuild_dashboard.py
====================
Captures ALL Q1 respondents (both with and without Q3 comments)
and maps them into their respective categories.
"""

import os, io, json, re, sys
import pandas as pd
from datetime import datetime, timezone

# ── Google Drive imports ──────────────────────────────────────────────────────
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
except ImportError:
    sys.exit("Missing google packages. Add to requirements.txt:\n"
             "  google-api-python-client\n  google-auth")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
DRIVE_FOLDER_ID  = os.environ.get("DRIVE_FOLDER_ID", "1AJvXVyAi0up23juK1RDQFqc2QoDQ7W-K")
MASTER_CSV_NAME  = "master_classified_all_time.csv"
TEMPLATE_HTML    = "dashboard_template.html"     # lives in GitHub repo
OUTPUT_HTML_NAME = "csat_dashboard_latest.html"  # uploaded back to Drive
SCOPES           = ["https://www.googleapis.com/auth/drive"]

# Mapping dictionary to standardize category names
PILLAR_NAME_FIX = {
    "Pillar 1": "Support Quality",
    "Pillar 2": "App / Tech Issues",
    "Pillar 3": "Partner and External Delays",
    "Pillar 4": "Compensation",
    "Pillar 5": "General / Minimal",
    "Support Quality": "Support Quality",
    "App / Tech Issues": "App / Tech Issues",
    "Partner & External Delays": "Partner and External Delays",
    "Partner and External Delays": "Partner and External Delays",
    "Compensation": "Compensation",
    "General / Minimal": "General / Minimal",
}

# ══════════════════════════════════════════════════════════════════════════════
# GOOGLE DRIVE HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def get_drive_service():
    """Authenticate using service account JSON stored in GitHub Secret."""
    creds_raw = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "")
    if not creds_raw:
        sys.exit("ERROR: GDRIVE_SERVICE_ACCOUNT_JSON secret not set.")
    creds_info = json.loads(creds_raw)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_file(service, name, folder_id):
    """Return file ID of the first match (or None)."""
    q = f"name=\"{name}\" and \"{folder_id}\" in parents and trashed=false"
    res = service.files().list(q=q, fields="files(id,name)", pageSize=5).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def download_bytes(service, file_id):
    """Download a Drive file and return its bytes."""
    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf


def upload_html(service, name, html_bytes, folder_id, existing_id=None):
    """Upload (or update) an HTML file on Drive."""
    mime = "text/html"
    media = MediaIoBaseUpload(io.BytesIO(html_bytes), mimetype=mime, resumable=True)
    if existing_id:
        service.files().update(fileId=existing_id, media_body=media).execute()
        print(f"  Updated:  {name} (id={existing_id})")
    else:
        meta = {"name": name, "mimeType": mime, "parents": [folder_id]}
        f = service.files().create(body=meta, media_body=media, fields="id").execute()
        print(f"  Created:  {name} (id={f.get('id')})")


# ══════════════════════════════════════════════════════════════════════════════
# TRANSCRIPTS BUILDER (INCLUDES ALL Q1 RESPONDENTS)
# ══════════════════════════════════════════════════════════════════════════════
def build_transcripts_js(df):
    """
    Captures ALL Q1 respondents:
      - Couriers who left Q3 comments (Pillars 1-4)
      - Couriers who only left Q1 ratings (Pillar 5 / General & Minimal)
    """
    # 1. Keep all rows that have a valid rating (either csat_negative or Q1 column)
    if "csat_negative" in df.columns:
        valid_df = df[df["csat_negative"].notna()].copy()
    elif "Q1" in df.columns:
        valid_df = df[df["Q1"].notna()].copy()
    else:
        valid_df = df.copy()

    # 2. Assign Category / Pillar
    def get_pillar_category(row):
        # Check pillar or pillar_name column
        raw_pillar = str(row.get("pillar_name", row.get("pillar", "")))
        if raw_pillar in PILLAR_NAME_FIX:
            return PILLAR_NAME_FIX[raw_pillar]
        
        # If Q3 text is missing or blank, assign to General / Minimal
        text = str(row.get("q3_text", "")).strip()
        if not text or text.lower() in ("nan", "none", ""):
            return "General / Minimal"
        return "General / Minimal"

    valid_df["dash_pillar"] = valid_df.apply(get_pillar_category, axis=1)

    # 3. Standardize Q1 (1-2★ vs 3-5★)
    def format_q1(r):
        if "csat_negative" in r and pd.notna(r["csat_negative"]):
            return "1-2\u2605" if r["csat_negative"] == 1 else "3-5\u2605"
        if "Q1" in r and pd.notna(r["Q1"]):
            return "1-2\u2605" if str(r["Q1"]).startswith("1-2") else "3-5\u2605"
        return "3-5\u2605"

    valid_df["Q1_fmt"] = valid_df.apply(format_q1, axis=1)

    # 4. Standardize Q2 (Yes vs No)
    def format_q2(val):
        if pd.isna(val):
            return "No"
        return "Yes" if str(val).strip() in ("1", "1.0", "Yes", "yes", "True") else "No"

    valid_df["Q2_fmt"] = valid_df["resolved"].apply(format_q2) if "resolved" in valid_df.columns else "No"

    # 5. Standardize Date
    date_col = "Date" if "Date" in valid_df.columns else "date"
    valid_df["Date_str"] = pd.to_datetime(valid_df[date_col]).dt.strftime("%Y-%m-%d")

    # 6. Build records
    rows = []
    for _, r in valid_df.sort_values("Date_str", ascending=False).iterrows():
        raw_text = r.get("q3_text", "")
        has_text = pd.notna(raw_text) and str(raw_text).strip() not in ("", "nan", "None")

        q3_text = str(raw_text).strip() if has_text else "[Rating Only \u2014 No Written Comment]"
        cluster = str(r.get("theme", "")) if has_text else "Rating Only"

        rows.append([
            str(r["dash_pillar"]),
            cluster,
            str(r["Date_str"]),
            str(r.get("market", "")),
            str(r.get("platform", "")),
            str(r["Q1_fmt"]),
            str(r["Q2_fmt"]),
            q3_text,
        ])

    header = '["Pillar","Cluster","Date","Market","Platform","Q1","Q2","Q3 Text"]'
    data_str = ",\n".join(
        "[" + ",".join(json.dumps(v, ensure_ascii=False) for v in row) + "]"
        for row in rows
    )
    
    q1_only_count = sum(1 for row in rows if row[7].startswith("[Rating Only"))
    print(f"  TRANSCRIPTS rows built: {len(rows):,}")
    print(f"    - With Q3 text:      {len(rows) - q1_only_count:,}")
    print(f"    - Rating only (Q1):  {q1_only_count:,}")

    return f"var TRANSCRIPTS=[\n{header},\n{data_str}\n];"


# ══════════════════════════════════════════════════════════════════════════════
# HTML REBUILD
# ══════════════════════════════════════════════════════════════════════════════
def inject_transcripts(template_html, transcripts_js):
    """Replace the TRANSCRIPTS placeholder in the template with live data."""
    pattern = r'var TRANSCRIPTS=\[.*?\];'
    if not re.search(pattern, template_html, flags=re.DOTALL):
        sys.exit("ERROR: Could not find TRANSCRIPTS placeholder in template HTML. "
                 "Ensure dashboard_template.html contains: var TRANSCRIPTS=[...];")
    updated = re.sub(pattern, transcripts_js, template_html, flags=re.DOTALL)
    return updated


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"  Dashboard Rebuild (All Q1 Feedback) — {ts}")
    print(f"{'='*60}")

    # 1. Auth
    print("\n[1/5] Authenticating with Google Drive...")
    service = get_drive_service()
    print("  OK")

    # 2. Download master CSV
    print(f"\n[2/5] Downloading {MASTER_CSV_NAME} from Drive...")
    csv_id = find_file(service, MASTER_CSV_NAME, DRIVE_FOLDER_ID)
    if not csv_id:
        sys.exit(f"ERROR: {MASTER_CSV_NAME} not found in folder {DRIVE_FOLDER_ID}")
    csv_buf = download_bytes(service, csv_id)
    df = pd.read_csv(csv_buf)
    print(f"  Rows loaded: {len(df):,}")

    # 3. Build TRANSCRIPTS JS
    print("\n[3/5] Building TRANSCRIPTS array...")
    transcripts_js = build_transcripts_js(df)
    print(f"  JS size: {len(transcripts_js):,} chars")

    # 4. Load template + inject
    print(f"\n[4/5] Loading template ({TEMPLATE_HTML}) and injecting data...")
    if not os.path.exists(TEMPLATE_HTML):
        sys.exit(f"ERROR: {TEMPLATE_HTML} not found. "
                 "Ensure it is committed to the repo root.")
    with open(TEMPLATE_HTML, "r", encoding="utf-8") as f:
        template = f.read()
    final_html = inject_transcripts(template, transcripts_js)
    print(f"  Final HTML size: {len(final_html):,} chars")

    # 5. Upload to Drive
    print(f"\n[5/5] Uploading {OUTPUT_HTML_NAME} to Drive...")
    existing_id = find_file(service, OUTPUT_HTML_NAME, DRIVE_FOLDER_ID)
    upload_html(
        service, OUTPUT_HTML_NAME,
        final_html.encode("utf-8"),
        DRIVE_FOLDER_ID, existing_id
    )

    print(f"\n{'='*60}")
    print(f"  Done — {OUTPUT_HTML_NAME} is live on Drive")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
