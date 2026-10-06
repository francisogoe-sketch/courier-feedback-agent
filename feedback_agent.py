#!/usr/bin/env python3
"""
Courier Feedback Intelligence Agent v4.0
Handles Q1 / Q2 / Q3 separate CSV files per market + platform.
Q1 = CSAT rating 1-5  → Positive {3,4,5} / Negative {1,2}
Q2 = Resolution Yes/No → Resolution Rate
Q3 = Free text         → Pillar classification
Joins all three on Visitor ID. 7-tab Excel output.
"""

import os, sys, json, argparse, logging, smtplib, tempfile
import urllib.request, pickle, re
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from io import BytesIO
from collections import defaultdict

import pandas as pd
import numpy as np
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

import google.auth
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

_env = Path(__file__).parent / ".env"
if _env.exists():
    for _l in _env.read_text(encoding="utf-8").splitlines():
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _, _v = _l.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler("logs/agent.log", encoding="utf-8")])
log = logging.getLogger("FeedbackAgent")

# ── CONFIG ────────────────────────────────────────────────────────────
class Config:
    AUTH_METHOD        = os.getenv("AUTH_METHOD",       "adc")
    SERVICE_ACCOUNT_KEY= os.getenv("GOOGLE_SA_KEY",     "service_account.json")
    OAUTH_CLIENT_FILE  = os.getenv("OAUTH_CLIENT",      "oauth_client.json")
    OAUTH_TOKEN_FILE   = os.getenv("OAUTH_TOKEN",       "oauth_token.pkl")
    DRIVE_FOLDER_ID    = os.getenv("DRIVE_FOLDER_ID",   "1Z2ugAbgFYgmErVrM-TTEJYGhcsx0U9Cm")
    OUTPUT_FOLDER_ID   = os.getenv("OUTPUT_FOLDER_ID",  "1AJvXVyAi0up23juK1RDQFqc2QoDQ7W-K")
    SLACK_WEBHOOK_URL  = os.getenv("SLACK_WEBHOOK_URL", "")
    SLACK_CHANNEL      = os.getenv("SLACK_CHANNEL",     "#courier-feedback-reports")
    SLACK_ALERT_TAG    = os.getenv("SLACK_ALERT_TAG",   "@here")
    SLACK_ALERT_NEG_PCT= float(os.getenv("SLACK_ALERT_NEG_PCT", "22"))
    SMTP_HOST  = os.getenv("SMTP_HOST","smtp.gmail.com")
    SMTP_PORT  = int(os.getenv("SMTP_PORT","587"))
    SMTP_USER  = os.getenv("SMTP_USER","")
    SMTP_PASS  = os.getenv("SMTP_PASS","")
    EMAIL_TO   = [e.strip() for e in os.getenv("EMAIL_TO","").split(",") if e.strip()]
    EMAIL_FROM = os.getenv("EMAIL_FROM","")
    SUPPORTED_MARKETS  = ["CA","UK","IL","SK","AT","DE","IE"]
    TODAY      = datetime.today()
    WEEK_LABEL = TODAY.strftime("W%V_%Y")
    SCOPES     = ["https://www.googleapis.com/auth/drive"]
    REPORT_TITLE = "T1 SB CSAT — Courier Feedback Intelligence"
    DATA_CAVEAT  = "Scope: CSAT surveys triggered when courier closes live chat. Q1=Rating Q2=Resolution Q3=FreeText"

# ── AUTH ─────────────────────────────────────────────────────────────
class AuthManager:
    def get_credentials(self):
        m = Config.AUTH_METHOD.lower()
        log.info(f"Auth method: {m.upper()}")
        if m=="adc":   return self._adc()
        if m=="oauth": return self._oauth()
        return self._adc()

    def _adc(self):
        try:
            creds, proj = google.auth.default(scopes=Config.SCOPES)
            log.info(f"  ADC loaded (project: {proj})")
            return creds
        except Exception as e:
            raise RuntimeError(f"ADC failed: {e}\nRun: gcloud auth application-default login")

    def _oauth(self):
        tp=Path(Config.OAUTH_TOKEN_FILE); cp=Path(Config.OAUTH_CLIENT_FILE)
        creds=None
        if tp.exists():
            with open(tp,"rb") as f: creds=pickle.load(f)
        if creds and creds.expired and creds.refresh_token: creds.refresh(Request())
        if not creds or not creds.valid:
            flow=InstalledAppFlow.from_client_secrets_file(str(cp),Config.SCOPES)
            creds=flow.run_local_server(port=0)
            with open(tp,"wb") as f: pickle.dump(creds,f)
        return creds

# ── DRIVE CLIENT ─────────────────────────────────────────────────────
class DriveClient:
    def __init__(self):
        creds=AuthManager().get_credentials()
        self.service=build("drive","v3",credentials=creds)
        log.info("Google Drive client ready.")

    def list_csvs(self,folder_id):
        q=(f"'{folder_id}' in parents "
           "and mimeType!='application/vnd.google-apps.folder' and trashed=false")
        r=(self.service.files()
           .list(q=q,fields="files(id,name)",pageSize=300,
                 supportsAllDrives=True,includeItemsFromAllDrives=True,corpora="allDrives")
           .execute())
        files=[f for f in r.get("files",[]) if f["name"].lower().endswith(".csv") and any(q in f["name"].upper() for q in ["Q1","Q2","Q3"])]
        log.info(f"Found {len(files)} CSV files.")
        return files

    def download_csv(self,file_id,file_name):
        req=self.service.files().get_media(fileId=file_id)
        buf=BytesIO()
        dl=MediaIoBaseDownload(buf,req)
        done=False
        while not done: _,done=dl.next_chunk()
        buf.seek(0)
        try:    df=pd.read_csv(buf,encoding="utf-8-sig")
        except: buf.seek(0); df=pd.read_csv(buf,encoding="latin-1")
        log.info(f"  Downloaded: {file_name} ({len(df):,} rows)")
        return df

    def upload_file(self,local_path,folder_id,mime):
        name=Path(local_path).name
        meta={"name":name,"parents":[folder_id]}
        media=MediaFileUpload(local_path,mimetype=mime,resumable=True)
        f=(self.service.files()
           .create(body=meta,media_body=media,fields="id,webViewLink",supportsAllDrives=True)
           .execute())
        log.info(f"  Uploaded: {name}")
        return f.get("webViewLink","")

