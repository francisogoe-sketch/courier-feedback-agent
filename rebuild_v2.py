#!/usr/bin/env python3
"""
rebuild_v2.py
Shared-Drive enabled data loader:
1. Enables supportsAllDrives & includeItemsFromAllDrives for Team Drives
2. Scans folders and locates the survey CSV
3. Builds data.json and syncs to GitHub / Drive
"""

import os, io, json, sys, glob
import pandas as pd
from datetime import datetime, timezone

try:
    import google.auth
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
except ImportError:
    sys.exit("Missing dependencies. Ensure google-api-python-client and google-auth are installed.")

INPUT_FOLDER_ID   = os.environ.get("DRIVE_FOLDER_ID", "1Z2ugAbgFYgmErVrM-TTEJYGhcsx0U9Cm")
OUTPUT_FOLDER_ID  = os.environ.get("OUTPUT_FOLDER_ID", "1AJvXVyAi0up23juK1RDQFqc2QoDQ7W-K")
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
    creds, _ = google.auth.default(scopes=SCOPES)
    print("  Authenticated via Application Default Credentials (ADC)")
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def list_drive_folder(service, folder_id):
    """Lists files inside a Shared/Team Drive folder."""
    q = f"'{folder_id}' in parents and trashed = false"
    res = service.files().list(
        q=q,
        fields="files(id, name, mimeType)",
        pageSize=100,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives"
    ).execute()
    files = res.get("files", [])
    print(f"\n  --- Files in Drive Folder ({folder_id}) ---")
    if not files:
        print("    (Folder is empty)")
    for f in files:
        print(f"    • {f['name']} (id: {f['id']})")
    return {f['name']: f['id'] for f in files}

def download_stream(service, file_id):
    req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf

def upload_file(service, name, file_bytes, mime_type, folder_id):
    q = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    res = service.files().list(
        q=q,
        fields="files(id)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    existing_id = res.get("files", [])[0]["id"] if res.get("files") else None

    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=True)
    if existing_id:
        service.files().update(fileId=existing_id, media_body=media, supportsAllDrives=True).execute()
        print(f"  Drive file updated: {name} (id={existing_id})")
    else:
        meta = {"name": name, "mimeType": mime_type, "parents": [folder_id]}
        f = service.files().create(body=meta, media_body=media, fields="id", supportsAllDrives=True).execute()
        print(f"  Drive file created: {name} (id={f.get('id')})")

def load_dataset(service):
    """Checks local filesystem, then checks Shared Drive folders."""
    # 1. Check local runner directories
    local_csvs = glob.glob("**/*.csv", recursive=True) + glob.glob("/tmp/**/*.csv", recursive=True)
    print("\n  --- Local CSV Files on Runner ---")
    if local_csvs:
        for lc in local_csvs:
            print(f"    • {lc} ({os.path.getsize(lc):,} bytes)")
        for lc in local_csvs:
            name_lower = os.path.basename(lc).lower()
            if any(k in name_lower for k in ["combined", "master", "classified"]):
                print(f"  --> Selected local file: {lc}")
                return pd.read_csv(lc)

    # 2. Check Shared Drive folders with full Team Drive support
    input_files = list_drive_folder(service, INPUT_FOLDER_ID)
    output_files = list_drive_folder(service, OUTPUT_FOLDER_ID)
    all_drive_files = {**input_files, **output_files}

    priority_keywords = ["combined_all_time", "master_classified", "combined", "master", "classified", "feedback"]
    for kw in priority_keywords:
        for fname, fid in all_drive_files.items():
            if kw in fname.lower() and fname.endswith(".csv"):
                print(f"\n  --> Selected Drive file: {fname} (id={fid})")
                buf = download_stream(service, fid)
                return pd.read_csv(buf)

    for fname, fid in all_drive_files.items():
        if fname.endswith(".csv"):
            print(f"\n  --> Selected Drive CSV: {fname} (id={fid})")
            buf = download_stream(service, fid)
            return pd.read_csv(buf)

    raise FileNotFoundError("Could not find any suitable CSV dataset locally or in Google Drive folders.")

