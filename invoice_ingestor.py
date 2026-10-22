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
from datetime import datetime, timedelta

# [TECHNICAL]: Self-installing dependencies block.
# [TRADING DOMAIN]: Ensures GitHub Actions runners automatically acquire required parsing and database libraries.
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

# [TECHNICAL]: Global integrity check to mirror your reference script exactly.
# [TRADING DOMAIN]: Prevents execution if critical routing variables are missing.
if not all([SUPABASE_URL, SUPABASE_KEY, SOURCE_FOLDER, DEST_FOLDER]):
    print("❌ ERROR: Missing Environment Variables")
    sys.exit(1)

# [TECHNICAL]: Global JSON parsing and Scope declaration, matching the exact structure of scripts/1_trade_ingestor.py.
# [TRADING DOMAIN]: Eliminates parsing anomalies by deferring cleanly to the google-auth library's native handlers.
SERVICE_ACCOUNT_INFO = json.loads(os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON"))
SCOPES = ['https://www.googleapis.com/auth/drive']

# [FUNCTION CONTRACT & MATH]: Environment Table Router
def get_table(base_name):
    # [TECHNICAL]: Checks the ENVIRONMENT flag to append _dev suffixes dynamically.
    # [TRADING DOMAIN]: Ensures test PDFs parse to dev tables, and production PDFs route to live ledgers securely.
    if ENVIRONMENT == "production": return base_name
    if base_name in ["strategies", "platform_profile", "user_registry"]: return base_name
    return f"{base_name}_dev"

# [FUNCTION CONTRACT & MATH]: Authentication Gateway
def get_drive_token():
    # [TECHNICAL]: Instantiates OAuth token using globally parsed JSON credentials.
    # [TRADING DOMAIN]: Finalizes the secure connection handshake before polling for PDFs.
    creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
    auth_request = google.auth.transport.requests.Request()
    creds.refresh(auth_request)
    return creds.token

# [FUNCTION CONTRACT & MATH]: Telegram Dispatcher
# --- AFTER (PATCHED CODE | File: invoice_ingestor.py | Lines 53 to 64) ---
# [FUNCTION CONTRACT & MATH]: Telegram Dispatcher
def send_telegram(chat_id, text, reply_markup=None):
    # [TECHNICAL]: Validates HTTP status code from Telegram Bot API and supports optional inline keyboards.
    # [TRADING DOMAIN]: Ensures failed Telegram alerts accurately trigger the Resend email fallback so clients never miss an invoice.
    if not chat_id or chat_id == 'NA' or not TELEGRAM_BOT_TOKEN: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = requests.post(url, json=payload)
    if not res.ok:
        print(f"⚠️ Telegram API Error ({res.status_code}): {res.text}")
        return False
    return True

# [FUNCTION CONTRACT & MATH]: Email Dispatcher
def send_email(to_email, subject, html_body):
    # [TECHNICAL]: Validates HTTP status code from Resend API and logs explicit rejection reasons.
    # [TRADING DOMAIN]: Guarantees auditability in GitHub Actions logs if an unmapped user's invoice email fails to dispatch.
    if not RESEND_API_KEY or not to_email or to_email == 'NA': return False
    res = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": "Ensuring Success <support@ensuringsuccess.in>", "to": [to_email], "subject": subject, "html": html_body}
    )
    if not res.ok:
        print(f"⚠️ Resend API Error ({res.status_code}): {res.text}")
        return False
    return True

