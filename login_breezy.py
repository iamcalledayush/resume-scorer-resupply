import csv
import os
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import streamlit as st
load_dotenv()

DEFAULT_OUTPUT_DIR = "resume_pdfs"


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

        print("Opening Breezy login page...")
        page.goto("https://app.breezy.hr/signin", wait_until="domcontentloaded")

        # --- LOGIN ---
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1500)
        
        # Wait for any likely login form/email input
        page.wait_for_selector(
            "form, input[type='email'], input[name='email'], input[name='email_address']",
            timeout=60000
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
        else:
            page.click("input[type='submit']")

        # Wait until main Breezy dashboard loads
        # After submit, wait for either:
        # 1) URL changes away from /signin OR
        # 2) Some known post-login UI appears OR
        # 3) A login error appears
        page.wait_for_timeout(1500)
        
        # Wait for navigation OR DOM change
        try:
            page.wait_for_url("**breezy.hr/**", timeout=60000)
        except Exception:
            pass
        
        # If still on signin, check for error and fail fast
        current_url = page.url
        print("After login URL:", current_url)
        
        if "signin" in current_url:
            # Try to detect common error patterns (toast / alert / inline)
            err_text = ""
            for sel in [
                "[role='alert']",
                ".Toastify__toast-body",
                ".toast",
                ".alert",
                "text=Invalid",
                "text=incorrect",
                "text=Try again",
            ]:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        err_text = loc.first.inner_text(timeout=1000)
                        break
                except Exception:
                    continue
            raise RuntimeError(f"Login did not redirect off /signin. Possible login failure or bot-check. URL={current_url}. Error={err_text[:200]}")
        else:
            print("Login likely succeeded (left /signin).")

        print("Successfully logged in!")

        # --- DOWNLOAD RESUMES ---
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)

            for row in reader:
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
                filename = f"{safe_name}.pdf"
                filepath = os.path.join(output_dir, filename)
                download.save_as(filepath)
                print(f"Saved: {filename}")
                time.sleep(1)

        browser.close()


if __name__ == "__main__":
    CSV_FILE = "resume_urls.csv"
    download_resumes_from_csv(CSV_FILE)