# ── FILE PARSER ───────────────────────────────────────────────────────
def parse_filename(name):
    n=name.upper()
    q_match=re.search(r'Q([123])',n)
    question=int(q_match.group(1)) if q_match else None
    market="UNKNOWN"
    for m in Config.SUPPORTED_MARKETS:
        if re.search(rf'\b{m}\b',n): market=m; break
    platform="iOS" if "IOS" in n else ("Android" if "AND" in n else "Unknown")
    # Robust: handles 'Sep 28 - Oct 04' AND 'sep_28_oct_04'
    period='unknown'
    _pm=re.search(r'([A-Za-z]{3})\s+(\d{1,2})\s*[-\u2013]\s*([A-Za-z]{3})\s+(\d{1,2})',name)
    if _pm: period=f"{_pm.group(1).capitalize()} {_pm.group(2)} - {_pm.group(3).capitalize()} {_pm.group(4)}"
    else:
        _pm=re.search(r'([a-z]{3})_(\d{1,2})_([a-z]{3})_(\d{1,2})',name.lower())
        if _pm: period=f"{_pm.group(1).capitalize()} {_pm.group(2)} - {_pm.group(3).capitalize()} {_pm.group(4)}"
    return {"market":market,"platform":platform,
            "question":question,"period":period,
            "group_key":f"{market}_{platform}_{period}"}

# ── Q1/Q2/Q3 CONSTANTS ────────────────────────────────────────────────
POS_RATINGS  = {"3","4","5"}
NEG_RATINGS  = {"1","2"}
VALID_RATINGS= {"1","2","3","4","5"}

PILLAR_DISPLAY = {
    "Pillar 1":"Support Quality",
    "Pillar 2":"App / Tech Issues",
    "Pillar 3":"Partner & External Delays",
    "Pillar 4":"Compensation",
    "Pillar 5":"General / Minimal / Positive",
}
PILLAR_ORDER=["Pillar 1","Pillar 2","Pillar 3","Pillar 4","Pillar 5"]

# ── JOIN Q1+Q2+Q3 ─────────────────────────────────────────────────────
def find_vid_col(df):
    for c in ["Visitor ID","visitor_id","respondent_id","VisitorID"]:
        if c in df.columns: return c
    return df.columns[0]

def join_questions(q1,q2,q3,market,platform,period):
    frames={}
    for n,df in [(1,q1),(2,q2),(3,q3)]:
        if df is None: continue
        vc=find_vid_col(df)
        df=df.rename(columns={vc:"visitor_id"})
        df["visitor_id"]=df["visitor_id"].astype(str).str.strip()
        frames[n]=df

    if not frames: return pd.DataFrame()

    base=frames.get(3,frames.get(1,list(frames.values())[0])).copy()
    for old,new in [("Date","submitted_at"),("Response","q3_text"),("response","q3_text")]:
        if old in base.columns and new not in base.columns:
            base=base.rename(columns={old:new})

    if 1 in frames:
        q1f=frames[1].copy()
        rc=None
        for c in q1f.columns:
            if c!="visitor_id" and q1f[c].astype(str).str.strip().isin(list(VALID_RATINGS)+[""]).mean()>0.4:
                rc=c; break
        if not rc: rc=[c for c in q1f.columns if c!="visitor_id"][0] if len(q1f.columns)>1 else None
        if rc:
            q1f=q1f[["visitor_id",rc]].rename(columns={rc:"q1_rating"}).rename(columns={rc:"q1_rating"})
            # Safe Q1 join: prefer courier_id, fall back to visitor_id
            try:
                if "courier_id" in q1f.columns and "courier_id" in base.columns:
                    _q1m=q1f[["courier_id","q1_rating"]].drop_duplicates("courier_id")
                    base=base.merge(_q1m,on="courier_id",how="left")
                else:
                    base=base.merge(q1f[["visitor_id","q1_rating"]],on="visitor_id",how="left")
            except Exception as _je:
                log.warning(f"  Q1 join failed: {_je}")

    if 2 in frames:
        q2f=frames[2].copy()
        rc=[c for c in q2f.columns if c!="visitor_id"][0] if len(q2f.columns)>1 else None
        if rc:
            q2f=q2f[["visitor_id",rc]].rename(columns={rc:"q2_resolution"}).rename(columns={rc:"q2_resolution"})
            base=base.merge(q2f,on="visitor_id",how="left")

    base["market"]=market; base["platform"]=platform; base["period"]=period
    if "q3_text" not in base.columns and "Response" in base.columns:
        base=base.rename(columns={"Response":"q3_text"})
    if "q3_text" not in base.columns: base["q3_text"]=""
    return base

# ── CLASSIFIER ────────────────────────────────────────────────────────
THEMES={
    "Support Quality":{"pillar":"Pillar 1","routing":"ACT","cadence":"Weekly",
        "keywords":["rude","unhelpful","not helpful","did not help","no help",
            "unprofessional","dismissive","incompetent","bad agent","bad support",
            "poor support","worst service","useless","did not resolve","not resolved",
            "closed chat","ended chat","left chat","ignored","not listening","bad service",
            "disrespectful","not answering","no response","no one helped","nobody answered",
            "chat frozen","no reply","f u","fu "]},
    "App / Tech Issues":{"pillar":"Pillar 2","routing":"P&T","cadence":"Weekly",
        "keywords":["app","application","gps","navigation","crash","freeze","frozen",
            "bug","glitch","not working","broken","error","cannot mark","mark delivered",
            "mark arrival","service unavailable","technical","system","platform",
            "login","loading","slow app","packed","age verification","age verify"]},
    "Partner & External Delays":{"pillar":"Pillar 3","routing":"Partner Team","cadence":"Monthly",
        "keywords":["wait","waiting","delay","delayed","late","slow","restaurant wait",
            "pickup delay","too long","long time","taking too long","traffic","order",
            "restaurant closed","shop closed","store closed","wrong address","bad address",
            "packaging","spilled","missing item","wrong item","on transit","in transit"]},
    "Compensation":{"pillar":"Pillar 4","routing":"Courier Pay","cadence":"Monthly",
        "keywords":["payment","pay","payout","not paid","missing payment","low fee",
            "fee","rate","acceptance rate","penalty","compensation","earnings",
            "incentive","bonus","challenge","reward","rewards","credit","refund"]},
    "Positive Feedback":{"pillar":"Pillar 5","routing":"ACT (QBR)","cadence":"Monthly",
        "keywords":["thank","thanks","great","amazing","excellent","good","helpful",
            "very helpful","love","perfect","fantastic","well done","appreciate",
            "happy","satisfied","awesome","best","wonderful","resolved","quick",
            "nice","kind","friendly","super podpora","dakujem"]},
}

