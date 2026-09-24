#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║         COURIER FEEDBACK INTELLIGENCE AGENT — v1.0                  ║
║         Automated Weekly Classification & Reporting Pipeline         ║
║         Regions supported: CA · UK · IL · SK · AT                   ║
╚══════════════════════════════════════════════════════════════════════╝

HOW TO RUN:
  python feedback_agent.py                    # process this week
  python feedback_agent.py --dry-run          # classify only, no upload
  python feedback_agent.py --folder-id <id>  # override Drive folder
  python feedback_agent.py --email            # send report via email

SCHEDULE (cron — every Monday 6 AM):
  0 6 * * 1 /usr/bin/python3 /path/to/feedback_agent.py >> /var/log/feedback_agent.log 2>&1
"""

# ─── STANDARD LIBRARY ────────────────────────────────────────────────
import os
import sys
import json
import argparse
import logging
import smtplib
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from io import BytesIO, StringIO

# ─── THIRD-PARTY (pip install -r requirements.txt) ───────────────────
import pandas as pd
import numpy as np
import altair as alt

from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# ─── LOGGING ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("feedback_agent.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("FeedbackAgent")


# ══════════════════════════════════════════════════════════════════════
# 1.  CONFIGURATION  (edit this section or use environment variables)
# ══════════════════════════════════════════════════════════════════════
class Config:
    # ── Google Drive ─────────────────────────────────────────────────
    DRIVE_FOLDER_ID     = os.getenv("DRIVE_FOLDER_ID",
                          "1Z2ugAbgFYgmErVrM-TTEJYGhcsx0U9Cm")   # shared folder
    SERVICE_ACCOUNT_KEY = os.getenv("GOOGLE_SA_KEY",
                          "service_account.json")                 # path to key file
    OUTPUT_FOLDER_NAME  = "weekly_reports"
    OUTPUT_FOLDER_ID    = "1AJvXVyAi0up23juK1RDQFqc2QoDQ7W-K"                        # subfolder for reports

    # ── Email (optional — leave blank to skip) ────────────────────────
    SMTP_HOST     = os.getenv("SMTP_HOST",   "smtp.gmail.com")
    SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER     = os.getenv("SMTP_USER",   "")
    SMTP_PASS     = os.getenv("SMTP_PASS",   "")
    EMAIL_TO      = os.getenv("EMAIL_TO",    "").split(",")       # comma-separated
    EMAIL_FROM    = os.getenv("EMAIL_FROM",  "")

    # ── Processing ────────────────────────────────────────────────────
    CSV_PATTERN         = ".csv"                                  # file extension filter
    MIN_RESPONSE_WORDS  = 2                                       # filter noise
    SUPPORTED_REGIONS   = ["CA","UK","IL","SK","AT","DE","IE"]
    TRANSLATION_ENABLED = False   # set True + add API key to translate IL/SK
    TRANSLATE_API_KEY   = os.getenv("GOOGLE_TRANSLATE_KEY", "")

    # ── File naming ───────────────────────────────────────────────────
    TODAY            = datetime.today()
    WEEK_LABEL       = TODAY.strftime("W%V_%Y")
    REPORT_FILENAME  = f"courier_feedback_report_{WEEK_LABEL}.xlsx"
    CSV_OUT_FILENAME = f"courier_feedback_classified_{WEEK_LABEL}.csv"


# ══════════════════════════════════════════════════════════════════════
# 2.  TIER-1 CLASSIFICATION ENGINE
#     Source: tier_1_csat_q3_feedback_theme_logic.pdf
#     Priority order matches the CSAT theme priority sequence
# ══════════════════════════════════════════════════════════════════════
THEMES = {
    # Priority 1 — tech root cause caught first
    "App / Tech Issues": {
        "super_category": "System/Platform-related",
        "routing_team":   "P&T",
        "cadence":        "Weekly",
        "emoji":          ":large_yellow_circle:",
        "keywords": [
            "app","application","gps","navigation","crash","freeze","frozen",
            "bug","glitch","not working","broken","error","cannot mark",
            "mark delivered","mark arrival","mark complete",
            "service unavailable","technical","system","platform",
            "login","sign in","loading","slow app","packed","nefunguje",
        ],
    },
    # Priority 2 — payment caught early to prevent keyword theft by support theme
    "Compensation / Payment": {
        "super_category": "System/Platform-related",
        "routing_team":   "Courier Pay",
        "cadence":        "Monthly",
        "emoji":          ":large_yellow_circle:",
        "keywords": [
            "payment","pay","payout","not paid","missing payment",
            "low fee","fee","rate","acceptance rate","penalty",
            "compensation","earnings","incentive","bonus",
            "challenge","reward","rewards","credit","refund",
        ],
    },
    # Priority 3 — age verification caught early for same reason
    "Age Verification": {
        "super_category": "System/Platform-related",
        "routing_team":   "Quality / P&T",
        "cadence":        "Monthly",
        "emoji":          ":white_circle:",
        "keywords": [
            "age verification","age verify","id check","verify age",
            "forgot to do age","age check",
        ],
    },
    # Priority 4 — support quality: big bucket, now clean of payment/age responses
    "Support Quality": {
        "super_category": "Agent Support-related",
        "routing_team":   "ACT",
        "cadence":        "Weekly",
        "emoji":          ":red_circle:",
        "keywords": [
            "rude","unhelpful","not helpful","did not help","no help",
            "unprofessional","dismissive","incompetent","bad agent",
            "bad support","poor support","worst support","worst service",
            "horrible support","terrible support","useless","did not resolve",
            "not resolved","closed chat","closed my ticket","ended chat",
            "end the chat","left chat","without explain","without resolving",
            "before answering","ignored","not listening","bad service",
            "poor service","never helped","waste of time","not answering",
            "no response","not professional","disrespectful","very rude",
            "extremely rude","no one helped","nobody answered","nobody here",
            "no answer","chat frozen","no reply","unanswered","never answered",
            "no agent","f u","fu ",
        ],
    },
    # Priority 5 — outside courier ops (restaurant/traffic)
    "Partner / External Delays": {
        "super_category": "Partner-related",
        "routing_team":   "Partner Team",
        "cadence":        "Monthly",
        "emoji":          ":large_blue_circle:",
        "keywords": [
            "wait","waiting","delay","delayed","late","slow",
            "restaurant wait","pickup delay","too long","long time",
            "taking too long","traffic","restaurant slow","partner",
        ],
    },
    # Priority 6 — order flow (overlaps with Partner)
    "Order Issues": {
        "super_category": "Partner-related",
        "routing_team":   "Partner / P&T",
        "cadence":        "Monthly",
        "emoji":          ":large_blue_circle:",
        "keywords": [
            "order","restaurant closed","shop closed","store closed",
            "restaurant timing","wrong address","bad address","wrong location",
            "cannot find","incorrect address","packaging","spilled",
            "missing item","wrong item","order not showing",
            "in transit","on transit","delivery issue",
        ],
    },
    # Priority 7 — positive: routed to ACT for monthly QBR content
    "Positive Feedback": {
        "super_category": "Positive",
        "routing_team":   "ACT (QBR)",
        "cadence":        "Monthly",
        "emoji":          ":large_green_circle:",
        "keywords": [
            "thank","thanks","great","amazing","excellent","good",
            "helpful","very helpful","love","perfect","fantastic",
            "well done","appreciate","happy","satisfied","awesome",
            "best","wonderful","superb","brilliant","resolved",
            "quick","fast response","nice","kind","friendly",
            "super podpora","dakujem",
        ],
    },
}

POSITIVE_WORDS = [
    "thank","thanks","great","good","love","amazing","excellent","helpful",
    "perfect","fantastic","awesome","best","wonderful","super","brilliant",
    "nice","happy","satisfied","kind","friendly","resolved","quick",
    "אוהב","dakujem","ochotna",
]
NEGATIVE_WORDS = [
    "rude","not help","no help","bad","worst","terrible","horrible","useless",
    "unprofessional","dismissive","incompetent","not resolve","closed chat",
    "nobody","no response","error","crash","broken","not working","delay",
    "late","missing","wrong","poor","problem","issue","גרוע","f u",
]


# ══════════════════════════════════════════════════════════════════════
# 3.  GOOGLE DRIVE CLIENT
# ══════════════════════════════════════════════════════════════════════
class DriveClient:
    SCOPES = [
        "https://www.googleapis.com/auth/drive",
    ]

    def __init__(self):
        import google.auth
        creds, _ = google.auth.default(scopes=self.SCOPES)
        self.service = build("drive", "v3", credentials=creds)
        log.info("Google Drive client initialised.")

    def list_csvs(self, folder_id: str) -> list[dict]:
        """Return all CSV files in the given Drive folder."""
        query = (
            f"'{folder_id}' in parents "
            f"and mimeType != 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        )
        result = (
            self.service.files()
            .list(q=query, fields="files(id,name,modifiedTime)", pageSize=200, supportsAllDrives=True, includeItemsFromAllDrives=True)
            .execute()
        )
        files = [
            f for f in result.get("files", [])
            if f["name"].lower().endswith(".csv")
        ]
        log.info(f"Found {len(files)} CSV files in Drive folder {folder_id}.")
        return files

    def download_csv(self, file_id: str, file_name: str) -> pd.DataFrame:
        """Download a Drive CSV file and return it as a DataFrame."""
        request = self.service.files().get_media(fileId=file_id)
        buf = BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buf.seek(0)
        df = pd.read_csv(buf, encoding="utf-8-sig")
        log.info(f"  ↓ Downloaded: {file_name}  ({len(df)} rows)")
        return df

    def get_or_create_folder(self, parent_id: str, name: str) -> str:
        """Get or create a subfolder in Drive; return its ID."""
        query = (
            f"'{parent_id}' in parents "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and name = '{name}' and trashed = false"
        )
        result = self.service.files().list(q=query, fields="files(id)").execute()
        folders = result.get("files", [])
        if folders:
            return folders[0]["id"]
        meta = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = self.service.files().create(body=meta, fields="id",
                    supportsAllDrives=True).execute()
        log.info(f"  Created Drive subfolder: {name}")
        return folder["id"]

    def upload_file(self, local_path: str, folder_id: str, mime: str) -> str:
        """Upload a local file to Drive; return its web view link."""
        name = Path(local_path).name
        meta = {"name": name, "parents": [folder_id]}
        media = MediaFileUpload(local_path, mimetype=mime, resumable=True)
        f = (
            self.service.files()
            .create(body=meta, media_body=media, fields="id,webViewLink", supportsAllDrives=True)
            .execute()
        )
        log.info(f"  ↑ Uploaded: {name}  → {f.get('webViewLink')}")
        return f.get("webViewLink", "")


# ══════════════════════════════════════════════════════════════════════
# 4.  CLASSIFIER
# ══════════════════════════════════════════════════════════════════════
class FeedbackClassifier:
    """
    Applies Tier-1 keyword classification in priority order.
    Returns (theme, super_category, routing_team, cadence, sentiment).
    """

    def classify(self, text: str) -> dict:
        if pd.isna(text) or not str(text).strip():
            return self._default()
        cleaned = str(text).strip()
        if len(cleaned.split()) < Config.MIN_RESPONSE_WORDS:
            return self._default()

        lower = cleaned.lower()
        for theme, data in THEMES.items():
            for kw in data["keywords"]:
                if kw.lower() in lower:
                    return {
                        "theme":         theme,
                        "super_category": data["super_category"],
                        "routing_team":  data["routing_team"],
                        "cadence":       data["cadence"],
                        "sentiment":     self._sentiment(lower),
                    }
        return {
            "theme":         "General / Minimal Feedback",
            "super_category": "Unclassified",
            "routing_team":  "Triage / ML Review",
            "cadence":       "Ongoing",
            "sentiment":     self._sentiment(lower),
        }

    @staticmethod
    def _default() -> dict:
        return {
            "theme":         "General / Minimal Feedback",
            "super_category": "Unclassified",
            "routing_team":  "Triage / ML Review",
            "cadence":       "Ongoing",
            "sentiment":     "neutral",
        }

    @staticmethod
    def _sentiment(lower: str) -> str:
        pos = sum(1 for w in POSITIVE_WORDS if w in lower)
        neg = sum(1 for w in NEGATIVE_WORDS if w in lower)
        if pos > neg:   return "positive"
        if neg > pos:   return "negative"
        return "neutral"

    def classify_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Classify all rows; add columns in-place."""
        results = df["Response"].apply(self.classify).apply(pd.Series)
        return pd.concat([df, results], axis=1)


