import csv
import os
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import streamlit as st
load_dotenv()

DEFAULT_OUTPUT_DIR = "resume_pdfs"

def _robust_login(page, email: str, password: str, max_attempts: int = 3):
    """
    Robust Breezy login with retries.
    Retries the entire login flow if it gets stuck or fails.
    """

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"[LOGIN] Attempt {attempt}/{max_attempts}")

            page.goto("https://app.breezy.hr/signin", wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

            # Wait for any login form/email field
            page.wait_for_selector(
                "input[type='email'], input[name='email'], input[name='email_address']",
                timeout=20000,
            )

            # -------- EMAIL --------
            if page.locator("input[name='email_address']").count() > 0:
                email_input = page.locator("input[name='email_address']").first
            elif page.locator("input[name='email']").count() > 0:
                email_input = page.locator("input[name='email']").first
            else:
                email_input = page.locator("input[type='email']").first

            email_input.click()
            email_input.fill("")
            email_input.type(email, delay=15)

            # -------- PASSWORD --------
            if page.locator("input[name='password']").count() > 0:
                pw_input = page.locator("input[name='password']").first
            else:
                pw_input = page.locator("input[type='password']").first

            pw_input.click()
            pw_input.fill("")
            pw_input.type(password, delay=15)

            # -------- SUBMIT --------
            if page.locator("button[type='submit']").count() > 0:
                page.locator("button[type='submit']").first.click()
            else:
                page.locator("input[type='submit']").first.click()

            # -------- SUCCESS OR FAILURE DETECTION --------
            page.wait_for_timeout(1200)
            page.wait_for_function(
                """
                () => {
                    const url = window.location.href;
                    if (!url.includes('/signin')) return true;

                    const err =
                        document.querySelector('[role="alert"]') ||
                        document.querySelector('.toast') ||
                        document.querySelector('.Toastify__toast-body') ||
                        document.querySelector('.alert');

                    return !!(err && err.innerText && err.innerText.trim().length > 0);
                }
                """,
                timeout=20000,
            )

            if "signin" in page.url:
                raise RuntimeError("Still on /signin after submit")

            print("[LOGIN] Success")
            return  # LOGIN SUCCESS

        except Exception as e:
            print(f"[LOGIN] Attempt {attempt} failed: {e}")

            if attempt == max_attempts:
                raise RuntimeError(
                    "Breezy login failed after multiple attempts "
                    "(likely bot protection, bad creds, or 2FA)"
                )

            # Reset state before retry
            page.wait_for_timeout(1500)


def download_resumes_from_csv(
    csv_path: str, output_dir: str = DEFAULT_OUTPUT_DIR, headless: bool = True, should_cancel=None
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
    
    def _cancel_now() -> bool:
        try:
            return bool(should_cancel and should_cancel())
        except Exception:
            return False

    if _cancel_now():
        print("[CANCEL] Cancel requested before starting Playwright.")
        return
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
    
            # (your login call here)
            # _robust_login(page, email, password, max_attempts=3)
    
            if _cancel_now():
                print("[CANCEL] Cancel requested right after login.")
                return
    
            # --- DOWNLOAD RESUMES ---
            with open(csv_path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if _cancel_now():
                        print("[CANCEL] Cancel requested during downloads. Stopping.")
                        return
    
                    # ... existing per-row logic ...
    
                    # OPTIONAL: check again after each download
                    if _cancel_now():
                        print("[CANCEL] Cancel requested after a download. Stopping.")
                        return
        finally:
            try:
                browser.close()
            except Exception:
                pass



if __name__ == "__main__":
    CSV_FILE = "resume_urls.csv"
    download_resumes_from_csv(CSV_FILE)
