import csv
import os
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import streamlit as st
import requests

load_dotenv()

DEFAULT_OUTPUT_DIR = "resume_pdfs"
def download_resumes_from_csv_with_cookie(
    csv_path: str,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    cookie_header_value: str = "",
) -> None:
    """
    Download Breezy resume PDFs using a manually provided Cookie header value
    (copied from browser DevTools). No Playwright, no scraping.
    """
    os.makedirs(output_dir, exist_ok=True)

    if not cookie_header_value.strip():
        raise RuntimeError("cookie_header_value is required")

    session = requests.Session()
    session.headers.update(
        {
            "Cookie": cookie_header_value.strip(),
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://app.breezy.hr/",
            "Accept": "application/pdf,*/*",
        }
    )

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)

        for idx, row in enumerate(reader, start=1):
            name = (row.get("name") or "").strip() or "candidate"
            url = (row.get("resume") or "").strip()

            if not url.startswith("http"):
                print(f"Skipping invalid resume URL for {name}: {url}")
                continue

            safe_name = name.replace(" ", "_")
            filename = f"{safe_name}__{idx}.pdf"
            filepath = os.path.join(output_dir, filename)

            print(f"Downloading resume for {name}...")
            r = session.get(url, timeout=60)

            if r.status_code != 200:
                raise RuntimeError(f"Download failed for {name} ({r.status_code}): {r.text[:200]}")

            if not r.content.startswith(b"%PDF"):
                raise RuntimeError(f"Not a PDF for {name}. First bytes: {r.content[:15]}")

            with open(filepath, "wb") as out:
                out.write(r.content)

            print(f"Saved: {filename}")
            time.sleep(0.3)


def _robust_login(page, email: str, password: str, max_attempts: int = 3):
    # Single-attempt login (ignore max_attempts)
    print("Opening Breezy login page...")
    page.goto("https://app.breezy.hr/signin", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    screenshot_bytes = page.screenshot(full_page=True)
    st.image(screenshot_bytes, caption="Breezy page after navigation")
    # --- LOGIN ---
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)

    # Wait for any likely login form/email input
    page.wait_for_selector(
        "form, input[type='email'], input[name='email'], input[name='email_address']",
        timeout=30000
    )

    # Fill email (try multiple possibilities)
    if page.locator("input[name='email_address']").count() > 0:
        page.fill("input[name='email_address']", email)
    elif page.locator("input[name='email']").count() > 0:
        page.fill("input[name='email']", email)
    else:
        page.fill("input[type='email']", email)

    # Fill password (try common options)
    if page.locator("input[name='password']").count() > 0:
        page.fill("input[name='password']", password)
    else:
        page.fill("input[type='password']", password)

    # Click submit (try common options)
    if page.locator("button[type='submit']").count() > 0:
        page.click("button[type='submit']")
        page.wait_for_timeout(2000)
        print("[LOGIN] URL after submit:", page.url)
    else:
        page.click("input[type='submit']")
        page.wait_for_timeout(2000)
        print("[LOGIN] URL after submit:", page.url)


    # Wait until main Breezy dashboard loads
    page.wait_for_url("**/app/**", timeout=30000)
    print("Successfully logged in!")


def login_to_breezy(headless: bool = True):
    """
    Launch Playwright, log into Breezy, and return (p, browser, context, page).
    Caller is responsible for closing browser/context and stopping playwright.
    """
    BREEZY_EMAIL = os.getenv("BREEZY_EMAIL", "")
    BREEZY_PASSWORD = os.getenv("BREEZY_PASSWORD", "")
    if not BREEZY_EMAIL or not BREEZY_PASSWORD:
        raise RuntimeError("BREEZY_EMAIL and BREEZY_PASSWORD must be set in the environment.")

    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = browser.new_context(
        accept_downloads=True,
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    )
    page = context.new_page()

    _robust_login(page, BREEZY_EMAIL, BREEZY_PASSWORD)
    return p, browser, context, page

def download_resumes_from_csv_with_page(
    page,
    csv_path: str,
    output_dir: str = DEFAULT_OUTPUT_DIR,
) -> None:
    """
    Download resumes using an already-authenticated Breezy page.
    """
    os.makedirs(output_dir, exist_ok=True)

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)

        for idx, row in enumerate(reader, start=1):
            name = (row.get("name") or "").strip() or "candidate"
            url = (row.get("resume") or "").strip()

            if not url.startswith("http"):
                print(f"Skipping invalid resume URL for {name}: {url}")
                continue

            print(f"Downloading resume for {name}...")

            with page.expect_download() as download_info:
                page.evaluate(f"window.location.href = '{url}'")

            download = download_info.value
            safe_name = name.replace(" ", "_")
            filename = (row.get("filename") or f"{safe_name}__{idx}.pdf").strip()
            filepath = os.path.join(output_dir, filename)
            download.save_as(filepath)
            print(f"Saved: {filename}")
            time.sleep(1)


def download_resumes_from_csv(
    csv_path: str, output_dir: str = DEFAULT_OUTPUT_DIR, headless: bool = True
) -> None:
    """
    Log in to Breezy via Playwright and download all resume URLs in the CSV.

    Expected CSV columns:
    - name
    - resume (URL)

    Credentials are read from BREEZY_EMAIL and BREEZY_PASSWORD.
    """
    BREEZY_EMAIL = os.getenv("BREEZY_EMAIL", "")
    BREEZY_PASSWORD = os.getenv("BREEZY_PASSWORD", "")
    email = BREEZY_EMAIL
    password = BREEZY_PASSWORD
    if not email or not password:
        raise RuntimeError(
            "BREEZY_EMAIL and BREEZY_PASSWORD must be set in the environment."
        )

    os.makedirs(output_dir, exist_ok=True)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        # --- LOGIN (robust + retries) ---
        _robust_login(page, email, password)

        # --- DOWNLOAD RESUMES ---
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)

            for idx, row in enumerate(reader, start=1):
                name = (row.get("name") or "").strip() or "candidate"
                url = (row.get("resume") or "").strip()

                if not url.startswith("http"):
                    print(f"Skipping invalid resume URL for {name}: {url}")
                    continue

                print(f"Downloading resume for {name}...")

                with page.expect_download() as download_info:
                    page.evaluate(f"window.location.href = '{url}'")

                download = download_info.value
                safe_name = name.replace(" ", "_")
                filename = f"{safe_name}__{idx}.pdf"
                filepath = os.path.join(output_dir, filename)
                download.save_as(filepath)
                print(f"Saved: {filename}")
                time.sleep(1)

        browser.close()
        



if __name__ == "__main__":
    CSV_FILE = "resume_urls.csv"
    download_resumes_from_csv(CSV_FILE)