# ══════════════════════════════════════════════════════════════════════
# 5.  REPORT BUILDER
# ══════════════════════════════════════════════════════════════════════
class ReportBuilder:
    C = {
        "hdr_dark":  "FF1A2E44",
        "hdr_blue":  "FF2A6496",
        "red":       "FFE84855",
        "green":     "FF4CAF50",
        "amber":     "FFF4A261",
        "teal":      "FF2A9D8F",
        "grey":      "FFB0BEC5",
        "lt_blue":   "FFCCE5F5",
        "lt_grey":   "FFF5F5F5",
        "white":     "FFFFFFFF",
        "row_alt":   "FFF0F7FF",
    }
    CAT_COLORS = {
        "Agent Support-related":    "FFFFE0E0",
        "System/Platform-related":  "FFFFF3E0",
        "Partner-related":          "FFE0F5F3",
        "Positive":                 "FFE8F5E9",
        "Unclassified":             "FFF5F5F5",
    }
    SENT_COLORS = {
        "positive": "FF4CAF50",
        "negative": "FFE84855",
        "neutral":  "FFB0BEC5",
    }

    def __init__(self, df: pd.DataFrame, week_label: str):
        self.df = df
        self.week = week_label
        self.wb = Workbook()
        self.wb.remove(self.wb.active)

    # ── helpers ──────────────────────────────────────────────────────
    def _hdr(self, cell, text, bg=None, fg="FFFFFFFF", sz=12, bold=True, align="center"):
        bg = bg or self.C["hdr_dark"]
        cell.value = text
        cell.fill = PatternFill("solid", fgColor=bg)
        cell.font = Font(color=fg, size=sz, bold=bold)
        cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=True)

    def _thin(self, ws, r1, r2, c1, c2):
        s = Side(style="thin", color="FFB0BEC5")
        for row in ws.iter_rows(min_row=r1, max_row=r2, min_col=c1, max_col=c2):
            for cell in row:
                cell.border = Border(left=s, right=s, top=s, bottom=s)

    # ── Sheet 1: Executive Summary ────────────────────────────────────
    def _sheet_exec(self):
        ws = self.wb.create_sheet("📊 Executive Summary")
        ws.sheet_view.showGridLines = False
        ws.column_dimensions["A"].width = 3
        for col in ["B","C","D","E","F","G","H"]:
            ws.column_dimensions[col].width = 22

        df = self.df
        total = len(df)
        classified = df[df["theme"] != "General / Minimal Feedback"]
        neg = df[df["sentiment"] == "negative"]
        pos = df[df["sentiment"] == "positive"]

        # Title
        ws.merge_cells("B1:H1")
        self._hdr(ws["B1"],
                  f"🚴  COURIER FEEDBACK INTELLIGENCE REPORT  —  {self.week}",
                  sz=15)
        ws.row_dimensions[1].height = 40

        ws.merge_cells("B2:H2")
        date_range = f"{df['Date'].min().date()} → {df['Date'].max().date()}"
        self._hdr(ws["B2"],
                  f"{date_range}  |  {df['region'].nunique()} Regions  |  {total:,} Responses",
                  bg=self.C["hdr_blue"], sz=12)
        ws.row_dimensions[2].height = 24

        # KPI tiles
        ws.row_dimensions[4].height = 50
        kpi_tiles = [
            ("B4","C4", f"{total:,}",            "Total Responses"),
            ("D4","E4", f"{len(classified)/total*100:.1f}%", "Classification Rate"),
            ("F4","G4", f"{len(pos)/total*100:.1f}%",        "Positive Rate"),
            ("H4","H4", f"{len(neg)/total*100:.1f}%",        "Negative Rate"),
        ]
        for ms, me, val, lbl in kpi_tiles:
            ws.merge_cells(f"{ms}:{me}")
            c = ws[ms]
            c.value = val
            c.fill = PatternFill("solid", fgColor=self.C["hdr_blue"])
            c.font = Font(color="FFFFFFFF", size=22, bold=True)
            c.alignment = Alignment(horizontal="center", vertical="center")
        for ms, me, lbl in [("B5","C5","Total Responses"),("D5","E5","Classification Rate"),
                             ("F5","G5","Positive Rate"),("H5","H5","Negative Rate")]:
            ws.merge_cells(f"{ms}:{me}")
            ws[ms].value = lbl
            ws[ms].alignment = Alignment(horizontal="center")
            ws[ms].font = Font(size=10, color="333333")

        # Super-category table
        ws.merge_cells("B7:H7")
        self._hdr(ws["B7"], "SUPER-CATEGORY BREAKDOWN", bg=self.C["hdr_blue"], sz=12)
        for ci, h in enumerate(["Category","Responses","% Total","Neg Rate","Routing Team"], 2):
            self._hdr(ws.cell(8, ci), h, bg=self.C["grey"],
                      fg=self.C["hdr_dark"][2:], sz=11)
        ws.row_dimensions[8].height = 28

        cat_bg = {"Agent Support-related": self.C["red"],
                  "System/Platform-related": self.C["amber"],
                  "Partner-related": self.C["teal"],
                  "Positive": self.C["green"],
                  "Unclassified": self.C["grey"]}

        ri = 9
        for cat, grp in df.groupby("super_category"):
            neg_r = grp[grp["sentiment"]=="negative"]
            vals = [cat, len(grp), f"{len(grp)/total*100:.1f}%",
                    f"{len(neg_r)/total*100:.1f}%",
                    grp["routing_team"].mode().iloc[0] if len(grp)>0 else "—"]
            bg = cat_bg.get(cat, self.C["grey"])
            for ci, v in enumerate(vals, 2):
                c = ws.cell(ri, ci)
                c.value = v
                c.fill = PatternFill("solid",
                    fgColor=bg if ci==2 else (self.C["row_alt"] if ri%2==0 else self.C["white"]))
                c.font = Font(color="FFFFFFFF" if ci==2 else "000000", size=11)
                c.alignment = Alignment(horizontal="center" if ci>2 else "left",
                                        vertical="center")
            ws.row_dimensions[ri].height = 24
            ri += 1

        self._thin(ws, 8, ri-1, 2, 6)
        ws.freeze_panes = "B8"

    # ── Sheet 2: Classified Data (top 1000) ──────────────────────────
    def _sheet_data(self):
        ws = self.wb.create_sheet("📋 Classified Data")
        ws.sheet_view.showGridLines = False
        cols  = ["Date","region","Response","theme","super_category","sentiment","routing_team"]
        hdrs  = ["Date","Region","Response","Theme","Super-Category","Sentiment","Routing Team"]
        widths= [20,10,55,28,24,12,22]
        for ci,(h,w) in enumerate(zip(hdrs,widths),1):
            ws.column_dimensions[get_column_letter(ci)].width = w
            self._hdr(ws.cell(1,ci), h, sz=11)
        ws.row_dimensions[1].height = 30

        for ri, row in self.df[cols].head(1000).iterrows():
            er = ri + 2
            bg = self.CAT_COLORS.get(str(row.get("super_category","")), "FFFFFFFF")
            for ci, col in enumerate(cols, 1):
                c = ws.cell(er, ci)
                c.value = str(row[col]) if pd.notna(row.get(col)) else ""
                c.font = Font(size=10)
                c.alignment = Alignment(horizontal="left", vertical="center",
                                        wrap_text=(ci==3))
                if ci == 6:
                    sc = self.SENT_COLORS.get(str(row.get("sentiment","")), self.C["grey"])
                    c.fill = PatternFill("solid", fgColor=sc)
                    c.font = Font(color="FFFFFFFF", size=10, bold=True)
                    c.alignment = Alignment(horizontal="center", vertical="center")
                else:
                    c.fill = PatternFill("solid", fgColor=bg)
            ws.row_dimensions[er].height = 20

        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}1"
        ws.freeze_panes = "A2"

    # ── Sheet 3: Region Scorecard ─────────────────────────────────────
    def _sheet_region(self):
        ws = self.wb.create_sheet("🗺️ Region Scorecard")
        ws.sheet_view.showGridLines = False
        hdrs   = ["Region","Total","Positive%","Negative%","Agent Issues%","App/Tech%","Top Theme"]
        widths = [20,10,14,14,16,14,30]
        for ci,(h,w) in enumerate(zip(hdrs,widths),1):
            ws.column_dimensions[get_column_letter(ci)].width = w
            self._hdr(ws.cell(1,ci), h, sz=11)
        ws.row_dimensions[1].height = 30

        ri = 2
        total = len(self.df)
        for region, grp in self.df.groupby("region"):
            n = len(grp)
            pos_p = len(grp[grp["sentiment"]=="positive"])/n*100
            neg_p = len(grp[grp["sentiment"]=="negative"])/n*100
            ag_p  = len(grp[grp["super_category"]=="Agent Support-related"])/n*100
            ap_p  = len(grp[grp["super_category"]=="System/Platform-related"])/n*100
            top   = grp[grp["theme"]!="General / Minimal Feedback"]["theme"].mode()
            top   = top.iloc[0] if len(top) else "N/A"
            vals  = [region, n, f"{pos_p:.1f}%", f"{neg_p:.1f}%",
                     f"{ag_p:.1f}%", f"{ap_p:.1f}%", top]
            bg = self.C["row_alt"] if ri%2==0 else self.C["white"]
            for ci, v in enumerate(vals, 1):
                c = ws.cell(ri, ci)
                c.value = v
                c.font = Font(size=11)
                c.alignment = Alignment(horizontal="center" if ci>1 else "left",
                                        vertical="center")
                c.fill = PatternFill("solid", fgColor=bg)
                if ci == 4:
                    nv = float(str(v).replace("%",""))
                    fc = self.C["red"] if nv>22 else (self.C["amber"] if nv>18 else self.C["green"])
                    c.fill = PatternFill("solid", fgColor=fc)
                    c.font = Font(color="FFFFFFFF", size=11, bold=True)
            ws.row_dimensions[ri].height = 26
            ri += 1
        self._thin(ws, 1, ri-1, 1, 7)

    # ── Sheet 4: Weekly Trend ─────────────────────────────────────────
    def _sheet_weekly(self):
        ws = self.wb.create_sheet("📈 Weekly Trend")
        ws.sheet_view.showGridLines = False
        self.df["week"] = pd.to_datetime(self.df["Date"]).dt.to_period("W").astype(str)
        wkly = self.df.groupby("week").agg(
            total=("Response","count"),
            positive=("sentiment", lambda x:(x=="positive").sum()),
            negative=("sentiment", lambda x:(x=="negative").sum()),
            agent=("super_category", lambda x:(x=="Agent Support-related").sum()),
            system=("super_category", lambda x:(x=="System/Platform-related").sum()),
            partner=("super_category", lambda x:(x=="Partner-related").sum()),
        ).reset_index()
        wkly["neg_pct"] = (wkly["negative"]/wkly["total"]*100).round(1)
        wkly["pos_pct"] = (wkly["positive"]/wkly["total"]*100).round(1)

        hdrs   = ["Week","Total","Positive","Negative","Neg%","Agent","System","Partner","Pos%"]
        widths = [22,10,11,11,10,12,12,12,10]
        for ci,(h,w) in enumerate(zip(hdrs,widths),1):
            ws.column_dimensions[get_column_letter(ci)].width = w
            self._hdr(ws.cell(1,ci), h, sz=11)
        ws.row_dimensions[1].height = 30

        for ri, row in wkly.iterrows():
            er = ri+2
            vals = [row["week"], row["total"], row["positive"], row["negative"],
                    f"{row['neg_pct']}%", row["agent"], row["system"],
                    row["partner"], f"{row['pos_pct']}%"]
            bg = self.C["row_alt"] if er%2==0 else self.C["white"]
            for ci, v in enumerate(vals, 1):
                c = ws.cell(er, ci)
                c.value = v
                c.fill = PatternFill("solid", fgColor=bg)
                c.font = Font(size=11)
                c.alignment = Alignment(horizontal="center", vertical="center")
                if ci==5:
                    nv = float(str(v).replace("%",""))
                    if nv>22: c.fill = PatternFill("solid", fgColor=self.C["red"])
                    elif nv>20: c.fill = PatternFill("solid", fgColor=self.C["amber"])
            ws.row_dimensions[er].height = 24
        self._thin(ws, 1, er, 1, 9)

    def build(self, out_path: str) -> str:
        log.info("Building Excel report…")
        self._sheet_exec()
        self._sheet_data()
        self._sheet_region()
        self._sheet_weekly()
        self.wb.save(out_path)
        log.info(f"Report saved → {out_path}")
        return out_path


