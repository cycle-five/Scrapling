import os
import pyotp

from typing import Callable
from argparse import ArgumentParser
from playwright.sync_api import Page
from scrapling.engines.toolbelt.custom import Response
from scrapling.fetchers import StealthySession
from scrapling.cli import log
from urllib.parse import urlparse
import random

box_selector = "div #cf_turnstile"
button_selector = "button[id='process_claim_hourly_faucet']"


def gaussian_random_delay(mean: float = 50, stddev: float = 10) -> int:
    """Generate a Gaussian random delay in milliseconds.
    The defaults are chosen to (hopefully) simulate human-like delays.

    Args:
        mean (float, optional): The mean delay in milliseconds. Defaults to 50.
        stddev (float, optional): The standard deviation of the delay in milliseconds. Defaults to 10.

    Returns:
        int: A random delay in milliseconds.
    """
    return int(max(0, random.gauss(mean, stddev)))


def get_balance_wagered_target(res: Response, currency: str = "UNK") -> tuple[float, float, float]:
    """Extract the balance from the page.

    Args:
        page (Page): The Playwright page object.
        currency (str, optional): The currency code. Defaults to "UNK".

    Returns:
        float: The balance wagered target.
    """
    balance_selector = res.css(
        selector="span[class=user_balance]",
        identifier=f"balance_{currency}", adaptive=True)
    wagered_selector = res.css(
        selector="b[id=total_wagered]",
        identifier=f"wagered_{currency}", adaptive=True)
    target_selector = res.css(
        selector="b[id=wagering_target]",
        identifier=f"target_{currency}", adaptive=True)

    balance_text = balance_selector.get(default="0")
    wagered_text = wagered_selector.get(default="0")
    target_text = target_selector.get(default="0")

    balance = float(balance_text.strip().replace(",", "").strip())
    wagered = float(wagered_text.strip())
    target = float(target_text.strip())

    return (balance, wagered, target)


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
                page.locator(selector).first.click(delay=gaussian_random_delay(), timeout=2000)
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
            page.click('button:has-text("Next")', delay=gaussian_random_delay(), timeout=3000)
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
            page.click('button:has-text("Next")', delay=gaussian_random_delay(), timeout=3000)
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


def login_page_make(username: str, password: str, currency: str = "UNK") -> Callable[[Page], None]:
    """Create a login page action.

    Args:
        username (str): Username for login.
        password (str): Password for login.

    Returns:
        Callable[[Page], None]: A function that performs the login action on the given page.
    """

    def login_page(page: Page):
        page.screenshot(path=f"screenshots/before_login_{currency}.png")
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
        page.screenshot(path=f"screenshots/after_login_{currency}.png")

    return login_page


def make_claim_faucet(selector: str, currency: str = "UNK") -> Callable[[Page], None]:
    """Create a claim faucet action.

    Args:
        selector (str): The CSS selector for the claim button.
    """

    def claim_faucet(page: Page):
        page.screenshot(path=f"screenshots/before_claim_{currency}.png")
        try:
            page.locator(selector).scroll_into_view_if_needed(timeout=2000)
            page.locator(selector).wait_for(state="visible", timeout=2000)
            page.wait_for_timeout(gaussian_random_delay(mean=500, stddev=100))  # Wait for some time, because.
        except Exception:
            log.info("No captcha box detected.")
            return

        delay = gaussian_random_delay()
        page.click(selector, delay=delay)
        page.screenshot(path=f"screenshots/after_claim_{currency}.png")

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


class Pick:
    url: str
    currency: str
    balance: float = 0.0
    wagered: float = 0.0
    target: float = 0.0

    def __init__(self, url: str, currency: str):
        self.url = url
        self.currency = currency

    def __str__(self):
        return f"At {self.url}, Balance: {self.balance} {self.currency}, Wagered: {self.wagered}, Target: {self.target}"

    def update(self, balance: float, wagered: float, target: float):
        self.balance = balance
        self.wagered = wagered
        self.target = target

    def get_env_prefix(self) -> str:
        return url_to_env_prefix(self.url)


def main(proxy: str | None = None, use_google_oauth: bool = False, headless: bool = True):
    picks = [
        Pick(url="https://tronpick.io/", currency="TRX"),
        Pick(url="https://litepick.io/", currency="LTC"),
        Pick(url="https://polpick.io/", currency="POL"),
        Pick(url="https://bnbpick.io/", currency="BNB"),
        Pick(url="https://dogepick.io/", currency="DOGE"),
        Pick(url="https://solpick.io/", currency="SOL"),
        Pick(url="https://suipick.io/", currency="SUI"),
        Pick(url="https://tonpick.game/", currency="TON"),
    ]

    for pick in picks:
        # Choose login method
        if use_google_oauth:
            login_page = google_oauth_login_page_make()
        else:
            username, password = get_credentials(pick.url)
            login_page = login_page_make(username, password, currency=pick.currency)

        with StealthySession(
            proxy=proxy,
            headless=headless,
            humanize=True,
            solve_cloudflare=True,
            google_search=False,
            # additional_args={"user_data_dir": "./google_logged_in"},
        ) as session:
            try:
                _: Response = session.fetch(
                    f"{pick.url}login.php",
                    page_action=login_page,
                    wait=5000,
                )

                faucet = make_claim_faucet(button_selector, currency=pick.currency)
                faucet_response: Response = session.fetch(
                    f"{pick.url}faucet.php",
                    page_action=faucet,
                    wait=5000,
                )

                balance, wagered, target = get_balance_wagered_target(faucet_response)

                pick.update(balance, wagered, target)

                log.info("%s", pick)

            except Exception as e:
                log.error("Error fetching %s: %s", pick, e)
                continue


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--proxy", help="proxy url to use", default=None)
    parser.add_argument("--headless", help="run in headless mode", action="store_true")
    parser.add_argument(
        "--google-oauth",
        action="store_true",
        help="use Google OAuth login instead of username/password",
    )
    args = parser.parse_args()
    main(args.proxy, args.google_oauth, args.headless)
