#!/usr/bin/env python3
"""
scripts/daily_booking_playwright.py

Async Playwright-based booking orchestrator:
- reads booking rows from a Google Sheet (service account).
- creates N parallel browser contexts/pages.
- pre-fills booking data in each page.
- performs near-simultaneous submit across pages.

This script contains site-specific selector placeholders that MUST be updated
after inspecting the live facility page DOM. Test locally with DRY_RUN=true
and HEADLESS=false to tune selectors.
"""

import os
import asyncio
import json
import base64
import logging
from datetime import datetime
import pytz

from dotenv import load_dotenv

# Playwright async API
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# gspread for Google Sheets
import gspread
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()

# Config via env / GitHub secrets
GOOGLE_SA_JSON_B64 = os.getenv("GOOGLE_SA_JSON_B64")  # base64-encoded service account JSON
SHEET_ID = os.getenv("SHEET_ID")  # google sheet ID
SHEET_RANGE = os.getenv("SHEET_RANGE", None)  # optional: e.g. "Bookings!A:F"
FACILITY_URL = os.getenv("FACILITY_URL", "https://www.recreation.gov/ticket/facility/234635?tab=tours")
RECGOV_USERNAME = os.getenv("RECGOV_USERNAME")
RECGOV_PASSWORD = os.getenv("RECGOV_PASSWORD")

# Scheduling: facility timezone (IANA) and desired hour (0-23) local time
FACILITY_TZ = os.getenv("FACILITY_TZ", "America/Denver")
DESIRED_LOCAL_HOUR = int(os.getenv("DESIRED_LOCAL_HOUR", "15"))  # e.g. 15 for 3pm local
RUN_ONCE_PER_HOUR = True  # workflow runs hourly; script will check local hour and run only if match

# Parallelism
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "5"))  # 1..15 per your requirement

# Behavior flags
HEADLESS = os.getenv("HEADLESS", "false").lower() in ("1", "true", "yes")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("booking")

# Expected sheet columns (case-sensitive): first_name, last_name, email, phone, ticket_qty, date(optional), time(optional)
REQUIRED_COLUMNS = ("first_name", "last_name", "email", "phone", "ticket_qty")


def load_google_sheet():
    if not GOOGLE_SA_JSON_B64:
        raise RuntimeError("GOOGLE_SA_JSON_B64 env not set (base64-encoded service account JSON).")
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID env not set.")

    sa_json = base64.b64decode(GOOGLE_SA_JSON_B64).decode("utf-8")
    creds_dict = json.loads(sa_json)

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scopes=scope)
    gc = gspread.authorize(credentials)

    sh = gc.open_by_key(SHEET_ID)
    ws = sh.sheet1 if not SHEET_RANGE else sh.worksheet(SHEET_RANGE.split("!")[0])
    # Get all records (list of dicts)
    records = ws.get_all_records()
    logger.info("Loaded %d rows from Google Sheet", len(records))
    # Validate columns
    if records:
        for key in REQUIRED_COLUMNS:
            if key not in records[0]:
                raise RuntimeError(f"Expected column '{key}' in sheet first row headers.")
    return records


def should_run_now():
    # Check facility local time and decide whether to run
    tz = pytz.timezone(FACILITY_TZ)
    local_now = datetime.now(tz)
    logger.info("Facility local time: %s", local_now.isoformat())
    return local_now.hour == DESIRED_LOCAL_HOUR