# ══════════════════════════════════════════════════════════════════════
# 6.  EMAIL SENDER
# ══════════════════════════════════════════════════════════════════════
class EmailSender:
    def __init__(self):
        self.cfg = Config

    def send(self, attachments: list[str], drive_link: str = ""):
        if not self.cfg.SMTP_USER or not self.cfg.EMAIL_TO:
            log.info("Email config not set — skipping email.")
            return
        log.info(f"Sending email to: {self.cfg.EMAIL_TO}")
        msg = MIMEMultipart()
        msg["From"]    = self.cfg.EMAIL_FROM or self.cfg.SMTP_USER
        msg["To"]      = ", ".join(self.cfg.EMAIL_TO)
        msg["Subject"] = f"📊 Courier Feedback Report — {Config.WEEK_LABEL}"

        body = f"""Hi team,

The weekly courier feedback analysis is ready for {Config.WEEK_LABEL}.

{"📁 Drive link: " + drive_link if drive_link else ""}

Please find the full report and classified CSV attached.

Summary highlights are on the Executive Summary tab of the Excel file.

Regards,
Courier Feedback Intelligence Agent 🚴
"""
        msg.attach(MIMEText(body, "plain"))
        for path in attachments:
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition",
                                f"attachment; filename={Path(path).name}")
                msg.attach(part)

        with smtplib.SMTP(self.cfg.SMTP_HOST, self.cfg.SMTP_PORT) as server:
            server.starttls()
            server.login(self.cfg.SMTP_USER, self.cfg.SMTP_PASS)
            server.sendmail(self.cfg.EMAIL_FROM or self.cfg.SMTP_USER,
                            self.cfg.EMAIL_TO, msg.as_string())
        log.info("Email sent successfully.")


