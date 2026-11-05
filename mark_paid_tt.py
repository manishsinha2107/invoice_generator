# --- AFTER (PATCHED CODE | File: mark_paid_tt.py | Lines 1 to 198) ---
# [MODULE/CLASS SSOT ROLE]: Standalone Playwright Batch Invoice Settlement Synchronizer (`mark_paid_tt.py`) for Creator Tradetron Account.
# [FUNCTION CONTRACT & MATH]: Queries `strategy_ledger` (or `strategy_ledger_dev`) for rows with `payment_status = 'Paid'` and `tt_sync_status = 'Pending'`, batch-selects `#invoice_<external_invoice_id>` on `https://tradetron.tech/user/my-subscribers/invoicing`, verifies modal `invoiceIds[]`, submits `Mark As Paid`, updates `tt_sync_status = 'TT_Marked_Paid'`, and dispatches an Admin Telegram audit log.

import os
import asyncio
from datetime import datetime, timezone
import urllib.request
import json
from dotenv import load_dotenv
from supabase import create_client, Client
from playwright.async_api import async_playwright

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TT_CREATOR_EMAIL = os.environ.get("TT_CREATOR_EMAIL")
TT_CREATOR_PASSWORD = os.environ.get("TT_CREATOR_PASSWORD")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "production")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_CHANNEL_ID = os.environ.get("ADMIN_CHANNEL_ID")


# [FUNCTION CONTRACT & MATH]: Synchronous stdout logger with immediate flush for console/GitHub Actions visibility.
def log(msg: str) -> None:
    # [TECHNICAL]: Print timestamped log line with `flush=True`.
    # [BUSINESS / DOMAIN LOGIC]: Provides real-time execution audit logs during batch invoice settlement on Tradetron.
    print(f"DEBUG: {msg}", flush=True)


# [FUNCTION CONTRACT & MATH]: Dispatches an HTML-formatted summary alert to `ADMIN_CHANNEL_ID` via Telegram Bot API.
def send_admin_telegram(html_text: str) -> None:
    # [TECHNICAL]: Construct Telegram `sendMessage` POST request using `urllib.request.urlopen`.
    # [BUSINESS / DOMAIN LOGIC]: Notifies the Admin Telegram channel whenever a batch of paid invoices is marked Paid on Tradetron.
    if not TELEGRAM_BOT_TOKEN or not ADMIN_CHANNEL_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": ADMIN_CHANNEL_ID,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"📣 Admin Telegram HTTP {resp.status}")
    except Exception as exc:
        log(f"⚠️ Admin Telegram notification failed: {exc}")