async def perform_login(page):
    """
    Attempt to programmatically log in to recreation.gov using provided credentials.
    Selectors here are best-effort and may require tuning.
    If login requires CAPTCHA or additional steps, you must do a manual login once
    and save the storage state for reuse across runs.
    """
    logger.info("Logging in programmatically")
    await page.goto("https://www.recreation.gov/")
    try:
        # try click sign-in link
        await page.locator("a[aria-label='Sign In'], a[href*='signin']").click(timeout=5000)
    except Exception:
        logger.debug("Sign-in link click failed or not present (maybe already showing login)")

    # Wait for login fields - selectors may differ if Recreation.gov uses hosted widget
    try:
        await page.fill("input#username, input[name='email']", RECGOV_USERNAME or "")
        await page.fill("input#password, input[name='password']", RECGOV_PASSWORD or "")
        # Click submit button if present
        try:
            await page.locator("button[type='submit']").click()
        except Exception:
            # fallback: press Enter in password field
            await page.press("input#password, input[name='password']", "Enter")
        # Wait for an element visible when logged-in (account/profile)
        await page.wait_for_selector("a[aria-label='Account'], img[alt*='profile']", timeout=15000)
        logger.info("Login appears successful (account element found).")
    except PWTimeout:
        logger.warning("Timed out waiting for login to complete; check selectors or interactive captcha.")
    except Exception as e:
        logger.exception("Login attempt failed: %s", e)


async def prepare_single_page(page, booking):
    """
    Navigate to facility page, select ticket/time/date and fill booking details.
    Return the locator for the submit button (so caller can click many simultaneously).
    This function uses placeholder selectors — you MUST inspect the live HTML and update.
    """
    # Unpack booking row
    first_name = booking.get("first_name", "")
    last_name = booking.get("last_name", "")
    email = booking.get("email", "")
    phone = booking.get("phone", "")
    ticket_qty = int(booking.get("ticket_qty", 1))
    desired_date = booking.get("date", "")  # optional; format may require normalization
    desired_time = booking.get("time", "")  # optional; the slot label to match

    logger.info("Preparing page for %s %s (%s), qty=%s", first_name, last_name, email, ticket_qty)

    await page.goto(FACILITY_URL)
    # Wait for page load / tours tab
    try:
        await page.wait_for_selector("div#tab-tours, [data-tab='tours']", timeout=10000)
    except PWTimeout:
        logger.warning("Tours tab not found promptly; continuing to attempt selectors.")

    # --- SELECT DATE ---
    if desired_date:
        try:
            if await page.locator("input[type='date']").count() > 0:
                await page.fill("input[type='date']", desired_date)
                logger.info("Set date input to %s", desired_date)
            else:
                logger.debug("Date input not present; calendar selection may be required.")
        except Exception as e:
            logger.debug("Date selection attempt failed: %s", e)

    # --- SELECT TIME SLOT ---
    submit_locator = None
    try:
        if desired_time:
            # Try to click a timeslot button that contains the desired_time text
            slot_btn = page.locator(f"xpath=//button[contains(., \"{desired_time}\")]")
            if await slot_btn.count() > 0:
                await slot_btn.first.click()
                logger.info("Clicked timeslot: %s", desired_time)
            else:
                logger.debug("No slot button with desired_time text found.")
    except Exception as e:
        logger.debug("Timeslot selection failed: %s", e)

    # --- SET QTY ---
    try:
        if await page.locator("select[name*='quantity'], select[id*='quantity']").count() > 0:
            sel = page.locator("select[name*='quantity'], select[id*='quantity']").first
            await sel.select_option(str(ticket_qty))
            logger.info("Selected quantity %s via select", ticket_qty)
        elif await page.locator("input[type='number']").count() > 0:
            await page.fill("input[type='number']", str(ticket_qty))
            logger.info("Set numeric quantity to %s", ticket_qty)
        else:
            logger.debug("Quantity control not found; possibly automatic qty from timeslot UI")
    except Exception as e:
        logger.debug("Quantity selection failed: %s", e)

    # --- FILL ATTENDEE DETAILS ---
    # These CSS selectors are placeholders. Update them after inspecting the real booking form.
    try:
        if await page.locator("input[name='firstName'], input[id*='first']").count() > 0:
            await page.fill("input[name='firstName'], input[id*='first']", first_name)
        if await page.locator("input[name='lastName'], input[id*='last']").count() > 0:
            await page.fill("input[name='lastName'], input[id*='last']", last_name)
        if await page.locator("input[name='email'], input[id*='email']").count() > 0:
            await page.fill("input[name='email'], input[id*='email']", email)
        if await page.locator("input[name='phone'], input[id*='phone']").count() > 0:
            await page.fill("input[name='phone'], input[id*='phone']", phone)
        logger.info("Filled attendee info fields (placeholders).")
    except Exception as e:
        logger.debug("Filling attendee details encountered an error: %s", e)

    # --- FIND SUBMIT / RESERVE BUTTON ---
    try:
        candidates = page.locator(
            "xpath=//button[contains(., 'Add to Cart') or contains(., 'Add to cart') or contains(., 'Reserve') or contains(., 'Purchase') or contains(., 'Continue')]"
        )
        if await candidates.count() > 0:
            submit_locator = candidates.first
            logger.info("Submit candidate found.")
        else:
            logger.warning("No submit-like button found on prepared page.")
    except Exception as e:
        logger.debug("Search for submit button failed: %s", e)

    return submit_locator


