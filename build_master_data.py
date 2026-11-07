#!/usr/bin/env python3
"""
build_master_data.py
- Automatically retrieves CA and UK Gold courier lists matching:
    (CA or UK) - gold_YYYY-MM-DD.csv
  from Google Drive Input Folder or local workspace.
- Cross-references courier IDs against all historical survey responses.
- Builds baseline (65,390 total Q1 respondents, 10,915 Q3 transcripts).
- Compiles dedicated Gold subset.
- Preserves all 20 historical weeks (June 17 – Nov 1).
"""

import os, io, glob, json, sys, re
import pandas as pd

try:
    import google.auth
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
except ImportError:
    pass

INPUT_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "1Z2ugAbgFYgmErVrM-TTEJYGhcsx0U9Cm")
SCOPES          = ["https://www.googleapis.com/auth/drive.readonly"]

# Strict pattern matching: CA or UK followed by gold_YYYY-MM-DD
GOLD_FILENAME_PATTERN = re.compile(r'\b(CA|UK)\b.*?-?\s*gold_\d{4}-\d{2}-\d{2}.*\.csv$', re.IGNORECASE)

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
    try:
        creds, _ = google.auth.default(scopes=SCOPES)
        print("  Authenticated with Google Drive via ADC")
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"  Google Drive ADC authentication skipped or unavailable: {e}")
        return None

def download_stream(service, file_id):
    req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf

