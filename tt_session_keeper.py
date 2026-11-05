# --- AFTER (PATCHED CODE | File: tt_session_keeper.py | Lines 1 to 210) ---
# [MODULE/CLASS SSOT ROLE]: Master Tradetron Collector Session Keeper (`tt_session_keeper.py`).
# [FUNCTION CONTRACT & MATH]: Authenticates into the Master Tradetron Account via Playwright (solving Altcha), persists fresh `tradetron_session` and `XSRF-TOKEN` cookies into `public.tt_collector_session`, and claims any `PENDING_CLAIM` Shared Broker Codes for active `AUTH_NODE` subscriptions.

import os
import re
import json
import asyncio
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client, Client
from playwright.async_api import async_playwright

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TT_CREATOR_EMAIL = os.environ.get("TT_CREATOR_EMAIL")
TT_CREATOR_PASSWORD = os.environ.get("TT_CREATOR_PASSWORD")

UUID_REGEX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
REGEN_URL_REGEX = re.compile(
    r"https://tradetron\.tech/user/broker-and-exchanges/regenerate-token/shared-access/\d+/\d+",
    re.IGNORECASE
)


# [FUNCTION CONTRACT & MATH]: Synchronous stdout logger with immediate flush for GitHub Actions console visibility.
def log(msg: str) -> None:
    # [TECHNICAL]: Print timestamped log line with `flush=True`.
    # [TRADING DOMAIN]: Provides real-time audit logs inside the GitHub Actions workflow run.
    print(f"[SESSION-KEEPER] {msg}", flush=True)