async def main():
    # Load bookings
    bookings = load_google_sheet()
    if not bookings:
        logger.info("No bookings to process.")
        return

    # Check scheduling condition
    if RUN_ONCE_PER_HOUR and not should_run_now():
        logger.info("Not the configured facility hour; exiting without action.")
        return

    # Limit bookings to MAX_WORKERS
    workers = bookings[:MAX_WORKERS]
    logger.info("Processing %d bookings (MAX_WORKERS=%d)", len(workers), MAX_WORKERS)

    # Start Playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        # Create an initial context and page to login and capture storage state
        context = await browser.new_context()
        page = await context.new_page()

        # Programmatic login attempt if credentials provided
        if RECGOV_USERNAME and RECGOV_PASSWORD:
            await perform_login(page)
        else:
            logger.warning("RECGOV_USERNAME/RECGOV_PASSWORD not provided; interactive login required.")

        # Save storage state to reuse across contexts
        storage = await context.storage_state()
        await context.close()

        # Prepare pages in parallel contexts
        pages = []
        submit_locators = []

        async def prepare_worker(i, booking):
            ctx = await browser.new_context(storage_state=storage)
            pg = await ctx.new_page()
            locator = await prepare_single_page(pg, booking)
            pages.append((pg, ctx))
            submit_locators.append((pg, locator))

        # Launch preparations concurrently
        tasks = [prepare_worker(i, b) for i, b in enumerate(workers)]
        await asyncio.gather(*tasks)

        # If DRY_RUN: do not click submit, just capture diagnostics
        if DRY_RUN:
            logger.info("DRY_RUN enabled — prepared pages but will not submit.")
            for i, (pg, loc) in enumerate(submit_locators):
                logger.info("Prepared page %d url=%s submit_locator=%s", i, pg.url, bool(loc))
            if not HEADLESS:
                logger.info("Non-headless mode: leaving browser open for inspection. Exiting now.")
                return
            else:
                await browser.close()
                return

        # Execute near-simultaneous submit: click all submit buttons in quick succession
        logger.info("Executing near-simultaneous submits for %d prepared pages", len(submit_locators))
        click_tasks = []
        for pg, locator in submit_locators:
            if locator:
                click_tasks.append(locator.click())
            else:
                logger.warning("No submit locator for one page; skipping that page")

        # Run all click tasks concurrently
        try:
            await asyncio.gather(*click_tasks)
            logger.info("Submit clicks executed. Now waiting briefly for confirmations.")
            await asyncio.sleep(5)
        except Exception as e:
            logger.exception("Error executing submit clicks: %s", e)

        # Post-submit: check for cart or checkout navigation and log results
        for i, (pg, loc) in enumerate(submit_locators):
            try:
                if await pg.locator("div.cart, #checkout, a[href*='checkout']").count() > 0:
                    logger.info("Page %d appears to be at cart/checkout: %s", i, pg.url)
                else:
                    logger.info("Page %d after submit url=%s", i, pg.url)
            except Exception:
                logger.debug("Could not verify post-submit state for page %d", i)

        # Keep browser open in non-headless mode for manual completion if needed
        if HEADLESS:
            await browser.close()
        else:
            logger.info("Non-headless mode: leaving browser open for manual completion. Exiting script.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("Script failed: %s", exc)
        raise
