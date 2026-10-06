#!/usr/bin/env python3
"""rebuild_dashboard.py - Step 9 of courier CSAT workflow.
Auth: ADC (Application Default Credentials)
Fix: </script> escaping in TRANSCRIPTS data
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

def get_drive_service():
    """ADC auth - same as feedback_agent.py"""
    creds, _ = google.auth.default(scopes=SCOPES)
    return build("drive","v3",credentials=creds,cache_discovery=False)

def find_file(service, name, folder_id):
    q = f'name="{name}" and "{folder_id}" in parents and trashed=false'
    res = service.files().list(
        q=q, fields="files(id,name,modifiedTime)",
        orderBy="modifiedTime desc", pageSize=5,
        supportsAllDrives=True, includeItemsFromAllDrives=True, corpora="allDrives"
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
        service.files().update(
            fileId=existing_id, media_body=media, supportsAllDrives=True
        ).execute()
        print(f"  Updated: {name}")
    else:
        meta = {"name":name,"mimeType":mime,"parents":[folder_id]}
        f = service.files().create(
            body=meta, media_body=media, fields="id", supportsAllDrives=True
        ).execute()
        print(f"  Created: {name} id={f.get(chr(39)+'id'+chr(39))}")

def build_transcripts_js(df):
    p14 = df[df["pillar"].isin(PILLAR_1_4)].copy()
    use = p14[p14["in_X"]==1].copy()
    if len(use)==0:
        print("  WARNING: in_X==1 returned 0 rows, using all P1-4")
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
        "["+",".join(json.dumps(v,ensure_ascii=True) for v in row)+"]"
        for row in rows
    )
    print(f"  TRANSCRIPTS rows: {len(rows):,}")
    js_out = f"var TRANSCRIPTS=[\n{header},\n{data_str}\n];"
    # Escape </script> to prevent premature script tag closure
    js_out = js_out.replace("</script>","<\/script>").replace("<!--","<\!--")
    return js_out

def inject_transcripts(template_html, transcripts_js):
    pattern = r'var TRANSCRIPTS=\[.*?\];'
    if not re.search(pattern, template_html, flags=re.DOTALL):
        sys.exit("ERROR: TRANSCRIPTS placeholder not found in dashboard_template.html")
    return re.sub(pattern, lambda m: transcripts_js, template_html, flags=re.DOTALL)

def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{chr(61)*60}\n  Dashboard Rebuild  {ts}\n{chr(61)*60}")

    print("\n[1/5] Authenticating (ADC)...")
    service = get_drive_service()
    print("  OK")

    print(f"\n[2/5] Downloading {MASTER_CSV_NAME}...")
    csv_id = find_file(service, MASTER_CSV_NAME, DRIVE_FOLDER_ID)
    if not csv_id:
        sys.exit(f"ERROR: {MASTER_CSV_NAME} not found in Drive folder")
    df = pd.read_csv(download_bytes(service, csv_id))
    print(f"  Rows: {len(df):,} | Range: {df['week_start'].min()} to {df['week_start'].max()}")

    print("\n[3/5] Building TRANSCRIPTS array...")
    transcripts_js = build_transcripts_js(df)

    print(f"\n[4/5] Loading {TEMPLATE_HTML}...")
    if not os.path.exists(TEMPLATE_HTML):
        sys.exit(f"ERROR: {TEMPLATE_HTML} not found in repo")
    with open(TEMPLATE_HTML,"r",encoding="utf-8") as f:
        template = f.read()
    final_html = inject_transcripts(template, transcripts_js)
    print(f"  Final HTML: {len(final_html):,} chars")

    print(f"\n[5/5] Uploading {OUTPUT_HTML_NAME}...")
    existing_id = find_file(service, OUTPUT_HTML_NAME, DRIVE_FOLDER_ID)
    upload_html(service, OUTPUT_HTML_NAME, final_html.encode("utf-8"), DRIVE_FOLDER_ID, existing_id)

    print(f"\n{chr(61)*60}\n  Done - {OUTPUT_HTML_NAME} is live\n{chr(61)*60}\n")

if __name__ == "__main__":
    main()
