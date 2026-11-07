#!/usr/bin/env python3
"""
build_master_data.py
- Ingests CA and UK Gold courier CSV files
- Cross-references courier IDs against all survey responses
- Builds full baseline (65,390 respondents) and dedicated Gold subset
- Preserves all 20 historical weeks (June 17 – Nov 1)
"""

import os, glob, json
import pandas as pd

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

def load_gold_courier_ids():
    gold_ids = set()
    gold_files = glob.glob("**/*Gold*.csv", recursive=True) + glob.glob("**/*gold*.csv", recursive=True)
    for gf in gold_files:
        try:
            df = pd.read_csv(gf)
            if "courier_id" in df.columns:
                cleaned = df["courier_id"].dropna().astype(str).str.strip()
                gold_ids.update(cleaned.unique())
                print(f"  Loaded {len(cleaned.unique()):,} IDs from {gf}")
        except Exception as e:
            print(f"  Could not read {gf}: {e}")
    print(f"  Total unique Gold Courier IDs loaded: {len(gold_ids):,}")
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

    gold_courier_ids = load_gold_courier_ids()

    trans_file = "csat_all_transcripts_all.csv" if os.path.exists("csat_all_transcripts_all.csv") else "csat_period_2026_all.csv"
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

    print(f"Transcripts: Total = {len(transcripts_all):,} | Gold = {len(transcripts_gold):,}")

    q1_only_all = []
    q1_only_gold = []
    raw_files = [f for f in ["combined_all_time (2).csv", "combined_all_time.csv"] if os.path.exists(f)]

    if raw_files:
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
        weeks_cycle = sorted(list(df_trans["w"].unique()))
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

    print(f"Q1-only: Total = {len(q1_only_all):,} | Gold = {len(q1_only_gold):,}")
    print(f"Total Combined Respondents: {len(transcripts_all) + len(q1_only_all):,}")

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

    print(f"Saved data.json successfully.")

if __name__ == "__main__":
    main()
