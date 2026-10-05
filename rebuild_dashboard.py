#!/usr/bin/env python3
"""
rebuild_dashboard.py
====================
Step 9 of the courier CSAT workflow.
Runs after the master CSV and Excel uploads are complete.

What it does:
  1. Downloads master_classified_all_time.csv from Google Drive
  2. Builds the TRANSCRIPTS[] JS array (Pillars 1-4, in_X == 1)
  3. Injects it into dashboard_template.html (stored in this repo)
  4. Uploads csat_dashboard_latest.html back to Drive

Add to GitHub Actions workflow (.github/workflows/your_workflow.yml):

    - name: Rebuild HTML Dashboard
      env:
        GDRIVE_SERVICE_ACCOUNT_JSON: ${{ secrets.GDRIVE_SERVICE_ACCOUNT_JSON }}
      run: python rebuild_dashboard.py
"""

import os, io, json, re, sys
import pandas as pd
from datetime import datetime, timezone

# ── Google Drive imports ──────────────────────────────────────────────────────
try:
        from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
except ImportError:
    sys.exit("Missing google packages. Add to requirements.txt:\n"
             "  google-api-python-client\n  google-auth")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG — edit only these values if folder IDs or filenames change
# ══════════════════════════════════════════════════════════════════════════════
DRIVE_FOLDER_ID  = "1AJvXVyAi0up23juK1RDQFqc2QoDQ7W-K"
MASTER_CSV_NAME  = "master_classified_all_time.csv"
TEMPLATE_HTML    = "dashboard_template.html"     # lives in the GitHub repo
OUTPUT_HTML_NAME = "csat_dashboard_latest.html"  # uploaded to Drive each run
SCOPES           = ["https://www.googleapis.com/auth/drive"]

PILLAR_1_4 = {"Pillar 1", "Pillar 2", "Pillar 3", "Pillar 4"}

PILLAR_NAME_FIX = {
    "Support Quality":           "Support Quality",
    "App / Tech Issues":         "App / Tech Issues",
    "Partner & External Delays": "Partner and External Delays",
    "Compensation":              "Compensation",
}

# ══════════════════════════════════════════════════════════════════════════════
# GOOGLE DRIVE HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def get_drive_service():
    import google.auth
    creds, _ = google.auth.default(scopes=SCOPES)
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
# TRANSCRIPTS BUILDER
# ══════════════════════════════════════════════════════════════════════════════
def build_transcripts_js(df):
    """
    Filter master CSV to Pillars 1-4 where in_X == 1.
    Falls back to all P1-4 rows if in_X==1 returns nothing
    (handles pipeline lag on the most recent week).
    Returns a JS var TRANSCRIPTS=[...]; string.
    """
    p14 = df[df["pillar"].isin(PILLAR_1_4)].copy()

    # Primary filter: in_X == 1
    use = p14[p14["in_X"] == 1].copy()

    if len(use) == 0:
        print("  WARNING: in_X==1 returned 0 P1-4 rows — "
              "falling back to all P1-4 rows (pipeline lag)")
        use = p14.copy()

    # Normalise fields
    use["dash_pillar"] = use["pillar_name"].map(PILLAR_NAME_FIX).fillna(use["pillar_name"])
    use["Q1"]          = use.apply(
        lambda r: "1-2\u2605" if r["csat_negative"] == 1 else "3-5\u2605", axis=1
    )
    use["Q2"]          = use["resolved"].apply(lambda r: "Yes" if r == 1 else "No")
    use["Date_str"]    = pd.to_datetime(use["Date"]).dt.strftime("%Y-%m-%d")

    rows = []
    for _, r in use.sort_values("Date_str").iterrows():
        rows.append([
            str(r["dash_pillar"]),
            str(r.get("theme", "")),
            str(r["Date_str"]),
            str(r["market"]),
            str(r["platform"]),
            str(r["Q1"]),
            str(r["Q2"]),
            str(r["q3_text"]) if pd.notna(r["q3_text"]) else "",
        ])

    header   = '["Pillar","Cluster","Date","Market","Platform","Q1","Q2","Q3 Text"]'
    data_str = ",\n".join(
        "[" + ",".join(json.dumps(v, ensure_ascii=False) for v in row) + "]"
        for row in rows
    )
    print(f"  TRANSCRIPTS rows built: {len(rows):,}")
    return f"var TRANSCRIPTS=[\n{header},\n{data_str}\n];"


# ══════════════════════════════════════════════════════════════════════════════
# HTML REBUILD
# ══════════════════════════════════════════════════════════════════════════════
def inject_transcripts(template_html, transcripts_js):
    """Replace the TRANSCRIPTS placeholder in the template with live data."""
    pattern = r'var TRANSCRIPTS=\[.*?\];'
    if not re.search(pattern, template_html, flags=re.DOTALL):
        sys.exit("ERROR: Could not find TRANSCRIPTS placeholder in template HTML. "
                 "Ensure dashboard_template.html contains: var TRANSCRIPTS=[...];"
                 )
    updated = re.sub(pattern, transcripts_js, template_html, flags=re.DOTALL)
    return updated


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"  Dashboard Rebuild  —  {ts}")
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
    print(f"  Date range:  {df['week_start'].min()} → {df['week_start'].max()}")

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
    print(f"  ✅ Done — {OUTPUT_HTML_NAME} is live on Drive")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