def build_data_payload(df_raw):
    if "question" in df_raw.columns and "Response" in df_raw.columns:
        print("  Reshaping tall question format...")
        idx_cols = [c for c in ["courier_id", "Date", "market", "platform"] if c in df_raw.columns]
        piv = df_raw.pivot_table(
            index=idx_cols,
            columns="question",
            values="Response",
            aggfunc="first"
        ).reset_index()
        piv.columns.name = None
    else:
        piv = df_raw.copy()

    q1_col = next((c for c in ["Q1_Rating", "Q1", "rating", "csat_negative"] if c in piv.columns), None)
    q2_col = next((c for c in ["Q2_Resolved", "Q2", "resolved"] if c in piv.columns), None)
    q3_col = next((c for c in ["Q3_Feedback", "Q3", "q3_text", "comment", "feedback"] if c in piv.columns), None)
    date_col = next((c for c in ["Date", "date", "created_at"] if c in piv.columns), None)
    market_col = next((c for c in ["market", "mkt", "Market"] if c in piv.columns), None)
    platform_col = next((c for c in ["platform", "plt", "Platform"] if c in piv.columns), None)
    courier_col = next((c for c in ["courier_id", "courier", "Courier ID"] if c in piv.columns), None)

    dt_series = pd.to_datetime(piv[date_col]) if date_col else pd.Series(pd.Timestamp.now(), index=piv.index)
    piv["d"] = dt_series.dt.strftime("%Y-%m-%d %H:%M:%S")
    mondays = dt_series - pd.to_timedelta(dt_series.dt.weekday, unit="D")
    piv["w"] = mondays.dt.strftime("%Y-%m-%d")

    piv["m"] = piv[market_col].astype(str) if market_col else "UK"
    piv["p"] = piv[platform_col].astype(str) if platform_col else "iOS"
    piv["c"] = piv[courier_col].astype(str) if courier_col else "Unknown"

    def parse_q1(val):
        s = str(val).strip()
        if s in ("1", "2", "1-2", "1-2★") or s.startswith("1") or s.startswith("2"):
            return "1-2★", True
        return "3-5★", False

    q1_vals = piv[q1_col].apply(parse_q1) if q1_col else [("3-5★", False)] * len(piv)
    piv["q1"] = [v[0] for v in q1_vals]
    piv["is_neg"] = [v[1] for v in q1_vals]

    piv["q2"] = piv[q2_col].apply(lambda v: "Yes" if str(v).strip().lower() in ("1", "yes", "true") else "No") if q2_col else "No"

    has_q3 = (
        piv[q3_col].notna() &
        (piv[q3_col].astype(str).str.strip() != "") &
        (piv[q3_col].astype(str).str.lower() != "nan")
    ) if q3_col else pd.Series(False, index=piv.index)

    df_transcripts = piv[has_q3].copy()
    df_q1_only = piv[~has_q3].copy()

    print(f"  Processed Total Q1: {len(piv):,} | With Q3 text: {len(df_transcripts):,} | Q1-only: {len(df_q1_only):,}")

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
    by_week_dict = {w: compute_stats([t for t in transcripts_list if t["w"] == w]) for w in all_weeks}

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
    print(f"  Dashboard Engine Rebuild — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    service = get_drive_service()

    print("\n[1/3] Locating and loading dataset...")
    df_raw = load_dataset(service)

    print("\n[2/3] Computing metrics and building payload...")
    payload = build_data_payload(df_raw)

    with open(DATA_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"  Local {DATA_JSON_FILE} saved.")

    print(f"\n[3/3] Uploading updated {DATA_JSON_FILE} to Google Drive...")
    json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    upload_file(service, DATA_JSON_FILE, json_bytes, "application/json", OUTPUT_FOLDER_ID)

    print(f"\n{'='*60}")
    print("  SUCCESS: data.json built and synced!")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