# [FUNCTION CONTRACT & MATH]: Core Processing Loop
def run_invoice_ingestion():
    token = get_drive_token()
    headers = {"Authorization": f"Bearer {token}"}
    query = f"'{SOURCE_FOLDER}' in parents and trashed = false"
    files = requests.get(f"https://www.googleapis.com/drive/v3/files?q={query}&fields=files(id,name)", headers=headers).json().get('files', [])
    
    if not files:
        print("✅ Idle: No new invoices found.")
        return

    # [TECHNICAL]: Retrieve global SSOT tax metrics from platform_profile with strict validation (zero hardcoded fallbacks).
    # [TRADING DOMAIN]: Enforces that tax rates and payment grace periods are strictly governed by the central database configuration.
    pf_res = supabase.table("platform_profile").select("cgst_pct,sgst_pct,due_days_count").eq("id", 1).execute()
    if not pf_res.data:
        raise RuntimeError("❌ CRITICAL: platform_profile (id=1) missing in database.")
    pf_data = pf_res.data[0]
    cgst_pct = float(pf_data["cgst_pct"])
    sgst_pct = float(pf_data["sgst_pct"])
    due_days = int(pf_data["due_days_count"])

    # [MODULE/CLASS SSOT ROLE]: Ledger Mapping Cache
    # [FUNCTION CONTRACT & MATH]: Pre-fetches strictly eligible (Live Auto, Active, Subscribed) strategies keyed by strategy_full_name.
    # [TECHNICAL]: Queries strategy_id, strategy_full_name, and monthly_fees for active live deployments.
    # [TRADING DOMAIN]: Enforces that only active, subscribed Live Auto strategies are eligible for automated invoice ingestion.
    strat_res = supabase.table("strategies").select("strategy_id,strategy_name,strategy_full_name,monthly_fees").eq("status", "Active").eq("subscription_status", "Active").eq("deployment_type", "Live Auto").execute()
    strat_map = {s['strategy_full_name'].lower().strip(): s for s in strat_res.data if s.get('strategy_full_name')}

    # [FUNCTION CONTRACT & MATH]: Flexible Date String Parser
    def parse_pdf_date(date_str):
        # [TECHNICAL]: Parses Tradetron date strings across full month (%B) or abbreviated month (%b) formats.
        # [TRADING DOMAIN]: Converts human-readable PDF cycle and issue dates into standard ISO dates for database storage.
        clean_d = re.sub(r'\s+', ' ', date_str.strip())
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(clean_d, fmt)
            except ValueError:
                continue
        raise ValueError(f"Unrecognized PDF date format: '{clean_d}'")

    for idx, f in enumerate(files):
        file_id, file_name = f['id'], f['name']
        if not file_name.endswith('.pdf'): continue
        
        print(f"📄 Processing: {file_name}")
        file_res = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media", headers=headers)
        
        with pdfplumber.open(io.BytesIO(file_res.content)) as pdf:
            text = "\n".join([page.extract_text() for page in pdf.pages if page.extract_text()])

        # [MODULE/CLASS SSOT ROLE]: Robust PDF Text Parser
        # [FUNCTION CONTRACT & MATH]: Extracts client identity, invoice ID, HWM, Grand Total, Strategy Full Name, Issue Date, and Cycle Range.
        # [TECHNICAL]: Uses multiline-safe regex patterns resilient to pdfplumber's spatial column merging.
        # [TRADING DOMAIN]: Extracts every mandatory billing parameter directly from the Tradetron PDF invoice.
        email_match = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', text)
        inv_match = re.search(r'Invoice\s*no\.?\s*(?:[:\|]\s*)?(\d+)', text, re.IGNORECASE)
        hwm_match = re.search(r'High[- ]water\s*mark\s*Rs\.?\s*([\d,.]+)', text, re.IGNORECASE)
        tot_match = re.search(r'Grand\s*Total\s*Rs\.?\s*([\d,.]+)', text, re.IGNORECASE)
        strat_match = re.search(r"'([^']*)'\s+strategy", text, re.IGNORECASE)
        cycle_match = re.search(r'Variable\s*Fee\s*Invoice\s*for\s*([0-9]{1,2}\s+[A-Za-z]+\s+\d{4}\s*-\s*[0-9]{1,2}\s+[A-Za-z]+\s+\d{4})', text, re.IGNORECASE)
        issue_match = re.search(r'Date\s*of\s*issue\s*(?:[:\|]\s*)?([0-9]{1,2}\s+[A-Za-z]+\s+\d{4})', text, re.IGNORECASE)
        profile_match = re.search(r'BILLED\s+TO(?:\s+INVOICE\s+DETAILS)?\s*\n\s*(.+?)(?:\s+Invoice\s*no\.?|\n)', text, re.IGNORECASE)

        if not (email_match and inv_match and hwm_match and strat_match and cycle_match and issue_match):
            print(f"❌ Extraction Failed for {file_name}. Skipping.")
            print(f"--- DEBUG EXTRACTED TEXT START ---\n{text}\n--- DEBUG EXTRACTED TEXT END ---")
            continue

        tt_email = email_match.group(1).lower().strip()
        inv_no = inv_match.group(1).strip()
        pdf_profile_name = profile_match.group(1).strip() if profile_match else None
        
        # [TECHNICAL]: Scrubs numeric strings of commas before casting to float.
        # [TRADING DOMAIN]: Prevents float-casting failures on high-water marks above Rs 999.
        hwm_val = float(hwm_match.group(1).replace(',', ''))
        pdf_total = float(tot_match.group(1).replace(',', '')) if tot_match else None
        
        # [TECHNICAL]: Scrubs any column-wrapped 'Rs XX.XX' fragments from the strategy name and normalizes whitespace.
        # [TRADING DOMAIN]: Reconstructs the exact strategy_full_name (e.g., 'Vedika Nifty Intraday Buying') to match the strategies table.
        raw_strat_name = strat_match.group(1)
        clean_strat_name = re.sub(r'Rs\.?\s*[\d,.]+', '', raw_strat_name, flags=re.IGNORECASE)
        strat_full_name = re.sub(r'\s+', ' ', clean_strat_name).strip()
        
        # [TECHNICAL]: Parses start date, end date, issue date, due date, full month name, and 4-digit year.
        # [TRADING DOMAIN]: Populates billing_start_date, billing_end_date, due_date, billing_month, and billing_year so algo.html and invoice_generator.html filters and cards work seamlessly.
        cycle_str = re.sub(r'\s+', ' ', cycle_match.group(1)).strip()
        start_raw, end_raw = [part.strip() for part in cycle_str.split('-', 1)]
        start_dt = parse_pdf_date(start_raw)
        end_dt = parse_pdf_date(end_raw)
        issue_dt = parse_pdf_date(issue_match.group(1))
        due_dt = issue_dt + timedelta(days=due_days)

        billing_start_date = start_dt.strftime("%Y-%m-%d")
        billing_end_date = end_dt.strftime("%Y-%m-%d")
        due_date_iso = due_dt.strftime("%Y-%m-%d")
        due_date_display = due_dt.strftime("%d/%m/%Y")
        billing_month = end_dt.strftime("%B")
        billing_year = end_dt.strftime("%Y")
        
        target_strat = strat_map.get(strat_full_name.lower())
        if not target_strat:
            print(f"⚠️ Strategy '{strat_full_name}' not active in DB. Skipping.")
            continue

        canonical_strat_full_name = target_strat['strategy_full_name'].strip()

        # [TECHNICAL]: Executes mathematical tax calculations using platform_profile rates.
        # [TRADING DOMAIN]: Calculates base performance fee, SGST, CGST, and rounded final INR total.
        fee_pct = float(target_strat['monthly_fees'])
        base_fee = hwm_val * fee_pct
        sgst = base_fee * sgst_pct
        cgst = base_fee * cgst_pct
        final_inr = round(base_fee + sgst + cgst)

        # [TECHNICAL]: Queries tradetron_active_subscribers for identity bridging without touching subscriber_vault.
        # [TRADING DOMAIN]: Links the invoice to the client's website user_email and Telegram chat ID if mapped, or keeps user_email='NA' until they claim it via OTP on algo.html.
        subs_table = get_table('tradetron_active_subscribers')
        id_res = supabase.table(subs_table).select("user_email,tt_profile_name").eq("tradetron_user_id", tt_email).execute()
        
        user_email = 'NA'
        tg_chat_id = 'NA'
        tg_username = None
        tt_profile = pdf_profile_name if pdf_profile_name else 'NA'

        if id_res.data:
            for rec in id_res.data:
                if rec.get('tt_profile_name') and rec.get('tt_profile_name') != 'NA':
                    tt_profile = rec['tt_profile_name']
                if rec.get('user_email') and rec.get('user_email') != 'NA':
                    user_email = rec['user_email']
                    break
            if user_email != 'NA':
                reg_res = supabase.table("user_registry").select("telegram_chat_id,telegram_username").eq("email", user_email).execute()
                if reg_res.data:
                    tg_chat_id = reg_res.data[0].get("telegram_chat_id") or 'NA'
                    tg_username = reg_res.data[0].get("telegram_username")

        # [TECHNICAL]: Deterministic batch hash per user and cycle string.
        # [TRADING DOMAIN]: Groups invoices from the same cycle while ensuring two different unmapped users ('NA') never collide on (user_email, strategy_id, invoice_batch_id).
        batch_hash_str = f"{tt_email}_{cycle_str}"
        batch_id = 'INV-' + hashlib.md5(batch_hash_str.encode()).hexdigest()[:8].upper()
        now_iso = datetime.utcnow().isoformat() + "Z"
        
        ledger_payload = {
            "user_email": user_email,
            "tt_email_id": tt_email,
            "tt_profile_name": tt_profile,
            "telegram_username": tg_username,
            "telegram_chat_id": tg_chat_id,
            "strategy_name": canonical_strat_full_name,
            "strategy_id": target_strat['strategy_id'],
            "invoice_batch_id": batch_id,
            "external_invoice_id": inv_no,
            "billing_start_date": billing_start_date,
            "billing_end_date": billing_end_date,
            "due_date": due_date_iso,
            "billing_month": billing_month,
            "billing_year": billing_year,
            "high_water_mark": hwm_val,
            "cycle_profit": hwm_val,
            "fee_percentage": fee_pct,
            "base_fee": base_fee,
            "sgst_amount": sgst,
            "cgst_amount": cgst,
            "final_inr_amount": final_inr,
            "payment_status": "Unpaid",
            "invoice_status": "Sent",
            "invoice_generated_at": now_iso,
            "invoice_sent_at": now_iso,
            "last_updated_at": now_iso
        }

        # [MODULE/CLASS SSOT ROLE]: Idempotent Ledger Upsert via Verified Schema Constraints
        # [FUNCTION CONTRACT & MATH]: Upserts on external_invoice_id (or updates existing row if re-ingested) while preserving Paid status.
        # [TECHNICAL]: Checks if external_invoice_id already exists; if already Paid, skips overwriting payment_status; otherwise upserts on the exact composite PK (user_email, strategy_id, invoice_batch_id).
        # [TRADING DOMAIN]: Guarantees idempotent PDF ingestion aligned with strategy_ledger_pkey and strategy_ledger_dev_pkey.
        ledger_table = get_table('strategy_ledger')
        existing_inv = supabase.table(ledger_table).select("external_invoice_id,payment_status").eq("external_invoice_id", inv_no).execute()
        if existing_inv.data and existing_inv.data[0].get("payment_status") == "Paid":
            print(f"ℹ️ Invoice {inv_no} is already marked Paid in {ledger_table}. Moving file without re-billing.")
            requests.patch(f"https://www.googleapis.com/drive/v3/files/{file_id}?addParents={DEST_FOLDER}&removeParents={SOURCE_FOLDER}", headers=headers)
            continue

        supabase.table(ledger_table).upsert(
            ledger_payload,
            on_conflict="user_email,strategy_id,invoice_batch_id"
        ).execute()
        
        # [TECHNICAL]: Decouples email and Telegram templates to support rich HTML button styling.
        # [TRADING DOMAIN]: Drives users to the auto-reconciling dashboard while providing strict warnings for the manual Razorpay fallback.
        dashboard_url = "https://ensuringsuccess.in/algo#billing" if ENVIRONMENT == "production" else "https://dev.ensuringsuccess.in/algo#billing"
        razorpay_url = "https://razorpay.me/@ensuringsucces"
        prefix = "" if ENVIRONMENT == "production" else "[DEV] "
        amt_fmt = f"₹{final_inr:,.2f}"

        tg_msg = (
            f"🧾 <b>{prefix}Invoice Generated</b>\n━━━━━━━━━━━━━━\n"
            f"👤 <b>Profile:</b> {tt_profile}\n"
            f"📧 <b>Email:</b> {tt_email}\n"
            f"📦 <b>Batch:</b> <code>{batch_id}</code>\n"
            f"🔖 <b>Tradetron Invoice No.:</b> <code>{inv_no}</code>\n"
            f"🔄 <b>Cycle:</b> {cycle_str}\n"
            f"🎯 <b>Strategy:</b> {canonical_strat_full_name}\n\n"
            f"<b>Calculation Breakdown:</b>\n"
            f"• HWM Profit: ₹{hwm_val:,.2f}\n"
            f"• Base Fee ({(fee_pct*100):.1f}%): ₹{base_fee:,.2f}\n"
            f"• GST: ₹{(sgst+cgst):,.2f}\n\n"
            f"💰 <b>Total Due:</b> {amt_fmt}\n"
            f"📅 <b>Due Date:</b> {due_date_display}\n\n"
            f"💡 <b>PREFERRED:</b> Pay directly via your billing dashboard for an instantly auto-reconciled, seamless experience.\n\n"
            f"⚠️ <b>USING DIRECT PAYMENT LINK?</b>\n"
            f"If paying via Razorpay directly, you MUST enter your Tradetron Invoice No. (<code>{inv_no}</code>) in the 'Add a note' field for payment reconciliation. Missing this ID will result in an untracked payment and risk the suspension of your Tradetron strategy access."
        )

        tg_markup = {
            "inline_keyboard": [
                [{"text": "💳 Settle Securely via Dashboard", "url": dashboard_url}],
                [{"text": "🔗 Pay via Direct Razorpay Link", "url": razorpay_url}]
            ]
        }
        
        em_html = f"""
        <div style="font-family:sans-serif; max-width:600px; border:1px solid #e2e8f0; border-radius:12px; padding:24px; color:#1e293b;">
            <h2 style="color:#0f172a; margin-top:0;">{prefix}Invoice Generated</h2>
            <table style="width:100%; font-size:14px; margin-bottom:20px;">
                <tr><td style="padding:4px 0; color:#64748b;">Profile:</td><td style="text-align:right; font-weight:bold;">{tt_profile}</td></tr>
                <tr><td style="padding:4px 0; color:#64748b;">Batch ID:</td><td style="text-align:right; font-family:monospace;">{batch_id}</td></tr>
                <tr><td style="padding:4px 0; color:#64748b;">Tradetron Invoice No.:</td><td style="text-align:right; font-family:monospace; color:#059669; font-weight:bold;">{inv_no}</td></tr>
                <tr><td style="padding:4px 0; color:#64748b;">Strategy:</td><td style="text-align:right;">{canonical_strat_full_name}</td></tr>
                <tr><td style="padding:4px 0; color:#64748b;">Total Due:</td><td style="text-align:right; font-weight:black; font-size:16px;">{amt_fmt}</td></tr>
                <tr><td style="padding:4px 0; color:#64748b;">Due Date:</td><td style="text-align:right; color:#e11d48; font-weight:bold;">{due_date_display}</td></tr>
            </table>
            
            <p style="font-size:13px; color:#0f172a; font-weight:bold; margin-bottom:10px;">💡 PREFERRED: Pay directly via your billing dashboard for an instantly auto-reconciled, seamless experience.</p>
            <a href="{dashboard_url}" style="display:block; text-align:center; background:#059669; color:#ffffff; padding:14px; text-decoration:none; border-radius:8px; font-weight:bold; font-size:14px;">💳 Settle Securely via Dashboard</a>
            
            <div style="background:#fffbeb; border:1px solid #fbbf24; padding:15px; border-radius:8px; margin-top:24px;">
                <p style="font-size:13px; color:#d97706; font-weight:bold; margin-top:0;">⚠️ USING DIRECT PAYMENT LINK?</p>
                <p style="font-size:12px; color:#92400e; margin-bottom:15px;">If paying via Razorpay directly, you MUST enter your Tradetron Invoice No. (<strong>{inv_no}</strong>) in the "Add a note" field for payment reconciliation. Missing this ID will result in an untracked payment and risk the suspension of your Tradetron strategy access.</p>
                <a href="{razorpay_url}" style="display:block; text-align:center; background:#ffffff; color:#64748b; border:1px solid #cbd5e1; padding:12px; text-decoration:none; border-radius:8px; font-weight:bold; font-size:13px;">🔗 Pay via Direct Razorpay Link</a>
            </div>
        </div>
        """

        if not send_telegram(tg_chat_id, tg_msg, tg_markup):
            target_recipient = user_email if user_email != 'NA' else tt_email
            send_email(target_recipient, f"{prefix}Action Required: Invoice {inv_no} Generated", em_html)

        # [TECHNICAL]: Moves processed PDF from SOURCE_FOLDER to DEST_FOLDER in Google Drive.
        # [TRADING DOMAIN]: Archives the ingested invoice so subsequent workflow runs do not re-process it.
        print(f"✅ Ingested Invoice {inv_no} ({canonical_strat_full_name}) -> Batch {batch_id}. Moving file.")
        requests.patch(f"https://www.googleapis.com/drive/v3/files/{file_id}?addParents={DEST_FOLDER}&removeParents={SOURCE_FOLDER}", headers=headers)

if __name__ == "__main__":
    run_invoice_ingestion()
