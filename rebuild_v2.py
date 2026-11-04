#!/usr/bin/env python3
"""
rebuild_v2.py
Fetches updated data from Google Drive, calculates all weekly & all-time metrics,
and updates data.json so index.html displays all charts and Q1-only feedback.
"""

import os, io, json, sys
import pandas as pd
from datetime import datetime, timezone

# Google Drive imports
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
except ImportError:
    sys.exit("Missing google libraries. Ensure google-api-python-client and google-auth are in requirements.txt")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
DRIVE_FOLDER_ID   = os.environ.get("DRIVE_FOLDER_ID", "1AJvXVyAi0up23juK1RDQFqc2QoDQ7W-K")
COMBINED_CSV_NAME = "combined_all_time.csv"
DATA_JSON_FILE    = "data.json"
SCOPES            = ["https://www.googleapis.com/auth/drive"]

PILLAR_ORDER = [
    "Support Quality",
    "App / Tech Issues",
    "Partner and External Delays",
    "Compensation",
    "General / Minimal"
]

PILLAR_COLORS = {
    "Support Quality": "#e74c3c",
    "App / Tech Issues": "#e67e22",
    "Partner and External Delays": "#16a085",
    "Compensation": "#2980b9",
    "General / Minimal": "#7f8c8d"
}

def get_drive_service():
    creds_raw = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "")
    if not creds_raw:
        sys.exit("ERROR: GDRIVE_SERVICE_ACCOUNT_JSON secret not set.")
    creds_info = json.loads(creds_raw)
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def download_file(service, name, folder_id):
    q = f'name="{name}" and "{folder_id}" in parents and trashed=false'
    res = service.files().list(q=q, fields="files(id,name)", pageSize=5).execute()
    files = res.get("files", [])
    if not files:
        # Fallback check for alternate master CSV names
        q_alt = f'name="master_classified_all_time.csv" and "{folder_id}" in parents and trashed=false'
        res_alt = service.files().list(q=q_alt, fields="files(id,name)", pageSize=5).execute()
        files = res_alt.get("files", [])
        if not files:
            raise FileNotFoundError(f"Neither {name} nor master_classified_all_time.csv found in folder {folder_id}")

    file_id = files[0]["id"]
    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf

def upload_file(service, name, file_bytes, mime_type, folder_id):
    q = f'name="{name}" and "{folder_id}" in parents and trashed=false'
    res = service.files().list(q=q, fields="files(id)", pageSize=1).execute()
    existing_id = res.get("files", [])[0]["id"] if res.get("files") else None

    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=True)
    if existing_id:
        service.files().update(fileId=existing_id, media_body=media).execute()
        print(f"  Drive file updated: {name} (id={existing_id})")
    else:
        meta = {"name": name, "mimeType": mime_type, "parents": [folder_id]}
        f = service.files().create(body=meta, media_body=media, fields="id").execute()
        print(f"  Drive file created: {name} (id={f.get('id')})")

