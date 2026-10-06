#!/usr/bin/env python3
"""rebuild_dashboard.py - Step 9 of courier CSAT workflow.
Auth: ADC | Fixes: script escaping, lambda re.sub, dynamic header KPIs
"""
import os, io, json, re, sys
import pandas as pd
from datetime import datetime, timezone

try:
    import google.auth
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
except ImportError:
    sys.exit("Missing: pip install google-api-python-client google-auth")

DRIVE_FOLDER_ID  = "1AJvXVyAi0up23juK1RDQFqc2QoDQ7W-K"
MASTER_CSV_NAME  = "master_classified_all_time.csv"
TEMPLATE_HTML    = "dashboard_template.html"
OUTPUT_HTML_NAME = "csat_dashboard_latest.html"
SCOPES           = ["https://www.googleapis.com/auth/drive"]
PILLAR_1_4       = {"Pillar 1","Pillar 2","Pillar 3","Pillar 4"}
PILLAR_NAME_FIX  = {
    "Support Quality":"Support Quality",
    "App / Tech Issues":"App / Tech Issues",
    "Partner & External Delays":"Partner and External Delays",
    "Compensation":"Compensation",
}
MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

# ── AUTH ──────────────────────────────────────────────────────
def get_drive_service():
    creds, _ = google.auth.default(scopes=SCOPES)
    return build("drive","v3",credentials=creds,cache_discovery=False)

# ── DRIVE ─────────────────────────────────────────────────────
def find_file(service, name, folder_id):
    q = f'name="{name}" and "{folder_id}" in parents and trashed=false'
    res = service.files().list(
        q=q, fields="files(id,name,modifiedTime)", orderBy="modifiedTime desc",
        pageSize=5, supportsAllDrives=True,
        includeItemsFromAllDrives=True, corpora="allDrives"
    ).execute()
    files = res.get("files",[])
    return files[0]["id"] if files else None

def download_bytes(service, file_id):
    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done: _, done = dl.next_chunk()
    buf.seek(0)
    return buf

def upload_html(service, name, html_bytes, folder_id, existing_id=None):
    mime = "text/html"
    media = MediaIoBaseUpload(io.BytesIO(html_bytes), mimetype=mime, resumable=True)
    if existing_id:
        service.files().update(fileId=existing_id, media_body=media,
                               supportsAllDrives=True).execute()
        print(f"  Updated: {name}")
    else:
        meta = {"name":name,"mimeType":mime,"parents":[folder_id]}
        f = service.files().create(body=meta, media_body=media, fields="id",
                                   supportsAllDrives=True).execute()
        print(f"  Created: {name}")

# ── TRANSCRIPTS ───────────────────────────────────────────────
def build_transcripts_js(df):
    p14 = df[df["pillar"].isin(PILLAR_1_4)].copy()
    use = p14[p14["in_X"]==1].copy()
    if len(use)==0:
        print("  WARNING: in_X==1 empty, using all P1-4")
        use = p14.copy()
    use["dash_pillar"] = use["pillar_name"].map(PILLAR_NAME_FIX).fillna(use["pillar_name"])
    use["Q1"] = use.apply(lambda r: "1-2" if r["csat_negative"]==1 else "3-5", axis=1)
    use["Q2"] = use["resolved"].apply(lambda r: "Yes" if r==1 else "No")
    use["Date_str"] = pd.to_datetime(use["Date"]).dt.strftime("%Y-%m-%d")
    rows = []
    for _, r in use.sort_values("Date_str").iterrows():
        rows.append([
            str(r["dash_pillar"]), str(r.get("theme","")),
            str(r["Date_str"]), str(r["market"]), str(r["platform"]),
            str(r["Q1"]), str(r["Q2"]),
            str(r["q3_text"]) if pd.notna(r["q3_text"]) else "",
        ])
    header = '["Pillar","Cluster","Date","Market","Platform","Q1","Q2","Q3 Text"]'
    data_str = ",\n".join(
        "["+",".join(json.dumps(v, ensure_ascii=True) for v in row)+"]"
        for row in rows
    )
    print(f"  TRANSCRIPTS rows: {len(rows):,}")
    js = f"var TRANSCRIPTS=[\n{header},\n{data_str}\n];"
    # Escape </script> to prevent premature tag closure
    js = js.replace("</script>","<\\/script>").replace("<!--","<\\!--")
    return js, len(rows)

