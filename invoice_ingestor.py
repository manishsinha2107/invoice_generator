# --- AFTER (PATCHED CODE | File: scripts/2_invoice_ingestor.py | Lines 1 to 216) ---
# [MODULE/CLASS SSOT ROLE]: Autonomous Invoice Extractor & Ledger Injector
# [FUNCTION CONTRACT & MATH]: Parses PDFs, queries platform taxes, writes to ledger with dynamic environment routing, and dispatches alerts.

import os
import sys
import subprocess
import re
import json
import io
import platform
import hashlib
from datetime import datetime

def install_if_missing():
    REQS = ["requests", "google-auth", "supabase", "pdfplumber"]
    try:
        import requests, google.oauth2, supabase, pdfplumber
    except ImportError:
        print("📦 Missing libraries detected. Installing self-dependencies...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *REQS])

install_if_missing()

import requests
from google.oauth2 import service_account
import google.auth.transport.requests
from supabase import create_client, Client
import pdfplumber

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SOURCE_FOLDER = os.environ.get("INVOICE_SOURCE_FOLDER")
DEST_FOLDER = os.environ.get("INVOICE_DEST_FOLDER")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# [TECHNICAL]: SSOT table name resolver based on environment flag.
# [TRADING DOMAIN]: Ensures test PDFs parse to dev tables, and production PDFs route to live ledgers securely.
def get_table(base_name):
    if ENVIRONMENT == "production": return base_name
    # Exclude tables that don't have dev counterparts
    if base_name in ["strategies", "platform_profile", "user_registry"]: return base_name
    return f"{base_name}_dev"

# [MODULE/CLASS SSOT ROLE]: Authentication Gateway
# [FUNCTION CONTRACT & MATH]: Parses GitHub Secrets JSON and reconstructs RSA private keys for secure Google Drive API handshakes.
def get_drive_token():
    # [TECHNICAL]: Loads the raw JSON credential string from the environment into a dictionary.
    # [TRADING DOMAIN]: Extracts the Google Cloud Identity needed to securely access the private invoice folders.
    SERVICE_ACCOUNT_INFO = json.loads(os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON"))
    
    # [TECHNICAL]: Replaces escaped newline string literals (\\n) with structural line breaks (\n).
    # [TRADING DOMAIN]: Fixes the PEM formatting corruption caused by GitHub Secrets, allowing the cryptography library to parse the key.
    if "private_key" in SERVICE_ACCOUNT_INFO:
        SERVICE_ACCOUNT_INFO["private_key"] = SERVICE_ACCOUNT_INFO["private_key"].replace('\\n', '\n')
        
    # [TECHNICAL]: Initializes the credential scope and refreshes the ephemeral OAuth token.
    # [TRADING DOMAIN]: Finalizes the secure connection handshake before polling for PDFs.
    SCOPES = ['https://www.googleapis.com/auth/drive']
    creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
    auth_request = google.auth.transport.requests.Request()
    creds.refresh(auth_request)
    return creds.token

def send_telegram(chat_id, text):
    if not chat_id or chat_id == 'NA': return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        return True
    except: return False

def send_email(to_email, subject, html_body):
    if not RESEND_API_KEY: return False
    try:
        requests.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                      json={"from": "Ensuring Success <support@ensuringsuccess.in>", "to": [to_email], "subject": subject, "html": html_body})
        return True
    except: return False

def run_invoice_ingestion():
    token = get_drive_token()
    headers = {"Authorization": f"Bearer {token}"}
    query = f"'{SOURCE_FOLDER}' in parents and trashed = false"
    files = requests.get(f"https://www.googleapis.com/drive/v3/files?q={query}&fields=files(id,name)", headers=headers).json().get('files', [])
    
    if not files:
        print("✅ Idle: No new invoices found.")
        return

    # Cache configurations
    pf_res = supabase.table("platform_profile").select("cgst_pct,sgst_pct,due_days_count").eq("id", 1).execute()
    pf_data = pf_res.data[0] if pf_res.data else {}
    cgst_pct = float(pf_data.get("cgst_pct", 0.09))
    sgst_pct = float(pf_data.get("sgst_pct", 0.09))
    due_days = int(pf_data.get("due_days_count", 2))

    strat_res = supabase.table("strategies").select("strategy_id,strategy_name,monthly_fees").eq("status", "Active").execute()
    strat_map = {s['strategy_name'].lower().strip(): s for s in strat_res.data}

    for idx, f in enumerate(files):
        file_id, file_name = f['id'], f['name']
        if not file_name.endswith('.pdf'): continue
        
        print(f"📄 Processing: {file_name}")
        file_res = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media", headers=headers)
        
        with pdfplumber.open(io.BytesIO(file_res.content)) as pdf:
            text = "\n".join([page.extract_text() for page in pdf.pages if page.extract_text()])

        # [TECHNICAL]: Robust regex extraction mapping exactly to Tradetron PDF formats.
        # [TRADING DOMAIN]: Automatically isolates the unique external identity of the bill and the raw HWM value.
        email_match = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', text)
        inv_match = re.search(r'Invoice no.\s*\|\s*(\d+)', text)
        hwm_match = re.search(r'High-water mark Rs\s*([\d.]+)', text)
        tot_match = re.search(r'Grand Total\s*Rs\s*([\d.]+)', text)
        strat_match = re.search(r"in\s+'(.*?)'\s+strategy", text)
        cycle_match = re.search(r'Variable Fee Invoice for\s*(.*?\d{4})', text)

        if not (email_match and inv_match and hwm_match and strat_match):
            print(f"❌ Extraction Failed for {file_name}. Skipping.")
            continue

        tt_email = email_match.group(1).lower()
        inv_no = inv_match.group(1)
        hwm_val = float(hwm_match.group(1))
        pdf_total = float(tot_match.group(1)) if tot_match else 0.0
        strat_name = strat_match.group(1).strip()
        cycle_str = cycle_match.group(1).strip() if cycle_match else "NA"
        
        target_strat = strat_map.get(strat_name.lower())
        if not target_strat:
            print(f"⚠️ Strategy '{strat_name}' not active in DB. Skipping.")
            continue

        fee_pct = float(target_strat['monthly_fees'])
        base_fee = hwm_val * fee_pct
        sgst = base_fee * sgst_pct
        cgst = base_fee * cgst_pct
        final_inr = round(base_fee + sgst + cgst)

        # [TECHNICAL]: Identifies Mapped vs Unmapped clients dynamically.
        # [TRADING DOMAIN]: Ensures bills correctly attach to Website accounts without generating duplicate fractured 'NA' rows.
        subs_table = get_table('tradetron_active_subscribers')
        id_res = supabase.table(subs_table).select("user_email,tt_profile_name").eq("tradetron_user_id", tt_email).execute()
        
        user_email = 'NA'
        tg_chat_id = 'NA'
        tg_username = None
        tt_profile = 'NA'

        if id_res.data:
            rec = id_res.data[0]
            tt_profile = rec.get('tt_profile_name', 'NA')
            if rec.get('user_email') and rec.get('user_email') != 'NA':
                user_email = rec['user_email']
                reg_res = supabase.table("user_registry").select("telegram_chat_id,telegram_username").eq("email", user_email).execute()
                if reg_res.data:
                    tg_chat_id = reg_res.data[0].get("telegram_chat_id", 'NA')
                    tg_username = reg_res.data[0].get("telegram_username")
        else:
            # Unmapped user logic: inject dummy vault tracking row
            vault_table = get_table('subscriber_vault')
            supabase.table(vault_table).upsert({
                "user_email": "NA", "product_code": "ALGO_SUBSCRIPTION", "broker_name": strat_name,
                "user_id": "NA", "password": "NA", "totp_key": "NA", "tradetron_password": "NA",
                "tradetron_user_id": tt_email, "tt_profile_name": "Unmapped", "status": "active"
            }, on_conflict="user_email,broker_name").execute()

        # Batch hash to group multiple PDFs from the same user/cycle into one web card while keeping rows distinct
        batch_hash_str = f"{tt_email}_{cycle_str}"
        batch_id = 'INV-' + hashlib.md5(batch_hash_str.encode()).hexdigest()[:8].upper()
        
        ledger_payload = {
            "user_email": user_email, "tt_email_id": tt_email, "tt_profile_name": tt_profile,
            "telegram_username": tg_username, "telegram_chat_id": tg_chat_id,
            "strategy_name": strat_name, "strategy_id": target_strat['strategy_id'],
            "invoice_batch_id": batch_id, "external_invoice_id": inv_no,
            "billing_month": cycle_str[:3].upper() if cycle_str != "NA" else "NA",
            "billing_year": cycle_str[-4:] if cycle_str != "NA" else "NA",
            "high_water_mark": hwm_val, "cycle_profit": hwm_val,
            "fee_percentage": fee_pct, "base_fee": base_fee,
            "sgst_amount": sgst, "cgst_amount": cgst,
            "final_inr_amount": final_inr, "payment_status": "Unpaid", "invoice_status": "Sent",
            "invoice_generated_at": datetime.now().isoformat()
        }

        # [TECHNICAL]: Write isolated row with exact Tradetron Invoice ID.
        ledger_table = get_table('strategy_ledger')
        ins_res = supabase.table(ledger_table).upsert(ledger_payload, on_conflict="invoice_batch_id,external_invoice_id").execute()
        
        # [TECHNICAL]: Notifications engine
        dashboard_url = "https://ensuringsuccess.in/algo#billing" if ENVIRONMENT == "production" else "https://dev.ensuringsuccess.in/algo#billing"
        prefix = "" if ENVIRONMENT == "production" else "[DEV] "
        amt_fmt = f"₹{final_inr:,.2f}"

        tg_msg = f"🧾 <b>{prefix}Invoice Generated</b>\n━━━━━━━━━━━━━━\n👤 <b>Profile:</b> {tt_profile}\n📧 <b>Email:</b> {tt_email}\n📦 <b>Batch:</b> <code>{batch_id}</code>\n🔖 <b>Ext ID:</b> <code>{inv_no}</code>\n🔄 <b>Cycle:</b> {cycle_str}\n🎯 <b>Strategy:</b> {strat_name}\n\n<b>Calculation Breakdown:</b>\n• HWM Profit: ₹{hwm_val}\n• Base Fee ({(fee_pct*100):.1f}%): ₹{base_fee:.2f}\n• GST: ₹{(sgst+cgst):.2f}\n\n💰 <b>Total Due:</b> {amt_fmt}\n\n<a href='{dashboard_url}'>💳 Settle Securely via Dashboard</a>"
        
        if not send_telegram(tg_chat_id, tg_msg):
            send_email(user_email if user_email != 'NA' else tt_email, f"{prefix}Action Required: Invoice {inv_no} Generated", tg_msg.replace('\n', '<br>'))

        # Move file out of source directory
        print(f"✅ Ingested Invoice {inv_no}. Moving file.")
        requests.patch(f"https://www.googleapis.com/drive/v3/files/{file_id}?addParents={DEST_FOLDER}&removeParents={SOURCE_FOLDER}", headers=headers)

if __name__ == "__main__":
    run_invoice_ingestion()
