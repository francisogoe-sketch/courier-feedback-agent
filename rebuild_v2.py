#!/usr/bin/env python3
"""
rebuild_v2.py — Cumulative Data Ingestion Engine
1. Loads existing data.json (preserving all 20 historical weeks)
2. Scans Google Drive and local workspace for new week CSV exports
3. Ingests new records (both Q1+Q3 transcripts and Q1-only responses)
4. Recalculates metrics and saves updated data.json
"""

import os, io, json, sys, glob
import pandas as pd
from datetime import datetime, timezone

try:
    import google.auth
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
except ImportError:
    sys.exit("Missing google libraries. Run: pip install google-api-python-client google-auth")

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

def get_drive_service():
    creds, _ = google.auth.default(scopes=SCOPES)
    print("  Authenticated via Application Default Credentials (ADC)")
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def list_drive_folder(service, folder_id):
    q = f"'{folder_id}' in parents and trashed = false"
    res = service.files().list(
        q=q,
        fields="files(id, name, mimeType)",
        pageSize=100,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives"
    ).execute()
    return {f['name']: f['id'] for f in res.get("files", [])}

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

    marketNeg = {}
    marketPos = {}
    marketTotal = {}
    for r in rows:
        m = r.get("m", "Unknown")
        marketTotal[m] = marketTotal.get(m, 0) + 1
        if r["q1"] == "1-2★":
            marketNeg[m] = marketNeg.get(m, 0) + 1
        else:
            marketPos[m] = marketPos.get(m, 0) + 1

    tot_neg = sum(marketNeg.values()) or 1
    tot_pos = sum(marketPos.values()) or 1

    m_neg_dict = {
        m: {"neg": marketNeg.get(m, 0), "pos": marketPos.get(m, 0), "total": marketTotal[m], "pct": round(marketNeg.get(m, 0) / tot_neg * 100, 1)}
        for m in marketTotal
    }
    m_pos_dict = {
        m: {"neg": marketNeg.get(m, 0), "pos": marketPos.get(m, 0), "total": marketTotal[m], "pct": round(marketPos.get(m, 0) / tot_pos * 100, 1)}
        for m in marketTotal
    }

    return {
        "total": tot,
        "negQ1": neg,
        "posQ1": pos,
        "resCount": res,
        "negQ1Pct": round(neg / tot * 100, 1),
        "posQ1Pct": round(pos / tot * 100, 1),
        "resPct": round(res / tot * 100, 1),
        "marketNeg": m_neg_dict,
        "marketPos": m_pos_dict,
        "pillars": pillars
    }