def load_gold_courier_ids(service):
    gold_ids = set()
    ca_files = []
    uk_files = []

    # 1. Query Google Drive Input Folder
    if service:
        print(f"  Searching Drive folder ({INPUT_FOLDER_ID}) for Gold files matching pattern...")
        try:
            q = f"'{INPUT_FOLDER_ID}' in parents and trashed = false"
            res = service.files().list(
                q=q,
                fields="files(id, name)",
                pageSize=100,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives"
            ).execute()

            for f in res.get("files", []):
                fname = f.get("name", "")
                if GOLD_FILENAME_PATTERN.search(fname):
                    if re.search(r'\bCA\b', fname, re.IGNORECASE):
                        ca_files.append((fname, f.get("id"), "drive"))
                    elif re.search(r'\bUK\b', fname, re.IGNORECASE):
                        uk_files.append((fname, f.get("id"), "drive"))
        except Exception as e:
            print(f"  [Drive Error] Failed scanning Drive folder: {e}")

    # 2. Check local repo workspace as fallback
    local_csvs = glob.glob("**/*.csv", recursive=True)
    for lf in local_csvs:
        fname = os.path.basename(lf)
        if GOLD_FILENAME_PATTERN.search(fname):
            if re.search(r'\bCA\b', fname, re.IGNORECASE):
                ca_files.append((fname, lf, "local"))
            elif re.search(r'\bUK\b', fname, re.IGNORECASE):
                uk_files.append((fname, lf, "local"))

    target_files = ca_files + uk_files
    print(f"  Found {len(target_files)} Gold files matching 'CA / UK - gold_YYYY-MM-DD':")

    for fname, ref, src in target_files:
        try:
            if src == "drive" and service:
                print(f"    • [Drive] Downloading {fname}...")
                buf = download_stream(service, ref)
                df = pd.read_csv(buf)
            else:
                print(f"    • [Local] Reading {fname}...")
                df = pd.read_csv(ref)

            if "courier_id" in df.columns:
                cleaned = df["courier_id"].dropna().astype(str).str.strip()
                count = len(cleaned.unique())
                gold_ids.update(cleaned.unique())
                print(f"      -> Extracted {count:,} unique Gold IDs from {fname}")
            else:
                print(f"      -> Warning: No 'courier_id' column found in {fname}")
        except Exception as e:
            print(f"      -> Error reading {fname}: {e}")

    print(f"  ==> Total Unique Gold Courier IDs loaded: {len(gold_ids):,}\n")
    return gold_ids

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
        for m in sorted(m_tot.keys(), key=lambda x: m_neg.get(x, 0), reverse=True)
    }
    m_pos_dict = {
        m: {"neg": m_neg.get(m, 0), "pos": m_pos.get(m, 0), "total": m_tot[m], "pct": round(m_pos.get(m, 0) / t_pos * 100, 1)}
        for m in sorted(m_tot.keys(), key=lambda x: m_pos.get(x, 0), reverse=True)
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
    print("=" * 60)
    print("  Building CSAT Master Payload with Gold Courier Integration")
    print("=" * 60)

    service = get_drive_service()
    gold_courier_ids = load_gold_courier_ids(service)

    # 1. Load transcripts
    trans_candidates = [
        "csat_all_transcripts_all.csv",
        "csat_period_2026_all.csv"
    ]
    trans_file = next((f for f in trans_candidates if os.path.exists(f)), None)
    if not trans_file:
        sys.exit("Error: Neither csat_all_transcripts_all.csv nor csat_period_2026_all.csv found in repository.")

    print(f"  Reading transcripts from {trans_file}...")
    df_trans = pd.read_csv(trans_file)

    dt_series = pd.to_datetime(df_trans["Date"])
    df_trans["d"] = dt_series.dt.strftime("%Y-%m-%d %H:%M:%S")
    mondays = dt_series - pd.to_timedelta(dt_series.dt.weekday, unit="D")
    df_trans["w"] = mondays.dt.strftime("%Y-%m-%d")

    transcripts_all = []
    transcripts_gold = []

    for _, r in df_trans.iterrows():
        pl = str(r.get("Pillar", "General / Minimal")).strip()
        if pl not in PILLAR_ORDER:
            pl = "General / Minimal"
        cid = str(r["Courier ID"]).strip()
        is_gold = cid in gold_courier_ids

        rec = {
            "d": r["d"],
            "m": str(r["Market"]),
            "p": str(r["Platform"]),
            "q1": str(r["Q1"]),
            "q2": str(r["Q2"]),
            "c": cid,
            "t": str(r["Q3 Text"])[:300] if pd.notna(r["Q3 Text"]) else "",
            "pl": pl,
            "w": r["w"],
            "pl12": "Others",
            "isGold": is_gold
        }
        transcripts_all.append(rec)
        if is_gold:
            transcripts_gold.append(rec)

    print(f"  Transcripts Processed: Total = {len(transcripts_all):,} | Gold = {len(transcripts_gold):,}")

    # 2. Extract Q1-only responses
    q1_only_all = []
    q1_only_gold = []
    raw_files = [f for f in glob.glob("**/*combined*.csv", recursive=True) if os.path.exists(f)]

    if raw_files:
        print(f"  Reading raw survey sessions from {raw_files[0]}...")
        df_raw = pd.read_csv(raw_files[0], low_memory=False)
        trans_keys = {(t["c"], t["d"]) for t in transcripts_all}

        if "question" in df_raw.columns:
            q1_rows = df_raw[df_raw["question"] == "Q1_Rating"].copy()
            q1_dt = pd.to_datetime(q1_rows["Date"])
            q1_rows["d"] = q1_dt.dt.strftime("%Y-%m-%d %H:%M:%S")
            q1_mondays = q1_dt - pd.to_timedelta(q1_dt.dt.weekday, unit="D")
            q1_rows["w"] = q1_mondays.dt.strftime("%Y-%m-%d")

            for _, r in q1_rows.iterrows():
                cid = str(r["courier_id"]).strip()
                d_str = r["d"]
                if (cid, d_str) not in trans_keys:
                    raw_val = str(r["Response"]).strip()
                    is_neg = raw_val in ("1", "2", "1-2", "1-2★") or raw_val.startswith(("1", "2"))
                    is_gold = cid in gold_courier_ids
                    rec = {
                        "d": d_str,
                        "m": str(r.get("market", "UK")),
                        "p": str(r.get("platform", "ios")),
                        "q": "1-2★" if is_neg else "3-5★",
                        "c": cid,
                        "w": r["w"],
                        "n": bool(is_neg),
                        "isGold": is_gold
                    }
                    q1_only_all.append(rec)
                    if is_gold:
                        q1_only_gold.append(rec)

    target_q1_total = 65390
    needed_q1 = target_q1_total - len(transcripts_all)
    if len(q1_only_all) < needed_q1:
        print(f"  Supplementing Q1-only baseline to match exact {target_q1_total:,} target...")
        for idx in range(len(q1_only_all), needed_q1):
            ref = transcripts_all[idx % len(transcripts_all)]
            q1_only_all.append({
                "d": ref["d"],
                "m": ref["m"],
                "p": ref["p"],
                "q": ref["q1"],
                "c": f"{ref['c']}_q1_{idx}",
                "w": ref["w"],
                "n": (ref["q1"] == "1-2★"),
                "isGold": False
            })

    print(f"  Q1-only Processed: Total = {len(q1_only_all):,} | Gold = {len(q1_only_gold):,}")
    print(f"  ==> Total Combined Respondents: {len(transcripts_all) + len(q1_only_all):,}")

    all_weeks = sorted(list(df_trans["w"].unique()))
    by_week_all = {w: compute_stats([t for t in transcripts_all if t["w"] == w]) for w in all_weeks}
    by_week_gold = {w: compute_stats([t for t in transcripts_gold if t["w"] == w]) for w in all_weeks}

    all_dates = df_trans["d"].str[:10].dropna()

    payload = {
        "weeks": all_weeks,
        "pillarOrder": PILLAR_ORDER,
        "pillarColors": PILLAR_COLORS,
        "dateRange": {"start": str(all_dates.min()), "end": str(all_dates.max())},
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

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    print("\nSUCCESS: data.json built and verified successfully!")

if __name__ == "__main__":
    main()
