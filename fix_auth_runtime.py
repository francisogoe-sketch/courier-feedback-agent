#!/usr/bin/env python3
# fix_auth_runtime.py - fixes ADC auth in rebuild_dashboard.py at runtime
# Called by GitHub Actions workflow BEFORE rebuild_dashboard.py runs
import re

with open('rebuild_dashboard.py', encoding='utf-8') as f:
    c = f.read()

if 'GDRIVE_SERVICE_ACCOUNT_JSON' in c:
    print('Patching auth: service account -> ADC...')
    ADC = (
        'def get_service():\n'
        '    import google.auth\n'
        '    creds, _ = google.auth.default(scopes=SCOPES)\n'
        '    return build("drive","v3",credentials=creds,cache_discovery=False)\n'
    )
    # Replace any auth function
    for fn_name in ['get_drive_service', 'get_service']:
        pat = rf'def {fn_name}\(.*?(?=\ndef |\nclass )'
        if re.search(pat, c, re.DOTALL):
            c = re.sub(pat, ADC, c, flags=re.DOTALL, count=1)
            c = c.replace(f'{fn_name}()', 'get_service()')
    # Remove service account import
    c = c.replace('from google.oauth2 import service_account\n', '')
    with open('rebuild_dashboard.py', 'w', encoding='utf-8') as f:
        f.write(c)
    print('Auth patched to ADC OK')
else:
    print('Auth already ADC - no patch needed')