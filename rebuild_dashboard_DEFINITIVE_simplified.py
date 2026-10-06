#!/usr/bin/env python3
"""rebuild_dashboard_DEFINITIVE.py — Simplified for dynamic template.
Auth: ADC. Injects TRANSCRIPTS only — template handles all rendering.
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
MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

PILLAR_1_4 = {"Pillar 1","Pillar 2","Pillar 3","Pillar 4"}

PILLAR_NAME_FIX = {
    "Support Quality":           "Support Quality",
    "App / Tech Issues":         "App / Tech Issues",
    "Partner & External Delays": "Partner and External Delays",
    "Compensation":              "Compensation",
    "Pillar 1": "Support Quality",
    "Pillar 2": "App / Tech Issues",
    "Pillar 3": "Partner and External Delays",
    "Pillar 4": "Compensation",
}


def get_service():
    creds, _ = google.auth.default(scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_file(svc, name, folder_id):
    q = 'name="{}" and "{}" in parents and trashed=false'.format(name, folder_id)
    res = svc.files().list(
        q=q, fields="files(id,name,modifiedTime)", orderBy="modifiedTime desc",
        pageSize=5, supportsAllDrives=True,
        includeItemsFromAllDrives=True, corpora="allDrives"
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def dl_bytes(svc, fid):
    req = svc.files().get_media(fileId=fid)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf


def upload(svc, name, data_bytes, folder_id, mime, existing_id=None):
    media = MediaIoBaseUpload(io.BytesIO(data_bytes), mimetype=mime, resumable=True)
    if existing_id:
        svc.files().update(fileId=existing_id, media_body=media,
                           supportsAllDrives=True).execute()
        print("  Updated: {}".format(name))
    else:
        meta = {"name": name, "mimeType": mime, "parents": [folder_id]}
        svc.files().create(body=meta, media_body=media, fields="id",
                           supportsAllDrives=True).execute()
        print("  Created: {}".format(name))


def build_transcripts(df):
    p14 = df[df["pillar"].isin(PILLAR_1_4)].copy()
    use = p14[p14["in_X"] == 1].copy()
    if len(use) == 0:
        print("  WARNING: in_X==1 empty, using all P1-4")
        use = p14.copy()

    use["dash_pillar"] = use["pillar_name"].map(PILLAR_NAME_FIX).fillna(use["pillar_name"])

    def _q1(r):
        if r["csat_negative"] == 1: return "1-2\u2605"
        if r["csat_positive"] == 1: return "3-5\u2605"
        if r["pillar"] in PILLAR_1_4: return "1-2\u2605"
        return "3-5\u2605"

    use["Q1"] = use.apply(_q1, axis=1)
    use["Q2"] = use["resolved"].apply(lambda r: "Yes" if r == 1 else "No")
    use["Date_str"] = pd.to_datetime(use["Date"], errors="coerce").dt.strftime("%Y-%m-%d")

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
            str(r["q3_text"]) if pd.notna(r.get("q3_text")) else ""
        ])

    header = '["Pillar","Cluster","Date","Market","Platform","Q1","Q2","Q3 Text"]'
    data = ",\n".join(
        "[" + ",".join(json.dumps(v, ensure_ascii=True) for v in row) + "]"
        for row in rows
    )
    print("  TRANSCRIPTS rows: {:,}".format(len(rows)))
    js = "var TRANSCRIPTS=[\n{},\n{}\n];".format(header, data)
    js = js.replace("</script>", "<\\/script>").replace("<!--", "<\\!--")
    return js, len(rows)


def inject(template, transcripts_js):
    pat = r'var TRANSCRIPTS=\[.*?\];'
    if not re.search(pat, template, flags=re.DOTALL):
        sys.exit("ERROR: TRANSCRIPTS placeholder not found in template")
    return re.sub(pat, lambda m: transcripts_js, template, flags=re.DOTALL)


def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("\n{}\n  Dashboard Rebuild  {}\n{}".format("="*60, ts, "="*60))

    print("\n[1/4] Authenticating (ADC)...")
    svc = get_service()
    print("  OK")

    print("\n[2/4] Downloading {}...".format(MASTER_CSV_NAME))
    cid = find_file(svc, MASTER_CSV_NAME, DRIVE_FOLDER_ID)
    if not cid:
        sys.exit("ERROR: {} not found in Drive folder".format(MASTER_CSV_NAME))
    df = pd.read_csv(dl_bytes(svc, cid))
    print("  Rows: {:,} | {} to {}".format(
        len(df), df["week_start"].min(), df["week_start"].max()))

    print("\n[3/4] Building TRANSCRIPTS (with Q1 star + pillar mapping)...")
    transcripts_js, n_rows = build_transcripts(df)

    print("\n[4/4] Loading template, injecting, uploading...")
    if not os.path.exists(TEMPLATE_HTML):
        sys.exit("ERROR: {} not found".format(TEMPLATE_HTML))
    with open(TEMPLATE_HTML, "r", encoding="utf-8") as f:
        template = f.read()

    html = inject(template, transcripts_js)
    print("  Final HTML: {:,} chars".format(len(html)))

    eid = find_file(svc, OUTPUT_HTML_NAME, DRIVE_FOLDER_ID)
    upload(svc, OUTPUT_HTML_NAME, html.encode("utf-8"), DRIVE_FOLDER_ID, "text/html", eid)

    print("\n{}\n  Done - {} is live\n{}\n".format("="*60, OUTPUT_HTML_NAME, "="*60))


if __name__ == "__main__":
    main()
