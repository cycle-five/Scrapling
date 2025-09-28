import os
import pyotp

from typing import Callable
from argparse import ArgumentParser
from playwright.sync_api import Page
from scrapling.fetchers import Response, StealthySession
from scrapling.cli import log
from urllib.parse import urlparse

box_selector = "div #cf_turnstile"
button_selector = "button[id='process_claim_hourly_faucet']"


def google_oauth_login_page_make() -> Callable[[Page], None]:
    """Create a Google OAuth login page action.

    Returns:
        Callable[[Page], None]: Function that performs Google OAuth login.
    """

    def google_login_page(page: Page):
        # Wait for page to load
        page.wait_for_load_state("domcontentloaded", timeout=5000)

        # Look for and click Google sign-in button
        google_button_selectors = [
            "button:has-text('Google')",
            "a:has-text('Google')",
            "button:has-text('Sign in with Google')",
            "[class*='google'][class*='login']",
            "[id*='google'][id*='login']",
        ]

        google_button_clicked = False
        for selector in google_button_selectors:
            try:
                page.locator(selector).first.click(timeout=2000)
                google_button_clicked = True
                log.info("Clicked Google sign-in button with selector: %s", selector)
                break
            except Exception:
                continue

        if not google_button_clicked:
            log.error("Could not find Google sign-in button")
            return

        # Wait for Google login page or redirect
        try:
            page.wait_for_url("**/accounts.google.com/**", timeout=10000)
        except Exception:
            log.info("Already logged in or no redirect to Google login page")
            return

        # Fill in Google email
        email = os.getenv("GOOGLE_EMAIL")
        if not email:
            log.error("GOOGLE_EMAIL environment variable not set")
            return

        try:
            page.fill('input[type="email"]', email, timeout=5000)
            page.click('button:has-text("Next")', timeout=3000)
            log.info("Entered Google email")
        except Exception as e:
            log.error("Failed to enter email: %s", e)
            return

        # Fill in password
        password = os.getenv("GOOGLE_PASSWORD")
        if not password:
            log.error("GOOGLE_PASSWORD environment variable not set")
            return

        try:
            page.wait_for_selector('input[type="password"]', state="visible", timeout=10000)
            page.fill('input[type="password"]', password, timeout=5000)
            page.click('button:has-text("Next")', timeout=3000)
            log.info("Entered Google password")
        except Exception as e:
            log.error("Failed to enter password: %s", e)
            return

        # Wait for redirect back to the original site
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
            log.info("Google OAuth login completed successfully")
        except Exception as e:
            log.warning("Timeout waiting for redirect, continuing: %s", e)

    return google_login_page


def login_page_make(username: str, password: str) -> Callable[[Page], None]:
    """Create a login page action.

    Args:
        username (str): Username for login.
        password (str): Password for login.

    Returns:
        Callable[[Page], None]: A function that performs the login action on the given page.
    """

    def login_page(page: Page):
        login_button_selector = "button[id='process_login']"
        page.fill("input[id='user_email']", username)
        page.fill("input[id='password']", password)

        try:
            page.locator(box_selector).scroll_into_view_if_needed(timeout=2000)
            page.locator(box_selector).wait_for(state="visible", timeout=2000)
            log.info("Captcha box detected, waiting for manual solve.")
            page.wait_for_timeout(500)  # Wait for manual captcha solve
        except Exception:
            log.info("No captcha box detected.")

        page.click(login_button_selector)

    return login_page


def make_claim_faucet(selector: str) -> Callable[[Page], None]:
    """Create a claim faucet action.

    Args:
        selector (str): The CSS selector for the claim button.
    """

    def claim_faucet(page: Page):
        try:
            page.locator(selector).scroll_into_view_if_needed(timeout=2000)
            page.locator(selector).wait_for(state="visible", timeout=2000)
        except Exception:
            log.info("No captcha box detected.")
            return
        page.click(selector)

    return claim_faucet


def url_to_env_prefix(url: str) -> str:
    """Convert a URL to an environment variable prefix.

    Args:
        url (str): The URL to convert.

    Returns:
        str: The environment variable prefix.
    """
    parsed_url = urlparse(url)
    netloc = parsed_url.netloc

    prefix = netloc.split(".")[0]
    return prefix.upper()


def get_credentials(url: str) -> tuple[str, str]:
    """Get the credentials for Tronpick.

    Raises:
        ValueError: If the credentials are not set.

    Returns:
        tuple[str, str]: The username and password.
    """

    env_prefix = url_to_env_prefix(url)

    username = os.getenv(f"{env_prefix}_USERNAME")
    password = os.getenv(f"{env_prefix}_PASSWORD")
    if not username or not password:
        raise ValueError(f"{env_prefix}_USERNAME and {env_prefix}_PASSWORD must be set")
    return username, password


def scroll_page(page: Page):
    page.mouse.wheel(10, 0)
    page.mouse.move(100, 400)
    page.mouse.up()


def main(proxy: str | None = None, use_google_oauth: bool = False):
    picks = [
        "https://tronpick.io/",
        "https://litepick.io/",
        "https://polpick.io/",
        "https://bnbpick.io/",
        "https://dogepick.io/",
        "https://solpick.io/",
        "https://suipick.io/",
        "https://tonpick.game/",
    ]

    for pick in picks:
        # Choose login method
        if use_google_oauth:
            login_page = google_oauth_login_page_make()
        else:
            username, password = get_credentials(pick)
            login_page = login_page_make(username, password)

        with StealthySession(
            proxy=proxy,
            headless=False,
            humanize=True,
            load_dom=True,
            solve_cloudflare=True,
            # additional_args={"user_data_dir": "./google_logged_in"},
        ) as session:
            try:
                _: Response = session.fetch(
                    f"{pick}login.php",
                    page_action=login_page,
                    wait=5000,
                )

                faucet = make_claim_faucet(button_selector)
                _: Response = session.fetch(
                    f"{pick}faucet.php",
                    page_action=faucet,
                    wait=5000,
                )

            except Exception as e:
                log.error("Error fetching %s: %s", pick, e)
                continue


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--proxy", help="proxy url to use", default=None)
    parser.add_argument(
        "--google-oauth",
        action="store_true",
        help="use Google OAuth login instead of username/password",
    )
    args = parser.parse_args()
    main(args.proxy, args.google_oauth)