def build_data_payload(df_raw):
    # Pivot tall schema if raw Q1/Q2/Q3 are in row format
    if "question" in df_raw.columns and "Response" in df_raw.columns:
        print("  Reshaping tall question format...")
        piv = df_raw.pivot_table(
            index=["courier_id", "Date", "market", "platform"],
            columns="question",
            values="Response",
            aggfunc="first"
        ).reset_index()
        piv.columns.name = None
    else:
        piv = df_raw.copy()

    # Column name normalization
    q1_col = "Q1_Rating" if "Q1_Rating" in piv.columns else ("Q1" if "Q1" in piv.columns else "rating")
    q2_col = "Q2_Resolved" if "Q2_Resolved" in piv.columns else ("Q2" if "Q2" in piv.columns else "resolved")
    q3_col = "Q3_Feedback" if "Q3_Feedback" in piv.columns else ("Q3" if "Q3" in piv.columns else "q3_text")

    # Dates and weeks (ISO Monday)
    dt_series = pd.to_datetime(piv["Date"])
    piv["d"] = dt_series.dt.strftime("%Y-%m-%d %H:%M:%S")
    mondays = dt_series - pd.to_timedelta(dt_series.dt.weekday, unit="D")
    piv["w"] = mondays.dt.strftime("%Y-%m-%d")

    piv["m"] = piv["market"].astype(str)
    piv["p"] = piv["platform"].astype(str)
    piv["c"] = piv["courier_id"].astype(str)

    # Q1 formatting
    def parse_q1(val):
        s = str(val).strip()
        if s in ("1", "2", "1-2", "1-2★") or s.startswith("1") or s.startswith("2"):
            return "1-2★", True
        return "3-5★", False

    q1_vals = piv[q1_col].apply(parse_q1)
    piv["q1"] = [v[0] for v in q1_vals]
    piv["is_neg"] = [v[1] for v in q1_vals]

    # Q2 formatting
    piv["q2"] = piv[q2_col].apply(lambda v: "Yes" if str(v).strip().lower() in ("1", "yes", "true") else "No")

    # Split Q1+Q3 transcripts vs Q1-only
    has_q3 = piv[q3_col].notna() & (piv[q3_col].astype(str).str.strip() != "") & (piv[q3_col].astype(str).str.lower() != "nan")

    df_transcripts = piv[has_q3].copy()
    df_q1_only = piv[~has_q3].copy()

    print(f"  Loaded total Q1: {len(piv):,} | With Q3: {len(df_transcripts):,} | Q1-only: {len(df_q1_only):,}")

    # Build q1NoQ3 records
    q1_no_q3_list = []
    for _, r in df_q1_only.iterrows():
        q1_no_q3_list.append({
            "d": r["d"],
            "m": r["m"],
            "p": r["p"],
            "q": r["q1"],
            "c": r["c"],
            "w": r["w"],
            "n": bool(r["is_neg"])
        })

    # Build Q1+Q3 transcript records
    transcripts_list = []
    for _, r in df_transcripts.iterrows():
        pl = r.get("pillar", r.get("pl", "General / Minimal"))
        if pl not in PILLAR_ORDER:
            pl = "General / Minimal"

        transcripts_list.append({
            "d": r["d"],
            "m": r["m"],
            "p": r["p"],
            "q1": r["q1"],
            "q2": r["q2"],
            "c": r["c"],
            "t": str(r.get(q3_col, ""))[:300],
            "pl": pl,
            "w": r["w"],
            "pl12": r.get("pl12", "Others")
        })

    # Compute metrics function
    def compute_stats(rows):
        tot = len(rows)
        if tot == 0:
            return {"total": 0, "negQ1": 0, "posQ1": 0, "resCount": 0, "negQ1Pct": 0.0, "posQ1Pct": 0.0, "resPct": 0.0, "marketNeg": {}, "marketPos": {}, "pillars": {}}
        neg = sum(1 for t in rows if t["q1"] == "1-2★")
        pos = tot - neg
        res = sum(1 for t in rows if t["q2"] == "Yes")

        pillars = {}
        for p in PILLAR_ORDER:
            sub = [t for t in rows if t["pl"] == p]
            stot = len(sub)
            sneg = sum(1 for t in sub if t["q1"] == "1-2★")
            spos = stot - sneg
            sres = sum(1 for t in sub if t["q2"] == "Yes")
            pillars[p] = {
                "total": stot,
                "share": round(stot / tot * 100, 1) if tot else 0.0,
                "neg": sneg,
                "pos": spos,
                "res": sres,
                "negPct": round(sneg / stot * 100, 1) if stot else 0.0,
                "posPct": round(spos / stot * 100, 1) if stot else 0.0,
                "resPct": round(sres / stot * 100, 1) if stot else 0.0
            }

        return {
            "total": tot,
            "negQ1": neg,
            "posQ1": pos,
            "resCount": res,
            "negQ1Pct": round(neg / tot * 100, 1),
            "posQ1Pct": round(pos / tot * 100, 1),
            "resPct": round(res / tot * 100, 1),
            "pillars": pillars
        }

    all_weeks = sorted(list(piv["w"].unique()))
    by_week_dict = {}
    for w in all_weeks:
        w_sub = [t for t in transcripts_list if t["w"] == w]
        by_week_dict[w] = compute_stats(w_sub)

    return {
        "weeks": all_weeks,
        "pillarOrder": PILLAR_ORDER,
        "pillarColors": PILLAR_COLORS,
        "dateRange": {
            "start": piv["d"].min()[:10],
            "end": piv["d"].max()[:10]
        },
        "all": {
            "allTime": compute_stats(transcripts_list),
            "byWeek": by_week_dict,
            "transcripts": transcripts_list
        },
        "gold": {
            "transcripts": []
        },
        "q1NoQ3": {
            "all": q1_no_q3_list,
            "gold": []
        }
    }

def main():
    print(f"\n{'='*60}")
    print(f"  Generating data.json for Option 1 — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    service = get_drive_service()

    # 1. Download
    print(f"\n[1/3] Downloading {COMBINED_CSV_NAME} from Google Drive...")
    buf = download_file(service, COMBINED_CSV_NAME, DRIVE_FOLDER_ID)
    df_raw = pd.read_csv(buf)

    # 2. Process
    print("\n[2/3] Computing stats and assembling full data.json...")
    payload = build_data_payload(df_raw)

    # Save locally in GitHub runner
    with open(DATA_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"  Local {DATA_JSON_FILE} generated.")

    # 3. Upload back to Drive
    print(f"\n[3/3] Uploading updated {DATA_JSON_FILE} to Google Drive...")
    json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    upload_file(service, DATA_JSON_FILE, json_bytes, "application/json", DRIVE_FOLDER_ID)

    print(f"\n{'='*60}")
    print(f"  SUCCESS: data.json updated! index.html is ready.")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