class FeedbackClassifier:
    def classify_text(self,text):
        if pd.isna(text) or not str(text).strip() or len(str(text).split())<2:
            return {"theme":"General / Minimal Feedback","pillar":"Pillar 5",
                    "pillar_name":PILLAR_DISPLAY["Pillar 5"],"routing":"Triage","cadence":"Ongoing"}
        lower=str(text).lower()
        for theme,data in THEMES.items():
            for kw in data["keywords"]:
                if kw.lower() in lower:
                    return {"theme":theme,"pillar":data["pillar"],
                            "pillar_name":PILLAR_DISPLAY[data["pillar"]],
                            "routing":data["routing"],"cadence":data["cadence"]}
        return {"theme":"General / Minimal Feedback","pillar":"Pillar 5",
                "pillar_name":PILLAR_DISPLAY["Pillar 5"],"routing":"Triage","cadence":"Ongoing"}

    def classify_dataframe(self,df):
        log.info("  Classifying Q3 text...")
        res=df["q3_text"].apply(self.classify_text).apply(pd.Series)
        df=pd.concat([df,res],axis=1)

        # Q1 CSAT sentiment
        if "q1_rating" in df.columns:
            log.info("  Computing Q1 CSAT sentiment...")
            rs=df["q1_rating"].astype(str).str.strip()
            df["csat_valid"]   =rs.isin(VALID_RATINGS).astype(int)
            df["csat_positive"]=rs.isin(POS_RATINGS).astype(int)
            df["csat_negative"]=rs.isin(NEG_RATINGS).astype(int)
            df["csat_sentiment"]=rs.apply(lambda x:"positive" if x in POS_RATINGS
                                          else("negative" if x in NEG_RATINGS else "no_response"))
        else:
            df["csat_valid"]=0; df["csat_positive"]=0
            df["csat_negative"]=0; df["csat_sentiment"]="no_response"
            log.warning("  Q1 rating not found")

        # Q2 resolution
        if "q2_resolution" in df.columns:
            df["resolved"]=df["q2_resolution"].astype(str).str.lower().str.strip()\
                .isin(["yes","y","1","true","resolved","כן","נפתר"]).astype(int)
        else:
            df["resolved"]=pd.NA

        df["in_X"]=(df["pillar"]!="Pillar 5").astype(int)

        if "submitted_at" in df.columns:
            df["Date"]=pd.to_datetime(df["submitted_at"],errors="coerce")
        elif "Date" in df.columns:
            df["Date"]=pd.to_datetime(df["Date"],errors="coerce")
        else: df["Date"]=pd.NaT

        df["week_start"]=df["Date"].dt.to_period("W").apply(
            lambda p:p.start_time.strftime("%Y-%m-%d") if hasattr(p,"start_time") else "")
        df["month"]=df["Date"].dt.strftime("%Y-%m")
        df["quarter"]=df["Date"].dt.to_period("Q").astype(str)

        v=df["csat_valid"].sum(); pos=df["csat_positive"].sum(); neg=df["csat_negative"].sum()
        log.info(f"  Q1 valid:{v:,}  positive:{pos:,}  negative:{neg:,}")
        log.info(f"  Q3 actionable: {(df['pillar']!='Pillar 5').sum():,}")
        return df