# ── INJECT ─────────────────────────────────────────────────────
def inject_transcripts(template_html, transcripts_js):
    pattern = r'var TRANSCRIPTS=\[.*?\];'
    if not re.search(pattern, template_html, flags=re.DOTALL):
        sys.exit("ERROR: TRANSCRIPTS placeholder not found")
    # Use lambda to prevent re.sub interpreting \u as escape
    return re.sub(pattern, lambda m: transcripts_js, template_html, flags=re.DOTALL)

# ── HEADER KPI UPDATE ──────────────────────────────────────────
def update_header_kpis(html, df, n_transcripts):
    """Replace hardcoded header KPI values with live computed values."""
    valid = int(df["csat_valid"].sum())
    pos   = int(df["csat_positive"].sum())
    neg   = int(df["csat_negative"].sum())
    pos_pct = round(pos/valid*100,1) if valid else 0
    neg_pct = round(neg/valid*100,1) if valid else 0
    total_q3 = len(df)

    dates   = pd.to_datetime(df["submitted_at"], errors="coerce").dropna()
    min_d, max_d = dates.min(), dates.max()
    min_str = f"{MONTHS[min_d.month-1]} {min_d.day}"
    max_str = f"{MONTHS[max_d.month-1]} {max_d.day}, {max_d.year}"
    new_range = f"{min_str} \u2013 {max_str}"

    now = datetime.now(timezone.utc)
    new_week = f"W{now.isocalendar()[1]:02d}_{now.year}"

    print(f"  Header update: {new_range} | Q1:{valid:,} | Pos:{pos_pct}% | Neg:{neg_pct}% | Q3:{n_transcripts:,}")

    # Week label
    html = re.sub(r'W\d{2}_\d{4}', new_week, html, count=5)
    # Date range
    for pat in [r'Jun\s+17\s+[\u2013\-]\s+Sep\s+27,?\s*2026',
                'Jun 17 \u2013 Sep 27, 2026', 'Jun 17 - Sep 27, 2026']:
        try: html = re.sub(pat, new_range, html, count=5)
        except Exception: pass
    # Q3 counts
    html = html.replace('18,670 Q3 transcripts', f'{total_q3:,} Q3 transcripts')
    html = html.replace('7,364', f'{n_transcripts:,}')
    html = html.replace('7364', str(n_transcripts))
    # Q1 counts
    html = html.replace('103,639', f'{valid:,}')
    html = html.replace('103639', str(valid))
    # CSAT percentages
    html = html.replace('49.5%', f'{pos_pct}%', 5)
    html = html.replace('50.5%', f'{neg_pct}%', 5)
    return html

# ── MAIN ───────────────────────────────────────────────────────
def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}\n  Dashboard Rebuild  {ts}\n{'='*60}")

    print("\n[1/5] Authenticating (ADC)...")
    service = get_drive_service()
    print("  OK")

    print(f"\n[2/5] Downloading {MASTER_CSV_NAME}...")
    csv_id = find_file(service, MASTER_CSV_NAME, DRIVE_FOLDER_ID)
    if not csv_id:
        sys.exit(f"ERROR: {MASTER_CSV_NAME} not found")
    df = pd.read_csv(download_bytes(service, csv_id))
    print(f"  Rows: {len(df):,} | Range: {df['week_start'].min()} to {df['week_start'].max()}")

    print("\n[3/5] Building TRANSCRIPTS array...")
    transcripts_js, n_rows = build_transcripts_js(df)

    print(f"\n[4/5] Loading {TEMPLATE_HTML} and updating...")
    if not os.path.exists(TEMPLATE_HTML):
        sys.exit(f"ERROR: {TEMPLATE_HTML} not found in repo")
    with open(TEMPLATE_HTML,"r",encoding="utf-8") as f:
        template = f.read()
    final_html = inject_transcripts(template, transcripts_js)
    final_html = update_header_kpis(final_html, df, n_rows)
    print(f"  Final HTML: {len(final_html):,} chars")

    print(f"\n[5/5] Uploading {OUTPUT_HTML_NAME}...")
    existing_id = find_file(service, OUTPUT_HTML_NAME, DRIVE_FOLDER_ID)
    upload_html(service, OUTPUT_HTML_NAME, final_html.encode("utf-8"),
                DRIVE_FOLDER_ID, existing_id)

    print(f"\n{'='*60}\n  Done - {OUTPUT_HTML_NAME} is live\n{'='*60}\n")

if __name__ == "__main__":
    main()
