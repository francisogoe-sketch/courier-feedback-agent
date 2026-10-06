#!/usr/bin/env python3
"""rebuild_dashboard.py - Step 9. Auth: ADC. ALL FIXES INCLUDED.
Fixes: ADC auth, Q1 star format (1-2★), P1-4 inference, script escaping,
       lambda re.sub, shared drive, dynamic header, Q1 summary reading.
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
Q1_SUMMARY_NAME  = "q1_summary.json"
TEMPLATE_HTML    = "dashboard_template.html"
OUTPUT_HTML_NAME = "csat_dashboard_latest.html"
SCOPES           = ["https://www.googleapis.com/auth/drive"]
PILLAR_1_4       = {"Pillar 1","Pillar 2","Pillar 3","Pillar 4"}
PILLAR_NAME_FIX  = {
    # Human names (rows where pillar_name is already resolved)
    "Support Quality":           "Support Quality",
    "App / Tech Issues":         "App / Tech Issues",
    "Partner & External Delays": "Partner and External Delays",
    "Compensation":              "Compensation",
    # Pillar numbers → human names (rows where pillar_name was not resolved)
    "Pillar 1": "Support Quality",
    "Pillar 2": "App / Tech Issues",
    "Pillar 3": "Partner and External Delays",
    "Pillar 4": "Compensation",
}
MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

def get_service():
    """ADC auth - NO service account needed."""
    creds, _ = google.auth.default(scopes=SCOPES)
    return build("drive","v3",credentials=creds,cache_discovery=False)

def find_file(svc, name, folder_id):
    q = f'name="{name}" and "{folder_id}" in parents and trashed=false'
    res = svc.files().list(
        q=q, fields="files(id,name,modifiedTime)", orderBy="modifiedTime desc",
        pageSize=5, supportsAllDrives=True,
        includeItemsFromAllDrives=True, corpora="allDrives"
    ).execute()
    files = res.get("files",[])
    return files[0]["id"] if files else None

def dl_bytes(svc, fid):
    req = svc.files().get_media(fileId=fid)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done: _, done = dl.next_chunk()
    buf.seek(0); return buf

def upload(svc, name, data_bytes, folder_id, mime, existing_id=None):
    media = MediaIoBaseUpload(io.BytesIO(data_bytes), mimetype=mime, resumable=True)
    if existing_id:
        svc.files().update(fileId=existing_id, media_body=media,
                           supportsAllDrives=True).execute()
        print(f"  Updated: {name}")
    else:
        meta = {"name":name,"mimeType":mime,"parents":[folder_id]}
        svc.files().create(body=meta, media_body=media, fields="id",
                           supportsAllDrives=True).execute()
        print(f"  Created: {name}")

def build_transcripts(df):
    p14 = df[df["pillar"].isin(PILLAR_1_4)].copy()
    use = p14[p14["in_X"]==1].copy()
    if len(use)==0:
        print("  WARNING: in_X==1 empty, using all P1-4")
        use = p14.copy()

    use["dash_pillar"] = use["pillar_name"].map(PILLAR_NAME_FIX).fillna(use["pillar_name"])

    # Q1 WITH STAR (★) - required by template JS indexOf checks
    def _q1(r):
        if r["csat_negative"] == 1: return "1-2\u2605"   # actual negative rating
        if r["csat_positive"] == 1: return "3-5\u2605"   # actual positive rating
        if r["pillar"] in PILLAR_1_4: return "1-2\u2605" # P1-4 unjoined = inferred negative
        return "3-5\u2605"
    use["Q1"] = use.apply(_q1, axis=1)
    use["Q2"] = use["resolved"].apply(lambda r: "Yes" if r==1 else "No")
    use["Date_str"] = pd.to_datetime(use["Date"]).dt.strftime("%Y-%m-%d")
    rows = []
    for _, r in use.sort_values("Date_str").iterrows():
        rows.append([str(r["dash_pillar"]),str(r.get("theme","")),
                     str(r["Date_str"]),str(r["market"]),str(r["platform"]),
                     str(r["Q1"]),str(r["Q2"]),
                     str(r["q3_text"]) if pd.notna(r["q3_text"]) else ""])
    header = '["Pillar","Cluster","Date","Market","Platform","Q1","Q2","Q3 Text"]'
    data   = ",\n".join("["+",".join(json.dumps(v,ensure_ascii=True) for v in row)+"]" for row in rows)
    print(f"  TRANSCRIPTS rows: {len(rows):,}")
    js = f"var TRANSCRIPTS=[\n{header},\n{data}\n];"
    # Escape </script> to prevent premature tag closure
    js = js.replace("</script>","<\\/script>").replace("<!--","<\\!--")
    return js, len(rows)

def inject(template, transcripts_js):
    pat = r'var TRANSCRIPTS=\[.*?\];'
    if not re.search(pat, template, flags=re.DOTALL):
        sys.exit("ERROR: TRANSCRIPTS placeholder not found in template")
    # Use lambda to prevent re.sub interpreting \u as escape
    return re.sub(pat, lambda m: transcripts_js, template, flags=re.DOTALL)

def update_kpis(svc, html, df, n_rows):
    total_q3 = len(df)
    # Try standalone Q1 summary from Drive for accurate header
    q1_data = None
    try:
        qid = find_file(svc, Q1_SUMMARY_NAME, DRIVE_FOLDER_ID)
        if qid:
            buf = dl_bytes(svc, qid)
            q1_data = json.load(buf)
            print(f"  Q1 summary: {q1_data.get('q1_valid',0):,} Q1 valid")
    except Exception as e:
        print(f"  Q1 summary unavailable: {e}")

    if q1_data:
        valid   = int(q1_data.get("q1_valid",0))
        pos_pct = float(q1_data.get("pos_pct",0))
        neg_pct = float(q1_data.get("neg_pct",0))
    else:
        valid   = int(df["csat_valid"].sum())
        pos     = int(df["csat_positive"].sum())
        neg     = int(df["csat_negative"].sum())
        pos_pct = round(pos/valid*100,1) if valid else 0
        neg_pct = round(neg/valid*100,1) if valid else 0

    dates   = pd.to_datetime(df["submitted_at"],errors="coerce").dropna()
    min_d,max_d = dates.min(), dates.max()
    new_range = f"{MONTHS[min_d.month-1]} {min_d.day} \u2013 {MONTHS[max_d.month-1]} {max_d.day}, {max_d.year}"
    now = datetime.now(timezone.utc)
    new_week = f"W{now.isocalendar()[1]:02d}_{now.year}"

    print(f"  Header: {new_range} | Q1:{valid:,} | Pos:{pos_pct}% | Neg:{neg_pct}% | Q3:{n_rows:,}")

    html = re.sub(r'W\d{2}_\d{4}', new_week, html, count=5)
    for pat in [r'Jun\s+17\s+[\u2013\-]\s+Sep\s+27,?\s*2026',
                'Jun 17 \u2013 Sep 27, 2026','Jun 17 - Sep 27, 2026']:
        try: html = re.sub(pat, new_range, html, count=5)
        except Exception: pass
    html = html.replace('18,670 Q3 transcripts', f'{total_q3:,} Q3 transcripts')
    html = html.replace('7,364', f'{n_rows:,}').replace('7364', str(n_rows))
    html = html.replace('103,639', f'{valid:,}').replace('103639', str(valid))
    html = html.replace('49.5%', f'{pos_pct}%', 5)
    html = html.replace('50.5%', f'{neg_pct}%', 5)
    return html



import html as _html_lib
PILLAR_1_4_SET = {"Pillar 1","Pillar 2","Pillar 3","Pillar 4"}
PILLAR_NAME_FIX_FULL = {
    "Support Quality":"Support Quality",
    "App / Tech Issues":"App / Tech Issues",
    "Partner & External Delays":"Partner and External Delays",
    "Compensation":"Compensation",
    "Pillar 1":"Support Quality",
    "Pillar 2":"App / Tech Issues",
    "Pillar 3":"Partner and External Delays",
    "Pillar 4":"Compensation",
}
PILLAR_TO_CLUSTER = {
    "Support Quality":"Incorrect Advice Given",
    "App / Tech Issues":"System / Order Errors",
    "Partner and External Delays":"Traffic / External Delays",
    "Compensation":"Refund / Compensation",
}
MKT_COLORS_CL = {
    "UK":"#1565C0","CA":"#E65100","IE":"#2E7D32",
    "AT":"#6D28D9","DE":"#0369A1","SK":"#065F46","IL":"#92400E"
}

def _q1_for_row(row):
    if row["csat_negative"]==1: return "1-2★"
    if row["csat_positive"]==1: return "3-5★"
    if row["pillar"] in PILLAR_1_4_SET: return "1-2★"
    return "3-5★"

def build_cluster_rows_html(pillar_df):
    import pandas as _pd
    rows_html = []
    for _, r in pillar_df.sort_values("Date_str").iterrows():
        date  = str(r["Date_str"])
        mkt   = str(r["market"])
        plt   = str(r.get("platform",""))
        q1    = _q1_for_row(r)
        q1cls = "qbn" if "1-2" in q1 else "qbp"
        q1esc = q1.replace("★","&#9733;")
        q2    = "Yes" if r["resolved"]==1 else "No"
        q2col = "#DC2626" if q2=="No" else "#16A34A"
        q3raw = str(r["q3_text"]) if (_pd.notna(r.get("q3_text")) and str(r.get("q3_text")) not in ("nan","")) else ""
        q3e   = _html_lib.escape(q3raw, quote=True)
        short = _html_lib.escape(q3raw[:80]+("..." if len(q3raw)>80 else ""), quote=True)
        mktcol= MKT_COLORS_CL.get(mkt,"#333333")
        tcm = ('<tr class="tcm"><td class="tc-date">'+date+'</td>'
               +'<td class="tc-mkt" style="color:'+mktcol+';font-weight:700">'+mkt+'</td>'
               +'<td class="tc-plt">'+plt+'</td>'
               +'<td><span class="qb '+q1cls+'">'+q1esc+'</span></td>'
               +'<td style="color:'+q2col+';font-weight:700;font-size:.69rem">'+q2+'</td>'
               +'<td class="tcshort">'+short+'</td></tr>')
        tcx = ('<tr class="tcx"><td colspan="6"><strong>'+date+' | '+mkt+' | '+plt+' | Q1: '+q1esc+' | Q2: '+q2+'</strong><br>'+q3e+'</td></tr>')
        rows_html.append(tcm+tcx)
    return "".join(rows_html)

def inject_cluster_html(html_str, df):
    import pandas as _pd
    p14 = df[df["pillar"].isin(PILLAR_1_4_SET)].copy()
    use = p14[p14["in_X"]==1].copy()
    if len(use)==0: use=p14.copy()
    use["dash_pillar"]=use["pillar_name"].map(PILLAR_NAME_FIX_FULL).fillna(use["pillar_name"])
    use["Date_str"]=_pd.to_datetime(use["Date"],errors="coerce").dt.strftime("%Y-%m-%d")
    for pillar,cluster in PILLAR_TO_CLUSTER.items():
        prows=use[use["dash_pillar"]==pillar]
        if len(prows)==0: print("  SKIP: no rows for",pillar); continue
        new_rows=build_cluster_rows_html(prows)
        n=len(prows)
        neg=int((prows["csat_negative"]==1).sum())
        neg_pct=round(neg/n*100,1) if n else 0.0
        sname_marker='<div class="sname">'+cluster+'</div>'
        sname_idx=html_str.find(sname_marker)
        if sname_idx<0: print("  WARNING: cluster not found:",cluster); continue
        region=html_str[sname_idx:sname_idx+600]
        region=re.sub(r"\d[\d,]* transcripts","{:,} transcripts".format(n),region,count=1)
        region=re.sub(r"[\d.]+% neg","{}% neg".format(neg_pct),region,count=1)
        region=re.sub(r"All [\d,]+ Q3 transcripts","All {:,} Q3 transcripts".format(n),region,count=1)
        html_str=html_str[:sname_idx]+region+html_str[sname_idx+600:]
        sname_idx2=html_str.find(sname_marker)
        tb_open=html_str.find("<tbody>",sname_idx2)
        if tb_open<0: print("  WARNING: no tbody for",cluster); continue
        tb_start=tb_open+len("<tbody>")
        tb_end=html_str.find("</tbody>",tb_start)
        html_str=html_str[:tb_start]+new_rows+html_str[tb_end:]
        print("  Injected {:,} rows into '{}' ({:.1f}% neg)".format(n,cluster,neg_pct))
    return html_str


def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}\n  Dashboard Rebuild  {ts}\n{'='*60}")

    print("\n[1/5] Authenticating (ADC)...")
    svc = get_service(); print("  OK")

    print(f"\n[2/5] Downloading {MASTER_CSV_NAME}...")
    cid = find_file(svc, MASTER_CSV_NAME, DRIVE_FOLDER_ID)
    if not cid: sys.exit(f"ERROR: {MASTER_CSV_NAME} not found")
    df = pd.read_csv(dl_bytes(svc, cid))
    print(f"  Rows: {len(df):,} | {df['week_start'].min()} to {df['week_start'].max()}")

    print("\n[3/5] Building TRANSCRIPTS (with Q1 star format)...")
    transcripts_js, n_rows = build_transcripts(df)

    print(f"\n[4/5] Loading {TEMPLATE_HTML}...")
    if not os.path.exists(TEMPLATE_HTML): sys.exit(f"ERROR: {TEMPLATE_HTML} not found")
    with open(TEMPLATE_HTML,"r",encoding="utf-8") as f: template = f.read()
    html = inject(template, transcripts_js)
    print("\n[4b/5] Injecting cluster HTML...")
    html = inject_cluster_html(html, df)
    html = update_kpis(svc, html, df, n_rows)
    print(f"  Final HTML: {len(html):,} chars")

    print(f"\n[5/5] Uploading {OUTPUT_HTML_NAME}...")
    eid = find_file(svc, OUTPUT_HTML_NAME, DRIVE_FOLDER_ID)
    upload(svc, OUTPUT_HTML_NAME, html.encode("utf-8"), DRIVE_FOLDER_ID, "text/html", eid)

    print(f"\n{'='*60}\n  Done - {OUTPUT_HTML_NAME} is live\n{'='*60}\n")

if __name__ == "__main__":
    main()