# ── REPORT BUILDER ────────────────────────────────────────────────────
class ReportBuilder:
    C={"dk":"FF1A2E44","bl":"FF2A6496","rd":"FFE84855","gn":"FF4CAF50",
       "am":"FFF4A261","tl":"FF2A9D8F","gy":"FFB0BEC5","wh":"FFFFFFFF","lt":"FFF0F7FF"}
    PC={"Pillar 1":"FFE84855","Pillar 2":"FFF4A261","Pillar 3":"FF2A9D8F",
        "Pillar 4":"FF2A6496","Pillar 5":"FFB0BEC5"}
    SEN={"positive":"FF4CAF50","negative":"FFE84855","neutral":"FFB0BEC5","no_response":"FFE0E0E0"}

    def __init__(self,df,week,q1_sa=None,q2_sa=None):
        self.df=df; self.week=week
        self.wb=Workbook(); self.wb.remove(self.wb.active); self.q1_sa=q1_sa; self.q2_sa=q2_sa

    def _h(self,c,t,bg=None,fg="FFFFFFFF",sz=10,bold=True,align="center"):
        c.value=t; c.fill=PatternFill("solid",fgColor=bg or self.C["dk"])
        c.font=Font(color=fg,size=sz,bold=bold)
        c.alignment=Alignment(horizontal=align,vertical="center",wrap_text=True)

    def _b(self,ws,r1,r2,c1,c2):
        s=Side(style="thin",color="FFB0BEC5")
        for row in ws.iter_rows(min_row=r1,max_row=r2,min_col=c1,max_col=c2):
            for cell in row: cell.border=Border(left=s,right=s,top=s,bottom=s)

    def _period_sheet(self,period_col,sheet_name,color_bg):
        ws=self.wb.create_sheet(sheet_name)
        ws.sheet_view.showGridLines=False
        df=self.df
        if period_col not in df.columns or df[period_col].replace("",pd.NA).isna().all():
            ws["A1"].value=f"{period_col} data not available"; return
        ws.merge_cells("A1:M1")
        self._h(ws["A1"],
            f"{sheet_name} — Q1 CSAT + Pillar Distribution | "
            "Positive%=(Q1∈{3,4,5}/Valid)×100 | Negative%=(Q1∈{1,2}/Valid)×100 | "
            "Pillar%=(Pillar/X)×100 X=Pillars1-4",
            sz=8,bold=False,bg=color_bg,fg="FF1A2E44")
        ws.row_dimensions[1].height=22
        headers=["Period","Market","Total","Q1 Valid","Positive","Pos%",
                 "Negative","Neg%","P1%","P2%","P3%","P4%","Resolution%"]
        widths  =[16,10,10,10,10,10,10,10,9,9,9,9,13]
        for ci,(h,w) in enumerate(zip(headers,widths),1):
            ws.column_dimensions[get_column_letter(ci)].width=w
            self._h(ws.cell(2,ci),h,bg=self.C["gy"],fg=self.C["dk"][2:],sz=9)
        ws.row_dimensions[2].height=26
        ri=3
        valid_periods=df[df[period_col].replace("",pd.NA).notna()]
        for (period,market),grp in valid_periods.groupby([period_col,"market"]):
            v=grp[grp["csat_valid"]==1]; X=grp[grp["in_X"]==1]; Xs=len(X)
            # Use q1_sa filtered by market+period for Q1 CSAT metrics
            q1_sub = pd.DataFrame()
            if hasattr(self,"q1_sa") and self.q1_sa is not None and period_col in self.q1_sa.columns:
                q1_sub = self.q1_sa[(self.q1_sa["market"]==market)&(self.q1_sa[period_col]==period)&(self.q1_sa["csat_valid"]==1)]
            if len(q1_sub)==0 and hasattr(self,"q1_sa") and self.q1_sa is not None:
                q1_sub = self.q1_sa[(self.q1_sa["market"]==market)&(self.q1_sa["csat_valid"]==1)]
            pos=int(q1_sub["csat_positive"].sum()) if len(q1_sub)>0 else int(v["csat_positive"].sum())
            neg=int(q1_sub["csat_negative"].sum()) if len(q1_sub)>0 else int(v["csat_negative"].sum())
            vt=len(q1_sub) if len(q1_sub)>0 else len(v)
            pp=round(pos/vt*100,1) if vt>0 else 0
            np_=round(neg/vt*100,1) if vt>0 else 0
            ps=[round(len(grp[grp["pillar"]==p])/Xs*100,1) if Xs>0 else 0
            for p in ["Pillar 1","Pillar 2","Pillar 3","Pillar 4"]]
            res=""
            if "resolved" in grp.columns and grp["resolved"].notna().any():
                r=grp["resolved"].sum(); res=f"{round(r/len(grp)*100,1)}%"
            vals=[str(period),market,len(grp),vt,int(pos),f"{pp}%",
                  int(neg),f"{np_}%",f"{ps[0]}%",f"{ps[1]}%",f"{ps[2]}%",f"{ps[3]}%",res]
            bg=self.C["lt"] if ri%2==0 else self.C["wh"]
            for ci,val in enumerate(vals,1):
                c=ws.cell(ri,ci); c.value=val
                c.font=Font(size=9); c.fill=PatternFill("solid",fgColor=bg)
                c.alignment=Alignment(horizontal="center",vertical="center")
                if ci==8:
                    try:
                        nv=float(str(val).replace("%",""))
                        fc=self.C["rd"] if nv>22 else(self.C["am"] if nv>18 else self.C["gn"])
                        c.fill=PatternFill("solid",fgColor=fc)
                        c.font=Font(color="FFFFFFFF",size=9,bold=True)
                    except: pass
                if ci==6:
                    c.fill=PatternFill("solid",fgColor=self.C["gn"])
                    c.font=Font(color="FFFFFFFF",size=9,bold=True)
            ws.row_dimensions[ri].height=20; ri+=1
        self._b(ws,2,ri-1,1,len(headers))
        ws.auto_filter.ref=f"A2:{get_column_letter(len(headers))}2"
        ws.freeze_panes="A3"

    def _exec_sheet(self):
        ws=self.wb.create_sheet("Executive Summary")
        ws.sheet_view.showGridLines=False
        ws.column_dimensions["A"].width=3
        for col in ["B","C","D","E","F","G","H"]: ws.column_dimensions[col].width=22
        df=self.df; valid=df[df["csat_valid"]==1]; X=df[df["in_X"]==1]
        vt=len(valid); Xt=len(X)
        if hasattr(self,"q1_sa") and self.q1_sa is not None:
            q1v=self.q1_sa[self.q1_sa["csat_valid"]==1]; vt=len(q1v)
            pp=round(q1v["csat_positive"].sum()/vt*100,1) if vt>0 else 0
            np_=round(q1v["csat_negative"].sum()/vt*100,1) if vt>0 else 0
        else:
            pp=round(valid["csat_positive"].sum()/vt*100,1) if vt>0 else 0
            np_=round(valid["csat_negative"].sum()/vt*100,1) if vt>0 else 0
        ws.merge_cells("B1:H1"); self._h(ws["B1"],Config.REPORT_TITLE,sz=14); ws.row_dimensions[1].height=34
        ws.merge_cells("B2:H2"); self._h(ws["B2"],f"{self.week} | Total:{len(df):,} | Q1 Valid:{vt:,} | X(Pillars1-4):{Xt:,}",bg=self.C["bl"],sz=9); ws.row_dimensions[2].height=18
        ws.merge_cells("B3:H3"); self._h(ws["B3"],Config.DATA_CAVEAT,bg="FFFFF3E0",fg="FF7B3F00",sz=8,bold=False); ws.row_dimensions[3].height=26
        ws.row_dimensions[5].height=44
        tiles=[("B5","C5",f"{vt:,}","Q1 Valid Responses"),
               ("D5","E5",f"{pp}%","Positive CSAT (Q1 3-4-5)"),
               ("F5","G5",f"{np_}%","Negative CSAT (Q1 1-2)"),
               ("H5","H5",f"{Xt:,}","X — Pillars 1-4")]
        for ms,me,val,lbl in tiles:
            ws.merge_cells(f"{ms}:{me}"); c=ws[ms]
            c.value=val; c.fill=PatternFill("solid",fgColor=self.C["bl"])
            c.font=Font(color="FFFFFFFF",size=18,bold=True)
            c.alignment=Alignment(horizontal="center",vertical="center")
            lb=ms[0]+"6"; ws.row_dimensions[6].height=14
            if ms!=me: ws.merge_cells(f"{ms[0]}6:{me[0]}6")
            ws[lb].value=lbl; ws[lb].alignment=Alignment(horizontal="center")
            ws[lb].font=Font(size=8,color="555555")
        ws.merge_cells("B7:H7")
        self._h(ws["B7"],"Formula: Pos%=(Q1∈{3,4,5}/Valid)×100 | Neg%=(Q1∈{1,2}/Valid)×100 | PillarShare%=(Pillar/X)×100 where X=Pillars1-4 only",
                bg="FFE3F2FD",fg="FF1A2E44",sz=8,bold=False); ws.row_dimensions[7].height=18
        ws.merge_cells("B9:H9"); self._h(ws["B9"],"PILLAR DISTRIBUTION — FIXED ORDER (Pillar 5 de-emphasised)",bg=self.C["bl"],sz=10)
        for ci,h in enumerate(["Pillar","Name","Responses","Share of X%","Pos%","Neg%","Routing","Cadence"],2):
            self._h(ws.cell(10,ci),h,bg=self.C["gy"],fg=self.C["dk"][2:],sz=9)
        ws.row_dimensions[10].height=22
        ri=11
        for p in PILLAR_ORDER:
            grp=df[df["pillar"]==p] if "pillar" in df.columns else pd.DataFrame(); v=grp[grp["csat_valid"]==1]
            share=round(len(grp)/Xt*100,1) if (Xt>0 and p!="Pillar 5") else 0
            pp2=round(v["csat_positive"].sum()/len(v)*100,1) if len(v)>0 else 0
            np2=round(v["csat_negative"].sum()/len(v)*100,1) if len(v)>0 else 0
            rt=next((d["routing"] for t,d in THEMES.items() if d["pillar"]==p),"—")
            cd=next((d["cadence"] for t,d in THEMES.items() if d["pillar"]==p),"—")
            is5=(p=="Pillar 5")
            vals=[p,PILLAR_DISPLAY[p],len(grp),
                  "excl. from X" if is5 else f"{share}%",
                  f"{pp2}%",f"{np2}%",rt,cd]
            pc=self.PC.get(p,self.C["gy"])
            rb="FFE0E0E0" if is5 else(self.C["lt"] if ri%2==0 else self.C["wh"])
            for ci,v2 in enumerate(vals,2):
                c=ws.cell(ri,ci); c.value=v2
                c.fill=PatternFill("solid",fgColor=pc if ci==2 else rb)
                c.font=Font(color="FFFFFFFF" if ci==2 else("FF999999" if is5 else"000000"),size=9,italic=is5)
                c.alignment=Alignment(horizontal="center" if ci>2 else"left",vertical="center")
            ws.row_dimensions[ri].height=20; ri+=1
        self._b(ws,10,ri-1,2,9); ws.freeze_panes="B10"

    def _q1_sheet(self):
        ws=self.wb.create_sheet("Q1 CSAT by Market")
        ws.sheet_view.showGridLines=False
        df=self.q1_sa if (hasattr(self,"q1_sa") and self.q1_sa is not None) else self.df
        ws.merge_cells("A1:H1"); self._h(ws["A1"],"Q1 CSAT Ratings — Positive(3-4-5) vs Negative(1-2) by Market",sz=11)
        heads=["Market","Platform","Total","Q1 Valid","Positive","Pos%","Negative","Neg%"]
        ws=[ws if True else None][0]
        for ci,(h,w) in enumerate(zip(heads,[12,12,10,10,12,12,12,12]),1):
            ws.column_dimensions[get_column_letter(ci)].width=w
            self._h(ws.cell(2,ci),h,bg=self.C["gy"],fg=self.C["dk"][2:],sz=9)
        ws.row_dimensions[2].height=24; ri=3
        for (m,pl),grp in df.groupby(["market","platform"]):
            v=grp[grp["csat_valid"]==1]; vt=len(v)
            pos=v["csat_positive"].sum(); neg=v["csat_negative"].sum()
            pp=round(pos/vt*100,1) if vt>0 else 0; np_=round(neg/vt*100,1) if vt>0 else 0
            bg=self.C["lt"] if ri%2==0 else self.C["wh"]
            for ci,val in enumerate([m,pl,len(grp),vt,int(pos),f"{pp}%",int(neg),f"{np_}%"],1):
                c=ws.cell(ri,ci); c.value=val
                c.fill=PatternFill("solid",fgColor=bg); c.font=Font(size=9)
                c.alignment=Alignment(horizontal="center",vertical="center")
                if ci==6: c.fill=PatternFill("solid",fgColor=self.C["gn"]); c.font=Font(color="FFFFFFFF",size=9,bold=True)
                if ci==8:
                    try:
                        nv=float(str(val).replace("%",""))
                        fc=self.C["rd"] if nv>22 else(self.C["am"] if nv>18 else self.C["gn"])
                        c.fill=PatternFill("solid",fgColor=fc); c.font=Font(color="FFFFFFFF",size=9,bold=True)
                    except: pass
            ws.row_dimensions[ri].height=20; ri+=1
        self._b(ws,2,ri-1,1,8)

    def _market_contribution_sheet(self):
        ws=self.wb.create_sheet("Market Contribution")
        ws.sheet_view.showGridLines=False
        df=self.q1_sa if (hasattr(self,"q1_sa") and self.q1_sa is not None) else self.df; valid=df[df["csat_valid"]==1]
        tn=valid["csat_negative"].sum(); tp=valid["csat_positive"].sum()
        ws.merge_cells("A1:F1")
        self._h(ws["A1"],"Market Contribution | Formula: Market Neg/Total Neg×100 | Market Pos/Total Pos×100",sz=9,bold=False,bg="FFE3F2FD",fg="FF1A2E44")
        for ci,h in enumerate(["Market","Mkt Negative","Neg Contribution%","Mkt Positive","Pos Contribution%","Formula"],1):
            ws.column_dimensions[get_column_letter(ci)].width=22
            self._h(ws.cell(2,ci),h,bg=self.C["gy"],fg=self.C["dk"][2:],sz=9)
        ri=3
        for mkt,grp in valid.groupby("market"):
            mn=grp["csat_negative"].sum(); mp=grp["csat_positive"].sum()
            nc=round(mn/tn*100,1) if tn>0 else 0; pc=round(mp/tp*100,1) if tp>0 else 0
            bg=self.C["lt"] if ri%2==0 else self.C["wh"]
            for ci,val in enumerate([mkt,int(mn),f"{nc}%",int(mp),f"{pc}%",f"Neg:{mn}/{tn}={nc}% Pos:{mp}/{tp}={pc}%"],1):
                c=ws.cell(ri,ci); c.value=val
                c.fill=PatternFill("solid",fgColor=bg); c.font=Font(size=9)
                c.alignment=Alignment(horizontal="center",vertical="center")
            ws.row_dimensions[ri].height=20; ri+=1
        self._b(ws,2,ri-1,1,6)

    def _resolution_sheet(self):
        ws=self.wb.create_sheet("Q2 Resolution Rate")
        ws.sheet_view.showGridLines=False
        df=self.df
        ws.merge_cells("A1:E1")
        self._h(ws["A1"],"Q2 Resolution Rate | Formula: Resolved/Total×100 | Note: IL in Hebrew (כן=Yes) - shows 0% until translation added",sz=9,bold=False,bg="FFE8F5E9",fg="FF1A2E44")
        # Use Q2 standalone if available
        if hasattr(self,"q2_sa") and self.q2_sa is not None:
            df=self.q2_sa
        has_q2="resolved" in df.columns and df["resolved"].notna().any()
        if not has_q2:
            ws["A2"].value="Q2 resolution data not available"; ws["A2"].font=Font(size=10,italic=True,color="FF999999"); return
        for ci,h in enumerate(["Breakdown","Group","Total","Resolved","Resolution%"],1):
            ws.column_dimensions[get_column_letter(ci)].width=24
            self._h(ws.cell(2,ci),h,bg=self.C["gy"],fg=self.C["dk"][2:],sz=9)
        ri=3
        for p in PILLAR_ORDER:
            grp=df[df["pillar"]==p] if "pillar" in df.columns else pd.DataFrame()
            if len(grp)==0: continue
            r=grp["resolved"].sum(); rate=round(r/len(grp)*100,1)
            pc=self.PC.get(p,self.C["gy"])
            for ci,val in enumerate(["By Pillar",PILLAR_DISPLAY[p],len(grp),int(r),f"{rate}%"],1):
                c=ws.cell(ri,ci); c.value=val
                c.fill=PatternFill("solid",fgColor=pc if ci==2 else self.C["lt"])
                c.font=Font(color="FFFFFFFF" if ci==2 else"000000",size=9)
                c.alignment=Alignment(horizontal="center",vertical="center")
            ws.row_dimensions[ri].height=20; ri+=1
        ri+=1
        for mkt,grp in df.groupby("market"):
            r=grp["resolved"].sum(); rate=round(r/len(grp)*100,1)
            bg=self.C["lt"] if ri%2==0 else self.C["wh"]
            for ci,val in enumerate(["By Market",mkt,len(grp),int(r),f"{rate}%"],1):
                c=ws.cell(ri,ci); c.value=val
                c.fill=PatternFill("solid",fgColor=bg); c.font=Font(size=9)
                c.alignment=Alignment(horizontal="center",vertical="center")
            ws.row_dimensions[ri].height=20; ri+=1
        self._b(ws,2,ri-1,1,5)

    def _root_cause_sheet(self):
        ws=self.wb.create_sheet("Q3 Root Cause")
        ws.sheet_view.showGridLines=False
        df=self.df
        ws.merge_cells("A1:H1"); self._h(ws["A1"],"Q3 Root Cause — Pillar Classification from Free Text",sz=11)
        cols=["Date","week_start","month","market","platform","pillar","pillar_name",
              "theme","q3_text","csat_sentiment","q1_rating","resolved"]
        avail=[c for c in cols if c in df.columns]
        for ci,col in enumerate(avail,1):
            ws.column_dimensions[get_column_letter(ci)].width=22 if col=="q3_text" else 13
            self._h(ws.cell(2,ci),col.replace("_"," ").title(),sz=9)
        for ri,row in df[avail].head(2000).iterrows():
            er=ri+3; bg=self.C["lt"] if er%2==0 else self.C["wh"]
            pc=self.PC.get(str(row.get("pillar","")),"FFFFFFFF")
            for ci,col in enumerate(avail,1):
                c=ws.cell(er,ci); c.value=str(row.get(col,"")) if pd.notna(row.get(col,"")) else ""
                c.font=Font(size=9); c.alignment=Alignment(horizontal="left",vertical="center",wrap_text=(col=="q3_text"))
                if col=="csat_sentiment":
                    c.fill=PatternFill("solid",fgColor=self.SEN.get(str(row.get(col,"")),"FFE0E0E0"))
                    c.font=Font(color="FFFFFFFF",size=9,bold=True); c.alignment=Alignment(horizontal="center",vertical="center")
                elif col=="pillar":
                    c.fill=PatternFill("solid",fgColor=pc); c.font=Font(color="FFFFFFFF",size=9,bold=True)
                    c.alignment=Alignment(horizontal="center",vertical="center")
                else: c.fill=PatternFill("solid",fgColor=bg)
            ws.row_dimensions[er].height=18
        ws.auto_filter.ref=f"A2:{get_column_letter(len(avail))}2"; ws.freeze_panes="A3"

    def build(self,path):
        log.info("Building 7-tab Excel report (v4.0)...")
        self._exec_sheet()
        self._q1_sheet()
        self._market_contribution_sheet()
        self._resolution_sheet()
        self._root_cause_sheet()
        self._period_sheet("week_start","Weekly Breakdown","FFCCE5F5")
        self._period_sheet("month","Monthly Breakdown","FFE8F5E9")
        self.wb.save(path); log.info(f"Report saved: {path}"); return path