# [FUNCTION CONTRACT & MATH]: Claims a pending `shared_broker_code` on Tradetron and resolves its exact `tt_regen_oneclick_url` using the authenticated HTTP cookie header.
def claim_and_resolve_url_via_http(cookie_header: str, xsrf_token: str, shared_code: str) -> str:
    # [TECHNICAL]: Send HTTP POST to `/user/manage/access/add/code` followed by `/user/manage/access/get/brokers/shared-with-me` and modal inspection.
    # [TRADING DOMAIN]: Links a subscriber's Shared Broker Code into the Master Tradetron Account and extracts the `/regenerate-token/shared-access/{broker_id}/{tt_user_id}` URL.
    headers = {
        "Cookie": cookie_header,
        "X-XSRF-TOKEN": xsrf_token,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    add_req = urllib.request.Request(
        "https://tradetron.tech/user/manage/access/add/code",
        data=json.dumps({"access_code": shared_code}).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    with urllib.request.urlopen(add_req, timeout=30) as resp:
        add_body = resp.read().decode("utf-8", errors="ignore")
        log(f"Add Code Response for {shared_code[:8]}...: HTTP {resp.status}")

    list_req = urllib.request.Request(
        "https://tradetron.tech/user/manage/access/get/brokers/shared-with-me",
        data=json.dumps({}).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    with urllib.request.urlopen(list_req, timeout=30) as resp:
        list_json = json.loads(resp.read().decode("utf-8", errors="ignore"))
        table_html = list_json.get("data", "")

    access_ids = re.findall(r'tokenRegen[^>]*data-access="(\d+)"', table_html, re.IGNORECASE)
    if not access_ids:
        return "CLAIMED_SHARED_CODE"

    latest_access_id = access_ids[-1]
    for modal_url in [
        f"https://tradetron.tech/user/manage/access/regenerate-token/modal/{latest_access_id}",
        f"https://tradetron.tech/user/manage/access/modal?id={latest_access_id}"
    ]:
        try:
            m_req = urllib.request.Request(modal_url, headers=headers, method="GET")
            with urllib.request.urlopen(m_req, timeout=20) as m_resp:
                m_text = m_resp.read().decode("utf-8", errors="ignore").replace("\\/", "/")
                match = REGEN_URL_REGEX.search(m_text)
                if match:
                    return match.group(0)
        except Exception:
            continue

    return "CLAIMED_SHARED_CODE"


# [FUNCTION CONTRACT & MATH]: Main async routine that logs into Tradetron via Playwright, saves fresh cookies into `tt_collector_session`, and resolves any pending 1-click broker codes.
async def run_session_keeper() -> None:
    # [TECHNICAL]: Validate required environment variables before launching Playwright or Supabase client.
    # [TRADING DOMAIN]: Prevents silent execution failures if creator credentials or Supabase keys are missing.
    if not SUPABASE_URL or not SUPABASE_KEY or not TT_CREATOR_EMAIL or not TT_CREATOR_PASSWORD:
        raise RuntimeError("Missing required environment variables: SUPABASE_URL, SUPABASE_KEY, TT_CREATOR_EMAIL, or TT_CREATOR_PASSWORD.")

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    async with async_playwright() as p:
        # [TECHNICAL]: Launch headless Chromium and authenticate using the verified 4-stage Tradetron login flow from `auto_approve_tt.py`.
        # [TRADING DOMAIN]: Solves the Altcha widget on `https://tradetron.tech/login` to obtain a fresh authenticated session for the Master Collector account.
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()

        log(f"Logging into Master Tradetron Account: {TT_CREATOR_EMAIL}")
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
            log("Solving Altcha Widget...")
            await altcha.locator(".altcha-checkbox").click()
            await altcha.locator("text=Verified").wait_for(state="visible", timeout=30000)

        await page.locator('button:has-text("Sign in"), button:has-text("Sign In")').first.click()
        await page.wait_for_load_state("networkidle", timeout=60000)

        # Navigate to Manage Shared Access to verify session and refresh XSRF-TOKEN cookie
        await page.goto("https://tradetron.tech/user/manage/access", wait_until="networkidle", timeout=60000)
        if "login" in page.url.lower():
            raise RuntimeError("Tradetron login failed: redirected back to /login.")

        # [TECHNICAL]: Extract all cookies for `tradetron.tech`, construct the Cookie header string, and decode `XSRF-TOKEN`.
        # [TRADING DOMAIN]: Persists the active session into `tt_collector_session` so Cloudflare (`vault.js`) has uninterrupted 24/7 access.
        cookies_list = await context.cookies("https://tradetron.tech")
        cookie_pairs = []
        xsrf_decoded = ""

        for c in cookies_list:
            name = c.get("name", "")
            val = c.get("value", "")
            if name and val:
                cookie_pairs.append(f"{name}={val}")
                if name == "XSRF-TOKEN":
                    xsrf_decoded = urllib.parse.unquote(val)

        cookie_header_str = "; ".join(cookie_pairs)
        if not cookie_header_str or "tradetron_session=" not in cookie_header_str:
            raise RuntimeError("Failed to extract valid tradetron_session cookie after login.")

        now_iso = datetime.now(timezone.utc).isoformat()
        sb.table("tt_collector_session").upsert({
            "collector_key": "MASTER_COLLECTOR",
            "tradetron_cookies": cookie_header_str,
            "xsrf_token": xsrf_decoded,
            "session_status": "ACTIVE",
            "updated_at": now_iso
        }).execute()
        log("Successfully updated MASTER_COLLECTOR session cookies in tt_collector_session.")

        # [TECHNICAL]: Check both `subscriber_vault` and `subscriber_vault_dev` for active `AUTH_NODE` rows belonging to `ONE_CLICK_REFRESH` brokers whose `shared_broker_code` has not yet been resolved to a `/regenerate-token/shared-access/` URL.
        # [TRADING DOMAIN]: Ensures any Shared Broker Code submitted while cookies were refreshing is automatically claimed and linked.
        env_pairs = [
            ("subscriber_vault", "broker_registry"),
            ("subscriber_vault_dev", "broker_registry_dev")
        ]

        for vault_table, reg_table in env_pairs:
            reg_res = sb.table(reg_table).select("broker_name").eq("is_active", True).eq("auth_mode", "ONE_CLICK_REFRESH").execute()
            oneclick_brokers = [r["broker_name"] for r in (reg_res.data or []) if r.get("broker_name")]
            if not oneclick_brokers:
                continue

            v_res = (
                sb.table(vault_table)
                .select("id, user_id, broker_name, shared_broker_code, tt_regen_oneclick_url")
                .eq("product_code", "AUTH_NODE")
                .eq("status", "active")
                .in_("broker_name", oneclick_brokers)
                .execute()
            )

            for row in (v_res.data or []):
                s_code = (row.get("shared_broker_code") or "").strip()
                cur_url = (row.get("tt_regen_oneclick_url") or "").strip()
                if UUID_REGEX.match(s_code) and "/regenerate-token/shared-access/" not in cur_url:
                    log(f"Auto-claiming pending 1-click code for {row.get('user_id')} ({row.get('broker_name')}) in {vault_table}...")
                    try:
                        resolved_url = claim_and_resolve_url_via_http(cookie_header_str, xsrf_decoded, s_code)
                        if "/regenerate-token/shared-access/" not in resolved_url:
                            # Fallback: inspect the page DOM directly via Playwright to grab the modal link
                            await page.goto("https://tradetron.tech/user/manage/access", wait_until="networkidle", timeout=60000)
                            regen_btn = page.locator("button.tokenRegen").last
                            if await regen_btn.is_visible(timeout=5000):
                                await regen_btn.click()
                                modal_link = page.locator('a[href*="/regenerate-token/shared-access/"]').first
                                await modal_link.wait_for(state="visible", timeout=10000)
                                href_val = await modal_link.get_attribute("href")
                                if href_val:
                                    resolved_url = href_val

                        sb.table(vault_table).update({
                            "tt_regen_oneclick_url": resolved_url,
                            "updated_at": datetime.now(timezone.utc).isoformat()
                        }).eq("id", row["id"]).execute()
                        log(f"Resolved tt_regen_oneclick_url for {row.get('user_id')}: {resolved_url}")
                    except Exception as claim_err:
                        log(f"Warning: Could not auto-claim {row.get('user_id')}: {claim_err}")

        await context.close()
        await browser.close()
        log("Session Keeper run completed successfully.")


if __name__ == "__main__":
    asyncio.run(run_session_keeper())
