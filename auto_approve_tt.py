# [MODULE/CLASS SSOT ROLE]: Event-Driven Playwright Auto-Approver (`auto_approve_tt.py`) for Tradetron Strategy Subscription Requests.
# [FUNCTION CONTRACT & MATH]: Queries `tradetron_approval_queue` for rows with `status = 'READY_TO_APPROVE'`, logs into the Creator Tradetron account, submits the approval form at `approve_url`, marks rows `APPROVED`, and notifies `/notifier`.

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
NOTIFIER_URL = "https://ensuringsuccess.in/notifier"


# [FUNCTION CONTRACT & MATH]: Synchronous stdout logger with immediate flush for GitHub Actions console visibility.
def log(msg: str) -> None:
    # [TECHNICAL]: Print timestamped log line with `flush=True`.
    # [TRADING DOMAIN]: Provides real-time audit logs inside the GitHub Actions workflow run.
    print(f"DEBUG: {msg}", flush=True)


# [FUNCTION CONTRACT & MATH]: Dispatches HTTP POST payload to `https://ensuringsuccess.in/notifier` upon verified Tradetron approval.
def notify_approval(user_email: str, tt_email: str, tt_profile: str, strategy_name: str, ref_id: str, tg_chat_id: str, tg_username: str) -> None:
    # [TECHNICAL]: Construct JSON body for `SUBSCRIPTION_APPROVED` and send via `urllib.request.urlopen`.
    # [TRADING DOMAIN]: Sends instant approval confirmation to the subscriber and Admin Telegram channel.
    payload = {
        "action": "SUBSCRIPTION_APPROVED",
        "user": {
            "email": user_email or tt_email,
            "tt_email": tt_email,
            "telegram_chat_id": tg_chat_id or "NA",
            "telegram_username": tg_username
        },
        "data": {
            "tt_profile": tt_profile,
            "strategy_name": strategy_name,
            "ref_id": ref_id
        }
    }
    req = urllib.request.Request(
        NOTIFIER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "EnsuringSuccess-GitHub-Approver"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        log(f"📣 Notifier HTTP {resp.status}: {resp.read().decode('utf-8')}")


# [FUNCTION CONTRACT & MATH]: Main async Playwright routine processing all `READY_TO_APPROVE` rows in `tradetron_approval_queue`.
async def run_auto_approver() -> None:
    # [TECHNICAL]: Validate required environment secrets before initializing Supabase or Chromium.
    # [TRADING DOMAIN]: Prevents silent partial execution if creator credentials or database keys are missing.
    if not SUPABASE_URL or not SUPABASE_KEY or not TT_CREATOR_EMAIL or not TT_CREATOR_PASSWORD:
        raise RuntimeError("Missing required environment variables: SUPABASE_URL, SUPABASE_KEY, TT_CREATOR_EMAIL, or TT_CREATOR_PASSWORD.")

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # [TECHNICAL]: Fetch all rows in `tradetron_approval_queue` with `status = 'READY_TO_APPROVE'`.
    # [TRADING DOMAIN]: Retrieves only subscription requests where the user has completed website account linkage.
    res = sb.table("tradetron_approval_queue").select("*").eq("status", "READY_TO_APPROVE").execute()
    pending_rows = res.data or []

    if not pending_rows:
        log("🏁 No READY_TO_APPROVE requests found in tradetron_approval_queue.")
        return

    log(f"🚀 Found {len(pending_rows)} pending Tradetron subscription request(s) to approve.")

    async with async_playwright() as p:
        # [TECHNICAL]: Launch headless Chromium and authenticate using the proven 4-stage Tradetron login sequence.
        # [TRADING DOMAIN]: Establishes an authenticated creator session on Tradetron capable of submitting CSRF-protected approval forms.
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
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

        # [TECHNICAL]: Iterate through each `READY_TO_APPROVE` row, navigate to `approve_url`, and click the `<form data-sr-answer="approve"> <button type="submit">Approve request</button>`.
        # [TRADING DOMAIN]: Executes the exact POST form submission with `_token` CSRF protection and verifies redirection to `/user/my-subscribers`.
        for row in pending_rows:
            row_id = row["id"]
            approve_url = row["approve_url"]
            tt_email = row["tt_email"]
            tt_profile = row["tt_profile_name"]
            strat_name = row["strategy_name"]
            user_email = row["user_email"] or tt_email
            ref_id = row["ref_id"] or "VERIFIED"

            log(f"🎯 Navigating to approve URL for {tt_profile} ({tt_email}): {approve_url}")
            await page.goto(approve_url, wait_until="load", timeout=60000)

            approve_btn = page.locator('form[data-sr-answer="approve"] button[type="submit"], button:has-text("Approve request")').first
            await approve_btn.wait_for(state="visible", timeout=30000)
            await approve_btn.click()

            await page.wait_for_url("**/user/my-subscribers**", timeout=30000)
            log(f"✅ Approved on Tradetron: {tt_profile} ({tt_email}) -> {strat_name}")

            # [TECHNICAL]: Update `tradetron_approval_queue` row to `APPROVED` with UTC timestamp and query `user_registry` for Telegram routing details.
            # [TRADING DOMAIN]: Marks the subscription request settled in Supabase and triggers the user/admin confirmation notification.
            sb.table("tradetron_approval_queue").update({
                "status": "APPROVED",
                "approved_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", row_id).execute()

            reg_res = sb.table("user_registry").select("telegram_chat_id, telegram_username").eq("email", user_email).execute()
            reg_rows = reg_res.data or []
            tg_chat_id = reg_rows[0]["telegram_chat_id"] if reg_rows else "NA"
            tg_username = reg_rows[0]["telegram_username"] if reg_rows else None

            notify_approval(user_email, tt_email, tt_profile, strat_name, ref_id, tg_chat_id, tg_username)

        await context.close()
        await browser.close()
        log("✨ All pending Tradetron subscription requests approved successfully!")


if __name__ == "__main__":
    asyncio.run(run_auto_approver())