# ── SLACK ─────────────────────────────────────────────────────────────
class SlackNotifier:
    def __init__(self): self.webhook=Config.SLACK_WEBHOOK_URL
    def post_report(self,df,drive_link="",week_label=""):
        if not self.webhook: log.warning("SLACK_WEBHOOK_URL not set."); return False
        valid=df[df["csat_valid"]==1]; vt=len(valid)
        pos=round(valid["csat_positive"].sum()/vt*100,1) if vt>0 else 0
        neg=round(valid["csat_negative"].sum()/vt*100,1) if vt>0 else 0
        is_alert=neg>=Config.SLACK_ALERT_NEG_PCT
        blocks=[{"type":"header","text":{"type":"plain_text","text":f"T1 SB CSAT — Courier Feedback {week_label}","emoji":True}}]
        if is_alert: blocks.append({"type":"section","text":{"type":"mrkdwn","text":f":rotating_light: *ALERT — Q1 Negative CSAT {neg}% exceeds {Config.SLACK_ALERT_NEG_PCT}% threshold* {Config.SLACK_ALERT_TAG}"}})
        blocks.append({"type":"section","fields":[
            {"type":"mrkdwn","text":f"*Total*\n{len(df):,}"},
            {"type":"mrkdwn","text":f"*Q1 Valid*\n{vt:,}"},
            {"type":"mrkdwn","text":f"*Q1 Negative (1-2)*\n{neg}%"},
            {"type":"mrkdwn","text":f"*Q1 Positive (3-5)*\n{pos}%"},
        ]})
        lines=[]
        X=df[df["in_X"]==1]; Xs=len(X)
        for p in PILLAR_ORDER[:4]:
            g=df[df["pillar"]==p]; pct=round(len(g)/Xs*100,1) if Xs>0 else 0
            em={1:":red_circle:",2:":large_yellow_circle:",3:":large_blue_circle:",4:":large_purple_circle:"}[int(p[-1])]
            lines.append(f"{em} *{PILLAR_DISPLAY[p]}* — {len(g):,} ({pct}% of X)")
        blocks.append({"type":"section","text":{"type":"mrkdwn","text":"*Pillar Distribution*\n"+"\n".join(lines)}})
        if drive_link: blocks.append({"type":"actions","elements":[{"type":"button","text":{"type":"plain_text","text":"Open Report in Drive","emoji":True},"url":drive_link,"style":"primary"}]})
        return self._send({"blocks":blocks})
    def post_error(self,err,week_label=""):
        if not self.webhook: return False
        return self._send({"blocks":[{"type":"header","text":{"type":"plain_text","text":f"Agent FAILED — {week_label}","emoji":True}},{"type":"section","text":{"type":"mrkdwn","text":f":rotating_light: ```{str(err)[:400]}```"}}]})
    def _send(self,payload):
        try:
            data=json.dumps(payload).encode("utf-8")
            req=urllib.request.Request(self.webhook,data=data,headers={"Content-Type":"application/json"},method="POST")
            with urllib.request.urlopen(req,timeout=10) as r:
                if r.status==200 and r.read().decode()=="ok": log.info("Slack sent."); return True
        except Exception as e: log.error(f"Slack failed: {e}"); return False