# [FUNCTION CONTRACT & MATH]: Main async Playwright routine processing all `payment_status = 'Paid'` and `tt_sync_status = 'Pending'` rows in `strategy_ledger`.
async def run_mark_paid() -> None:
    # [TECHNICAL]: Validate required environment secrets before initializing Supabase or Chromium.
    # [BUSINESS / DOMAIN LOGIC]: Prevents partial execution if creator credentials or database keys are missing.
    if not SUPABASE_URL or not SUPABASE_KEY or not TT_CREATOR_EMAIL or not TT_CREATOR_PASSWORD:
        raise RuntimeError("Missing required environment variables: SUPABASE_URL, SUPABASE_KEY, TT_CREATOR_EMAIL, or TT_CREATOR_PASSWORD.")

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    ledger_table = "strategy_ledger" if ENVIRONMENT == "production" else "strategy_ledger_dev"
    env_prefix = "" if ENVIRONMENT == "production" else "[DEV] "

    # [TECHNICAL]: Query `ledger_table` for rows where `payment_status = 'Paid'`, `tt_sync_status = 'Pending'`, and `external_invoice_id` is not null.
    # [BUSINESS / DOMAIN LOGIC]: Retrieves only settled Tradetron invoices that have not yet been marked as Paid on the Tradetron Creator Portal.
    res = (
        sb.table(ledger_table)
        .select("user_email, strategy_id, invoice_batch_id, external_invoice_id, tt_profile_name, strategy_name, final_inr_amount")
        .eq("payment_status", "Paid")
        .eq("tt_sync_status", "Pending")
        .not_.is_("external_invoice_id", "null")
        .execute()
    )
    raw_rows = res.data or []
    pending_rows = [
        r for r in raw_rows
        if r.get("external_invoice_id") and str(r["external_invoice_id"]).strip().upper() != "NA"
    ]

    if not pending_rows:
        log(f"🏁 No paid invoices with tt_sync_status = 'Pending' found in {ledger_table}.")
        return

    target_by_ext_id = {}
    for row in pending_rows:
        ext_id = str(row["external_invoice_id"]).strip()
        target_by_ext_id[ext_id] = row

    log(f"🚀 Found {len(target_by_ext_id)} paid Tradetron invoice(s) to mark as Paid: {list(target_by_ext_id.keys())}")

    async with async_playwright() as p:
        # [TECHNICAL]: Launch headless Chromium and authenticate using the 4-stage Tradetron login sequence.
        # [BUSINESS / DOMAIN LOGIC]: Establishes an authenticated creator session on Tradetron to access `/user/my-subscribers/invoicing`.
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        log(f"🔑 Logging into Creator Tradetron Account: {TT_CREATOR_EMAIL}")
        await page.goto("https://tradetron.tech/login", wait_until="load", timeout=90000)

        classic_theme_btn = page.locator('button.tt-wel__btn--ghost:has-text("Use the classic theme")')
        if await classic_theme_btn.is_visible(timeout=4000):
            await classic_theme_btn.click()
            await page.wait_for_load_state("networkidle")

        lang_nudge = page.locator('a.tt-lang-nudge__no:has-text("Continue in English")')
        if await lang_nudge.is_visible(timeout=2000):
            await lang_nudge.click()

        email_input = page.locator('input[name="email"], input[type="email"]').first
        password_input = page.locator('input[name="password"], input[type="password"]').first
        await email_input.fill(TT_CREATOR_EMAIL, timeout=30000)
        await password_input.fill(TT_CREATOR_PASSWORD, timeout=30000)

        altcha = page.locator("altcha-widget").first
        if await altcha.is_visible(timeout=5000):
            log("🛡️ Solving Altcha Widget...")
            await altcha.locator(".altcha-checkbox").click()
            await altcha.locator("text=Verified").wait_for(state="visible", timeout=30000)

        await page.locator('button:has-text("Sign in"), button:has-text("Sign In")').first.click()
        await page.wait_for_load_state("networkidle", timeout=60000)

        # [TECHNICAL]: Navigate to `/user/my-subscribers/invoicing`, set DataTables length to 100, and wait for `#my-subscribers-invoicing_processing` to hide.
        # [BUSINESS / DOMAIN LOGIC]: Loads up to 100 recent invoices on a single page so checkboxes can be batch-selected without relying on Tradetron's broken search bar.
        log("📄 Navigating to Tradetron Creator Invoicing page...")
        await page.goto("https://tradetron.tech/user/my-subscribers/invoicing", wait_until="load", timeout=60000)
        await page.locator("table#my-subscribers-invoicing").wait_for(state="visible", timeout=30000)

        length_select = page.locator('select[name="my-subscribers-invoicing_length"]')
        if await length_select.is_visible(timeout=5000):
            await length_select.select_option("100")
            processing_spinner = page.locator("#my-subscribers-invoicing_processing")
            if await processing_spinner.is_visible(timeout=2000):
                await processing_spinner.wait_for(state="hidden", timeout=30000)
            await page.wait_for_timeout(1500)

        checked_ext_ids = []
        already_paid_ext_ids = []
        not_found_ext_ids = []

        # [TECHNICAL]: Locate each `input.invoiceCheckBox#invoice_<ext_id>`, verify row status cell (`td:last-child`), and check the box if status is `Pending`.
        # [BUSINESS / DOMAIN LOGIC]: Deterministically matches each paid invoice by its unique 7-digit Tradetron Invoice ID (`#invoice_3389038`).
        for ext_id, row_data in target_by_ext_id.items():
            checkbox = page.locator(f"input.invoiceCheckBox#invoice_{ext_id}")
            if await checkbox.count() == 0:
                log(f"⚠️ Invoice #{ext_id} ({row_data.get('tt_profile_name')}) not found on current Invoicing table view.")
                not_found_ext_ids.append(ext_id)
                continue

            tr_row = page.locator(f"table#my-subscribers-invoicing tbody tr:has(input#invoice_{ext_id})").first
            status_text = (await tr_row.locator("td").last.inner_text()).strip()

            if status_text.lower() == "paid":
                log(f"ℹ️ Invoice #{ext_id} ({row_data.get('tt_profile_name')}) is already marked Paid on Tradetron.")
                already_paid_ext_ids.append(ext_id)
            elif status_text.lower() == "pending":
                await checkbox.check()
                checked_ext_ids.append(ext_id)
                log(f"☑️ Checked Invoice #{ext_id} for {row_data.get('tt_profile_name')} ({row_data.get('strategy_name')})")
            else:
                log(f"⚠️ Unexpected status '{status_text}' for Invoice #{ext_id}. Skipping.")

        # [TECHNICAL]: If at least one checkbox was checked, select `mark` in `#actionType`, click `#actionButtonInvoicing`, verify modal `invoiceIds[]`, and submit.
        # [BUSINESS / DOMAIN LOGIC]: Executes the two-step Tradetron "Mark As Paid" modal confirmation with a strict pre-submit assertion that only our target invoice IDs are selected.
        if checked_ext_ids:
            log(f"🎯 Submitting 'Mark As Paid' action for {len(checked_ext_ids)} invoice(s): {checked_ext_ids}")
            await page.locator("select#actionType").select_option("mark")
            await page.locator("button#actionButtonInvoicing").click()

            modal_form = page.locator('#dynamicContentForActionModal form[action$="/invoicing/action/mark/paid"]')
            await modal_form.wait_for(state="visible", timeout=30000)

            modal_checked_inputs = modal_form.locator('input[name="invoiceIds[]"]:checked')
            modal_count = await modal_checked_inputs.count()
            modal_ids = []
            for idx in range(modal_count):
                val = await modal_checked_inputs.nth(idx).get_attribute("value")
                if val:
                    modal_ids.append(val.strip())

            if set(modal_ids) != set(checked_ext_ids):
                raise RuntimeError(
                    f"SAFETY HALT: Modal invoiceIds[] {modal_ids} do not match expected checked_ext_ids {checked_ext_ids}!"
                )

            confirm_btn = modal_form.locator('button[type="submit"]:has-text("Mark As Paid")').first
            await confirm_btn.wait_for(state="visible", timeout=15000)
            await confirm_btn.click()

            await page.wait_for_load_state("networkidle", timeout=60000)
            log(f"✅ Successfully marked {len(checked_ext_ids)} invoice(s) as Paid on Tradetron!")

        await context.close()
        await browser.close()

    # [TECHNICAL]: Update `tt_sync_status = 'TT_Marked_Paid'` in `ledger_table` for all `checked_ext_ids` and `already_paid_ext_ids`.
    # [BUSINESS / DOMAIN LOGIC]: Marks the invoices settled in Supabase so subsequent runs do not re-process them.
    synced_ids = checked_ext_ids + already_paid_ext_ids
    now_utc = datetime.now(timezone.utc).isoformat()
    for ext_id in synced_ids:
        sb.table(ledger_table).update({
            "tt_sync_status": "TT_Marked_Paid",
            "last_updated_at": now_utc
        }).eq("external_invoice_id", ext_id).eq("payment_status", "Paid").execute()

    if synced_ids or not_found_ext_ids:
        lines = [f"✅ <b>{env_prefix}ADMIN LOG: TRADETRON INVOICES MARKED PAID</b>\n━━━━━━━━━━━━━━"]
        for ext_id in checked_ext_ids:
            r = target_by_ext_id[ext_id]
            lines.append(f"• <code>{ext_id}</code> | <b>{r.get('tt_profile_name')}</b> | ₹{r.get('final_inr_amount')} (Marked Paid)")
        for ext_id in already_paid_ext_ids:
            r = target_by_ext_id[ext_id]
            lines.append(f"• <code>{ext_id}</code> | <b>{r.get('tt_profile_name')}</b> (Already Paid on TT)")
        for ext_id in not_found_ext_ids:
            r = target_by_ext_id[ext_id]
            lines.append(f"⚠️ <code>{ext_id}</code> | <b>{r.get('tt_profile_name')}</b> (Not Found on Table)")
        send_admin_telegram("\n".join(lines))

    log("✨ Tradetron Mark-As-Paid synchronization complete!")


if __name__ == "__main__":
    asyncio.run(run_mark_paid())
