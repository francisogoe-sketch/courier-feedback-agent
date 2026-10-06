#!/usr/bin/env python3
"""save_index.py - Downloads csat_dashboard_latest.html from Drive and saves as index.html"""
import io, sys
try:
    import google.auth
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
except ImportError:
    sys.exit("Missing google packages")

DRIVE_FOLDER_ID  = "1AJvXVyAi0up23juK1RDQFqc2QoDQ7W-K"
OUTPUT_HTML_NAME = "csat_dashboard_latest.html"
SCOPES = ["https://www.googleapis.com/auth/drive"]

def main():
    print("Downloading dashboard for GitHub Pages...")
    creds, _ = google.auth.default(scopes=SCOPES)
    svc = build("drive","v3",credentials=creds,cache_discovery=False)
    q = f'name="{OUTPUT_HTML_NAME}" and "{DRIVE_FOLDER_ID}" in parents and trashed=false'
    res = svc.files().list(q=q, fields="files(id,name)", pageSize=3,
                           supportsAllDrives=True, includeItemsFromAllDrives=True,
                           corpora="allDrives").execute()
    files = res.get("files",[])
    if not files:
        sys.exit(f"ERROR: {OUTPUT_HTML_NAME} not found on Drive")
    fid = files[0]["id"]
    req = svc.files().get_media(fileId=fid)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done: _, done = dl.next_chunk()
    buf.seek(0)
    html = buf.read().decode("utf-8")
    with open("index.html","w",encoding="utf-8") as f:
        f.write(html)
    print(f"  Saved index.html ({len(html):,} chars)")
    print("  GitHub Pages will serve this file automatically")

if __name__ == "__main__":
    main()