# ── PIPELINE ─────────────────────────────────────────────────────────
class FeedbackAgentPipeline:
    def __init__(self,args):
        self.args=args; self.folder_id=getattr(args,"folder_id",None) or Config.DRIVE_FOLDER_ID
        self.drive=DriveClient(); self.clf=FeedbackClassifier(); self.slack=SlackNotifier()

    def run(self):
        log.info("="*60); log.info(f"COURIER FEEDBACK AGENT v4.0 — {Config.WEEK_LABEL}"); log.info("="*60)
        try:
            log.info("STEP 1  Scanning Drive...")
            all_files=self.drive.list_csvs(self.folder_id)
            if not all_files: raise RuntimeError("No CSV files found.")

            log.info(f"STEP 2  Grouping {len(all_files)} files by Q1/Q2/Q3...")
            groups=defaultdict(lambda:{1:None,2:None,3:None}); group_meta={}
            for f in all_files:
                p=parse_filename(f["name"]); gk=p["group_key"]; q=p["question"]
                if q is None: log.warning(f"  No question number: {f['name']}"); continue
                groups[gk][q]=self.drive.download_csv(f["id"],f["name"])
                group_meta[gk]={k:v for k,v in p.items() if k!="question"}
            log.info(f"  {len(groups)} market/platform/period groups")

            log.info("STEP 2b  Q1+Q2 standalone processing...")
            q1_frames=[]; q2_frames=[]
            for gk2,meta2 in group_meta.items():
                mkt2=meta2["market"]; plt2=meta2["platform"]; prd2=meta2["period"]
                # Q1 standalone
                if groups[gk2][1] is not None:
                    qf=groups[gk2][1].copy()
                    rc=next((col for col in qf.columns if qf[col].astype(str).str.strip().isin(list(VALID_RATINGS)+["","nan"]).mean()>0.2),None)
                    if not rc: rc=next((col for col in qf.columns if col.lower() not in ["visitor id","visitor_id","respondent_id","courier_id"]),None)
                    if rc:
                        _dc = "Date" if "Date" in qf.columns else ("submitted_at" if "submitted_at" in qf.columns else None)
                        _dt = qf[_dc] if _dc else pd.Series([pd.NaT]*len(qf))
                        q1_frames.append(pd.DataFrame({"market":mkt2,"platform":plt2,"period":prd2,"q1_rating":qf[rc].astype(str).str.strip(),"Date":_dt}))
                # Q2 standalone
                if groups[gk2][2] is not None:
                    qf2=groups[gk2][2].copy()
                    import re as _re
                    all_cols=[col for col in qf2.columns if col.lower() not in ["visitor id","visitor_id","respondent_id","courier_id"] and not _re.search(r"[0-9a-f]{8}-[0-9a-f]{4}",str(qf2[col].dropna().head(3).tolist()),_re.I)]
                    yn_vals={"yes","no","y","n","true","false","1","0","resolved","not resolved","unresolved","כן","לא","נפתר","לא נפתר"}
                    non_id=sorted(all_cols,key=lambda col:-qf2[col].astype(str).str.lower().str.strip().isin(yn_vals).mean())
                    if non_id:
                        rc2=non_id[0]
                        resolved_vals=qf2[rc2].astype(str).str.lower().str.strip().isin(["yes","y","1","true","resolved","כן","נפתר"]).astype(int)
                        q2_frames.append(pd.DataFrame({"market":mkt2,"platform":plt2,"period":prd2,"resolved":resolved_vals,"total":1}))
            # Process Q1 standalone
            if q1_frames:
                q1_sa=pd.concat(q1_frames,ignore_index=True)
                q1_sa["csat_valid"]=q1_sa["q1_rating"].isin(VALID_RATINGS).astype(int)
                q1_sa["Date"]=pd.to_datetime(q1_sa["Date"],errors="coerce")
                q1_sa["week_start"]=q1_sa["Date"].dt.to_period("W").dt.start_time.dt.strftime("%Y-%m-%d").fillna("")
                q1_sa["month"]=q1_sa["Date"].dt.strftime("%Y-%m")
                if "submitted_at" in q1_sa.columns:
                    q1_sa["Date"]=pd.to_datetime(q1_sa["submitted_at"],errors="coerce")
                    q1_sa["week_start"]=q1_sa["Date"].dt.to_period("W").apply(lambda p: p.start_time.strftime("%Y-%m-%d") if hasattr(p,"start_time") else "")
                    q1_sa["month"]=q1_sa["Date"].dt.strftime("%Y-%m")
                q1_sa["csat_positive"]=q1_sa["q1_rating"].isin(POS_RATINGS).astype(int)
                q1_sa["csat_negative"]=q1_sa["q1_rating"].isin(NEG_RATINGS).astype(int)
                v2=q1_sa["csat_valid"].sum(); p2=q1_sa["csat_positive"].sum(); n2=q1_sa["csat_negative"].sum()
                log.info(f"  Q1: valid={v2:,} pos={p2:,} neg={n2:,} | pos%={round(p2/v2*100,1) if v2 else 0}% neg%={round(n2/v2*100,1) if v2 else 0}%")
            else: q1_sa=None; log.warning("  No Q1 data found")
            # Process Q2 standalone
            if q2_frames:
                q2_sa=pd.concat(q2_frames,ignore_index=True)
                tot=len(q2_sa); res=q2_sa["resolved"].sum()
                log.info(f"  Q2: total={tot:,} resolved={res:,} rate={round(res/tot*100,1) if tot else 0}%")
            else: q2_sa=None; log.warning("  No Q2 data found")
            log.info("STEP 3  Joining Q1+Q2+Q3 per group...")
            frames=[]
            for gk,meta in group_meta.items():
                q1,q2,q3=groups[gk][1],groups[gk][2],groups[gk][3]
                if q3 is None and q1 is None: continue
                joined=join_questions(q1,q2,q3,meta["market"],meta["platform"],meta["period"])
                if len(joined)>0:
                    joined["group_key"]=gk; frames.append(joined)
                    log.info(f"  {gk}: {len(joined):,} rows (Q1={'y' if q1 is not None else 'n'} Q2={'y' if q2 is not None else 'n'} Q3={'y' if q3 is not None else 'n'})")
            all_df=pd.concat(frames,ignore_index=True) if frames else (_ for _ in ()).throw(RuntimeError("No valid Q1/Q2/Q3 files found in source folder. Check filenames contain Q1, Q2 or Q3."))
            log.info(f"  Combined: {len(all_df):,} rows")

            log.info("STEP 4  Classifying...")
            cdf=self.clf.classify_dataframe(all_df)
            d1=cdf["Date"].dropna().min().strftime("%b%d") if cdf["Date"].notna().any() else "unknown"
            d2=cdf["Date"].dropna().max().strftime("%b%d_%Y") if cdf["Date"].notna().any() else "unknown"
            rfn=f"courier_feedback_report_{d1}-{d2}.xlsx"
            cfn=f"courier_feedback_classified_{d1}-{d2}.csv"

            if self.args.dry_run:
                cdf.to_csv(cfn,index=False); log.info(f"DRY RUN complete. Saved: {cfn}"); return

            log.info("STEP 5  Building 7-tab Excel report...")
            with tempfile.TemporaryDirectory() as tmp:
                xp=os.path.join(tmp,rfn); cp=os.path.join(tmp,cfn)
                ReportBuilder(cdf,Config.WEEK_LABEL,q1_sa=q1_sa,q2_sa=q2_sa).build(xp); cdf.to_csv(cp,index=False)
                log.info("STEP 6  Uploading to Drive...")
                link=self.drive.upload_file(xp,Config.OUTPUT_FOLDER_ID,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.drive.upload_file(cp,Config.OUTPUT_FOLDER_ID,"text/csv")
                if self.args.slack: log.info("STEP 7  Slack..."); self.slack.post_report(cdf,link,Config.WEEK_LABEL)
            log.info("="*60); log.info("PIPELINE COMPLETE"); log.info("="*60)
        except Exception as e:
            log.error(f"FAILED: {e}",exc_info=True)
            if self.args.slack: self.slack.post_error(str(e),Config.WEEK_LABEL)
            raise

def parse_args():
    p=argparse.ArgumentParser(description="Courier Feedback Agent v4.0")
    p.add_argument("--dry-run",action="store_true"); p.add_argument("--slack",action="store_true")
    p.add_argument("--email",action="store_true"); p.add_argument("--folder-id",default=None)
    p.add_argument("--auth",choices=["adc","oauth","key"],default=None)
    return p.parse_args()

if __name__=="__main__":
    args=parse_args()
    if args.auth: Config.AUTH_METHOD=args.auth
    FeedbackAgentPipeline(args).run()