# ══════════════════════════════════════════════════════════════════════
# 7.  MAIN PIPELINE ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════
class FeedbackAgentPipeline:
    """
    End-to-end pipeline:
      Drive → Download CSVs → Classify → Build Report → Upload → Email
    """

    def __init__(self, args):
        self.args       = args
        self.drive      = DriveClient()
        self.classifier = FeedbackClassifier()
        self.folder_id  = args.folder_id or Config.DRIVE_FOLDER_ID

    def run(self):
        log.info("═" * 60)
        log.info(f"COURIER FEEDBACK AGENT — {Config.WEEK_LABEL}")
        log.info("═" * 60)

        # ── Step 1: Discover files ─────────────────────────────────────
        log.info("STEP 1 › Scanning Drive folder for CSV files…")
        csv_files = self.drive.list_csvs(self.folder_id)
        if not csv_files:
            log.warning("No CSV files found. Exiting.")
            return

        # ── Step 2: Download & combine ────────────────────────────────
        log.info(f"STEP 2 › Downloading {len(csv_files)} files…")
        frames = []
        for f in csv_files:
            df = self.drive.download_csv(f["id"], f["name"])
            # Infer region from filename (e.g. copy_of_ca_ios_... → CA)
            name_lower = f["name"].lower()
            region = "UNKNOWN"
            for r in Config.SUPPORTED_REGIONS:
                if f"_{r.lower()}_" in name_lower or name_lower.startswith(r.lower()):
                    region = r
                    break
            df["region"]    = region
            df["source_file"] = f["name"]
            frames.append(df)

        all_df = pd.concat(frames, ignore_index=True)
        all_df["Date"] = pd.to_datetime(all_df["Date"], errors="coerce")
        log.info(f"  Combined: {len(all_df):,} total rows across {len(frames)} files.")

        # ── Step 3: Classify ──────────────────────────────────────────
        log.info("STEP 3 › Classifying all responses…")
        classified_df = self.classifier.classify_dataframe(all_df)
        classified_rate = (
            classified_df["theme"].ne("General / Minimal Feedback").sum()
            / len(classified_df) * 100
        )
        neg_rate = (classified_df["sentiment"]=="negative").mean()*100
        log.info(f"  Classification rate : {classified_rate:.1f}%")
        log.info(f"  Negative sentiment  : {neg_rate:.1f}%")

        if self.args.dry_run:
            log.info("DRY RUN — stopping before report/upload.")
            classified_df.to_csv(Config.CSV_OUT_FILENAME, index=False)
            log.info(f"  Saved classified CSV locally: {Config.CSV_OUT_FILENAME}")
            return

        # ── Step 4: Build report ──────────────────────────────────────
        log.info("STEP 4 › Building Excel report…")
        with tempfile.TemporaryDirectory() as tmp:
            xlsx_path = os.path.join(tmp, Config.REPORT_FILENAME)
            csv_path  = os.path.join(tmp, Config.CSV_OUT_FILENAME)

            builder = ReportBuilder(classified_df, Config.WEEK_LABEL)
            builder.build(xlsx_path)
            classified_df.to_csv(csv_path, index=False)
            log.info(f"  Excel saved → {Config.REPORT_FILENAME}")
            log.info(f"  CSV saved   → {Config.CSV_OUT_FILENAME}")

            # ── Step 5: Upload to Drive ────────────────────────────────
            log.info("STEP 5 › Uploading outputs to Drive…")
            out_fid = Config.OUTPUT_FOLDER_ID
            drive_link = self.drive.upload_file(
                xlsx_path, out_fid,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            self.drive.upload_file(csv_path, out_fid, "text/csv")

            # ── Step 6: Email ──────────────────────────────────────────
            if self.args.email:
                log.info("STEP 6 › Sending email…")
                EmailSender().send([xlsx_path, csv_path], drive_link)

        log.info("═" * 60)
        log.info("✅  PIPELINE COMPLETE")
        log.info(f"   Report uploaded → {drive_link}")
        log.info("═" * 60)


# ══════════════════════════════════════════════════════════════════════
# 8.  CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════
def parse_args():
    p = argparse.ArgumentParser(
        description="Courier Feedback Intelligence Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--slack",action="store_true");p.add_argument("--dry-run",   action="store_true",
                   help="Classify only; do not upload or email")
    p.add_argument("--folder-id", default=None,
                   help="Override Drive folder ID")
    p.add_argument("--email",     action="store_true",
                   help="Send report via email after processing")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    pipeline = FeedbackAgentPipeline(args)
    pipeline.run()