def main():
    print(f"\n{'='*60}")
    print(f"  Cumulative CSAT Ingestion — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    # 1. Load existing historical data.json
    if not os.path.exists(DATA_JSON_FILE):
        sys.exit(f"ERROR: {DATA_JSON_FILE} not found. Ensure baseline data.json is in repo root.")

    with open(DATA_JSON_FILE, "r", encoding="utf-8") as f:
        master_data = json.load(f)

    existing_transcripts = master_data["all"]["transcripts"]
    existing_q1_only = master_data["q1NoQ3"]["all"]
    existing_weeks = set(master_data["weeks"])
    existing_transcript_keys = {(t["c"], t["d"]) for t in existing_transcripts}
    existing_q1_keys = {(t["c"], t["d"]) for t in existing_q1_only}

    print(f"  Existing Baseline Loaded:")
    print(f"    • Weeks: {len(existing_weeks)}")
    print(f"    • Q1+Q3 Transcripts: {len(existing_transcripts):,}")
    print(f"    • Q1-Only Records:   {len(existing_q1_only):,}")

    service = get_drive_service()

    # 2. Look for new CSV files to ingest
    new_csv_files = []
    local_csvs = glob.glob("**/*.csv", recursive=True) + glob.glob("/tmp/**/*.csv", recursive=True)
    for lc in local_csvs:
        if any(k in os.path.basename(lc).lower() for k in ["combined", "raw", "new", "batch", "sep", "oct", "nov"]):
            new_csv_files.append(("local", lc))

    input_files = list_drive_folder(service, INPUT_FOLDER_ID)
    for name, fid in input_files.items():
        if name.endswith(".csv"):
            new_csv_files.append(("drive", (name, fid)))

    print(f"\n  Found {len(new_csv_files)} potential source files to inspect for new weeks.")

    added_transcripts = 0
    added_q1_only = 0

    for source_type, file_ref in new_csv_files:
        try:
            if source_type == "local":
                df_new = pd.read_csv(file_ref)
            else:
                name, fid = file_ref
                buf = download_stream(service, fid)
                df_new = pd.read_csv(buf)

            if "question" in df_new.columns and "Response" in df_new.columns:
                piv = df_new.pivot_table(
                    index=[c for c in ["courier_id", "Date", "market", "platform"] if c in df_new.columns],
                    columns="question",
                    values="Response",
                    aggfunc="first"
                ).reset_index()
                piv.columns.name = None
            else:
                piv = df_new.copy()

            q1_col = next((c for c in ["Q1_Rating", "Q1", "rating"] if c in piv.columns), None)
            q2_col = next((c for c in ["Q2_Resolved", "Q2", "resolved"] if c in piv.columns), None)
            q3_col = next((c for c in ["Q3_Feedback", "Q3", "q3_text"] if c in piv.columns), None)
            date_col = next((c for c in ["Date", "date", "created_at"] if c in piv.columns), None)
            market_col = next((c for c in ["market", "mkt"] if c in piv.columns), None)
            platform_col = next((c for c in ["platform", "plt"] if c in piv.columns), None)
            courier_col = next((c for c in ["courier_id", "courier"] if c in piv.columns), None)

            if not q1_col or not date_col:
                continue

            dt_series = pd.to_datetime(piv[date_col])
            piv["d"] = dt_series.dt.strftime("%Y-%m-%d %H:%M:%S")
            mondays = dt_series - pd.to_timedelta(dt_series.dt.weekday, unit="D")
            piv["w"] = mondays.dt.strftime("%Y-%m-%d")

            for _, r in piv.iterrows():
                cid = str(r[courier_col]) if courier_col else "Unknown"
                d_str = r["d"]
                m_str = str(r[market_col]) if market_col else "UK"
                p_str = str(r[platform_col]) if platform_col else "iOS"
                w_str = r["w"]

                raw_q1 = str(r[q1_col]).strip()
                q1_fmt = "1-2★" if raw_q1 in ("1", "2", "1-2", "1-2★") or raw_q1.startswith("1") or raw_q1.startswith("2") else "3-5★"
                is_neg = (q1_fmt == "1-2★")
                q2_fmt = "Yes" if q2_col and str(r.get(q2_col, "")).strip().lower() in ("1", "yes", "true") else "No"

                raw_q3 = str(r.get(q3_col, "")).strip() if q3_col else ""
                has_text = bool(raw_q3 and raw_q3.lower() not in ("nan", "none", ""))

                if has_text:
                    if (cid, d_str) not in existing_transcript_keys:
                        existing_transcripts.append({
                            "d": d_str, "m": m_str, "p": p_str,
                            "q1": q1_fmt, "q2": q2_fmt, "c": cid,
                            "t": raw_q3[:300],
                            "pl": "General / Minimal",
                            "w": w_str,
                            "pl12": "Others"
                        })
                        existing_transcript_keys.add((cid, d_str))
                        existing_weeks.add(w_str)
                        added_transcripts += 1
                else:
                    if (cid, d_str) not in existing_q1_keys:
                        existing_q1_only.append({
                            "d": d_str, "m": m_str, "p": p_str,
                            "q": q1_fmt, "c": cid, "w": w_str,
                            "n": is_neg
                        })
                        existing_q1_keys.add((cid, d_str))
                        existing_weeks.add(w_str)
                        added_q1_only += 1
        except Exception as e:
            continue

    print(f"\n  Cumulative Ingestion Result:")
    print(f"    • Added {added_transcripts:,} new Q3 transcripts")
    print(f"    • Added {added_q1_only:,} new Q1-only records")

    # 3. Recalculate full byWeek and allTime metrics
    sorted_weeks = sorted(list(existing_weeks))
    by_week_dict = {w: compute_stats([t for t in existing_transcripts if t["w"] == w]) for w in sorted_weeks}

    all_dates = [t["d"][:10] for t in existing_transcripts] + [t["d"][:10] for t in existing_q1_only]

    master_data["weeks"] = sorted_weeks
    master_data["dateRange"] = {"start": min(all_dates), "end": max(all_dates)}
    master_data["all"]["transcripts"] = existing_transcripts
    master_data["all"]["allTime"] = compute_stats(existing_transcripts)
    master_data["all"]["byWeek"] = by_week_dict
    master_data["q1NoQ3"]["all"] = existing_q1_only

    # 4. Save and sync back
    with open(DATA_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(master_data, f, separators=(",", ":"))
    print(f"  Saved cumulative {DATA_JSON_FILE} locally.")

    json_bytes = json.dumps(master_data, separators=(",", ":")).encode("utf-8")
    upload_file(service, DATA_JSON_FILE, json_bytes, "application/json", OUTPUT_FOLDER_ID)

    print(f"\n{'='*60}")
    print("  SUCCESS: data.json updated cumulatively!")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
