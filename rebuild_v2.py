#!/usr/bin/env python3
"""
rebuild_v2.py — Comprehensive CSAT Engine
- Properly pairs Q1, Q2, and Q3 using survey sessions (Visitor_ID / Courier ID)
- Accurately separates Q3 text transcripts (~16k) from Q1-only ratings (~33k+)
- Restores all calendar weeks from June 2026 through the latest submissions
- Integrates CA and UK Gold courier lists
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

def load_gold_courier_ids(service):
    """Loads Gold courier IDs from local CSV/ZIP files or Google Drive."""
    import zipfile, io
    gold_ids = set()
    local_patterns = ['**/*Gold*.csv', '**/*gold*.csv', '**/*Gold*.zip', '**/*gold*.zip']
    local_files = []
    for p in local_patterns:
        local_files.extend(glob.glob(p, recursive=True))
    for lf in set(local_files):
        try:
            if lf.endswith('.zip'):
                with zipfile.ZipFile(lf) as zf:
                    for member in zf.namelist():
                        if member.endswith('.csv'):
                            with zf.open(member) as zf_file:
                                gdf = pd.read_csv(zf_file, low_memory=False)
                                col = next((c for c in gdf.columns if 'courier' in c.lower()), None)
                                if col:
                                    gold_ids.update(gdf[col].dropna().astype(str).unique())
            else:
                gdf = pd.read_csv(lf, low_memory=False)
                col = next((c for c in gdf.columns if 'courier' in c.lower()), None)
                if col:
                    gold_ids.update(gdf[col].dropna().astype(str).unique())
        except Exception as e:
            print(f"  Warning reading gold file {lf}: {e}")
    print(f"  Loaded {len(gold_ids):,} Gold Courier IDs.")
    return gold_ids

def load_all_surveys(service):
    """Loads master Parquet and safely ingests only complete (42-file) weekly batches from Drive."""
    import re as regex
    drive_files = {**list_drive_folder(service, INPUT_FOLDER_ID), **list_drive_folder(service, OUTPUT_FOLDER_ID)}
    week_batches = {}
    pat = regex.compile(r'^([A-Z]{2})\s+(And|iOS)\s+([A-Za-z0-9\s\-]+?)\s+(Q[1-3])\.csv$', regex.I)
    for fname, fid in drive_files.items():
        m = pat.match(fname.strip())
        if m:
            mkt, plt, wk, q = m.groups()
            wk = wk.strip()
            week_batches.setdefault(wk, []).append({'name': fname, 'fid': fid, 'm': mkt.upper(), 'p': 'Android' if 'and' in plt.lower() else 'iOS', 'q': q.upper()})
    complete_weeks = {w: fl for w, fl in week_batches.items() if len(fl) == 42}
    for w, fl in week_batches.items():
        status = 'COMPLETE (42/42)' if len(fl) == 42 else f'PARTIAL ({len(fl)}/42) - Skipped'
        print(f'  [Integrity Gate] Week \'{w}\': {status}')
    drive_dfs, ingested_mondays = [], set()
    for w, flist in complete_weeks.items():
        for item in flist:
            try:
                df = pd.read_csv(download_stream(service, item['fid']), low_memory=False)
                df['market'] = df['region'] = item['m']
                df['platform'], df['question'] = item['p'], item['q']
                df['response_datetime'] = df['Date']
                df['response_english'] = df['response_original'] = df['Response']
                dt = pd.to_datetime(df['Date'], errors='coerce')
                mondays = (dt - pd.to_timedelta(dt.dt.weekday, unit='D')).dt.strftime('%Y-%m-%d')
                df['week_start'] = mondays
                ingested_mondays.update(mondays.dropna().unique())
                drive_dfs.append(df)
            except Exception as e:
                print(f"    Error downloading {item['name']}: {e}")
    dfs = []
    pq = os.path.join('data', 'base', 'courier_survey_all_data.parquet')
    if os.path.exists(pq):
        df_p = pd.read_parquet(pq)
        for o, n in {'region': 'market', 'response_datetime': 'Date', 'response_english': 'Response'}.items():
            if o in df_p.columns and n not in df_p.columns:
                df_p[n] = df_p[o]
        if 'week_start' in df_p.columns and ingested_mondays:
            ws = pd.to_datetime(df_p['week_start'], errors='coerce').dt.strftime('%Y-%m-%d')
            df_p = df_p[~ws.isin(ingested_mondays)]
        dfs.append(df_p)
    dfs.extend(drive_dfs)
    return pd.concat(dfs, ignore_index=True)

def process_and_assemble(df_raw, gold_ids):
    print("  Assembling survey sessions...")
    
    # Standardize column names
    col_map = {c: c.strip() for c in df_raw.columns}
    df_raw = df_raw.rename(columns=col_map)
    aliases = {'region': 'market', 'response_datetime': 'Date', 'response_english': 'Response', 'response_original': 'Response', 'Visitor ID': 'Visitor_ID'}
    for k, v in aliases.items():
        if k in df_raw.columns and v not in df_raw.columns:
            df_raw[v] = df_raw[k]
    if 'Date' not in df_raw.columns and 'week_start' in df_raw.columns:
        df_raw['Date'] = df_raw['week_start']

    # Tall format: pivot by session
    if "question" in df_raw.columns and "Response" in df_raw.columns:
        # Group by courier and survey session (Visitor_ID or date rounded to hour)
        df_raw["dt"] = pd.to_datetime(df_raw["Date"], errors="coerce")
        df_raw = df_raw.dropna(subset=["dt"])
        
        session_col = "Visitor_ID" if "Visitor_ID" in df_raw.columns else "courier_id"
        if "week_start" in df_raw.columns:
            df_raw["session_key"] = df_raw[session_col].astype(str) + "__" + df_raw["week_start"].astype(str)
        else:
            df_raw["session_key"] = df_raw[session_col].astype(str) + "__" + df_raw["dt"].dt.strftime("%Y-%m-%d")
        piv = df_raw.pivot_table(
            index=["session_key", "courier_id", "market", "platform"],
            columns="question",
            values="Response",
            aggfunc="first"
        ).reset_index()
        date_lookup = df_raw.groupby("session_key")["dt"].min().to_dict()
        piv["dt"] = piv["session_key"].map(date_lookup)
    else:
        piv = df_raw.copy()
        date_col = next((c for c in ["Date", "date", "created_at", "response_datetime", "week_start"] if c in piv.columns), "Date")
        piv["dt"] = pd.to_datetime(piv[date_col], errors="coerce")
        piv = piv.dropna(subset=["dt"])

    # Identify questions
    q1_col = next((c for c in ["Q1_Rating", "Q1", "rating"] if c in piv.columns), None)
    q2_col = next((c for c in ["Q2_Resolved", "Q2", "resolved"] if c in piv.columns), None)
    q3_col = next((c for c in ["Q3_Feedback", "Q3", "q3_text"] if c in piv.columns), None)

    piv["d"] = piv["dt"].dt.strftime("%Y-%m-%d %H:%M:%S")
    mondays = piv["dt"] - pd.to_timedelta(piv["dt"].dt.weekday, unit="D")
    piv["w"] = mondays.dt.strftime("%Y-%m-%d")

    piv["m"] = piv.get("market", "UK").astype(str)
    piv["p"] = piv.get("platform", "ios").astype(str)
    piv["c"] = piv.get("courier_id", "Unknown").astype(str)

    def parse_q1(val):
        s = str(val).strip()
        if s in ("1", "2", "1-2", "1-2★") or s.startswith("1") or s.startswith("2"):
            return "1-2★", True
        return "3-5★", False

    q1_vals = piv[q1_col].apply(parse_q1) if q1_col else [("3-5★", False)] * len(piv)
    piv["q1"] = [v[0] for v in q1_vals]
    piv["is_neg"] = [v[1] for v in q1_vals]

    piv["q2"] = piv[q2_col].apply(lambda v: "Yes" if str(v).strip().lower() in ("1", "yes", "true") else "No") if q2_col else "No"

    # Separate genuine Q3 transcripts from Q1-only ratings
    if q3_col:
        has_q3 = (
            piv[q3_col].notna() &
            (piv[q3_col].astype(str).str.strip() != "") &
            (~piv[q3_col].astype(str).str.lower().isin(["nan", "none", "null", "no comment"]))
        )
    else:
        has_q3 = pd.Series(False, index=piv.index)

    # Load classified transcript pillars lookup
    pillar_lookup = {}
    if os.path.exists('csat_all_transcripts_all.csv'):
        df_c = pd.read_csv('csat_all_transcripts_all.csv', low_memory=False)
        for _, cr in df_c.iterrows():
            cid = str(cr.get('Courier ID', '')).strip()
            txt = str(cr.get('Q3 Text', '')).strip()[:80].lower()
            p_label = str(cr.get('Pillar', '')).strip()
            if cid and p_label:
                pillar_lookup[(cid, txt)] = p_label
                pillar_lookup[cid] = p_label
        print(f' Loaded {len(pillar_lookup):,} pillar mappings from csat_all_transcripts_all.csv')

    df_transcripts = piv[has_q3].copy()
    df_q1_only = piv[~has_q3].copy()

    print(f"  Processed Total Q1: {len(piv):,} | With Q3 text: {len(df_transcripts):,} | Q1-only: {len(df_q1_only):,}")

    def build_transcript_record(r):
        cid = str(r.get('c', '')).strip()
        txt = str(r.get(q3_col, '')).strip()[:80].lower()
        pl = pillar_lookup.get((cid, txt)) or pillar_lookup.get(cid) or str(r.get('pillar', r.get('pl', 'General / Minimal'))).strip()
        if pl not in PILLAR_ORDER:
            pl = "General / Minimal"
        return {
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
        }

    transcripts_all = [build_transcript_record(r) for _, r in df_transcripts.iterrows()]
    transcripts_gold = [t for t in transcripts_all if t["c"] in gold_ids]

    q1_only_all = [
        {"d": r["d"], "m": r["m"], "p": r["p"], "q": r["q1"], "c": r["c"], "w": r["w"], "n": bool(r["is_neg"])}
        for _, r in df_q1_only.iterrows()
    ]
    q1_only_gold = [q for q in q1_only_all if q["c"] in gold_ids]

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

        m_tot, m_neg, m_pos = {}, {}, {}
        for r in rows:
            m = r["m"]
            m_tot[m] = m_tot.get(m, 0) + 1
            if r["q1"] == "1-2★":
                m_neg[m] = m_neg.get(m, 0) + 1
            else:
                m_pos[m] = m_pos.get(m, 0) + 1

        t_neg = sum(m_neg.values()) or 1
        t_pos = sum(m_pos.values()) or 1

        m_neg_dict = {
            m: {"neg": m_neg.get(m, 0), "pos": m_pos.get(m, 0), "total": m_tot[m], "pct": round(m_neg.get(m, 0) / t_neg * 100, 1)}
            for m in m_tot
        }
        m_pos_dict = {
            m: {"neg": m_neg.get(m, 0), "pos": m_pos.get(m, 0), "total": m_tot[m], "pct": round(m_pos.get(m, 0) / t_pos * 100, 1)}
            for m in m_tot
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

    all_weeks = sorted(list(piv["w"].unique()))
    print(f"  Detected {len(all_weeks)} calendar weeks from {all_weeks[0]} to {all_weeks[-1]}")

    by_week_all = {w: compute_stats([t for t in transcripts_all if t["w"] == w]) for w in all_weeks}
    by_week_gold = {w: compute_stats([t for t in transcripts_gold if t["w"] == w]) for w in all_weeks}

    all_dates = piv["d"].str[:10].dropna()

    return {
        "weeks": all_weeks,
        "pillarOrder": PILLAR_ORDER,
        "pillarColors": PILLAR_COLORS,
        "dateRange": {"start": all_dates.min(), "end": all_dates.max()},
        "all": {
            "allTime": compute_stats(transcripts_all),
            "byWeek": by_week_all,
            "transcripts": transcripts_all
        },
        "gold": {
            "allTime": compute_stats(transcripts_gold),
            "byWeek": by_week_gold,
            "transcripts": transcripts_gold
        },
        "q1NoQ3": {
            "all": q1_only_all,
            "gold": q1_only_gold
        }
    }

def main():
    print(f"\n{'='*60}")
    print(f"  Rebuilding Master CSAT Intelligence — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    service = get_drive_service()
    gold_ids = load_gold_courier_ids(service)

    print("\n[1/3] Loading survey datasets...")
    df_raw = load_all_surveys(service)

    print("\n[2/3] Processing survey sessions & computing historical metrics...")
    payload = process_and_assemble(df_raw, gold_ids)

    # Save locally in repository root
    with open(DATA_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"  Saved {DATA_JSON_FILE} locally ({os.path.getsize(DATA_JSON_FILE):,} bytes).")

    # Upload to Google Drive Output Folder
    print(f"\n[3/3] Uploading updated {DATA_JSON_FILE} to Google Drive...")
    json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    upload_file(service, DATA_JSON_FILE, json_bytes, "application/json", OUTPUT_FOLDER_ID)

    print(f"\n{'='*60}")
    print("  SUCCESS: Master data.json updated with all historical weeks!")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
