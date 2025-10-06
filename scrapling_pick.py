import os
import pyotp
import random
import functools
import time

from argparse import ArgumentParser
from functools import wraps
from typing import Callable, TypeVar
from pathlib import Path
from playwright.sync_api import Page
from scrapling.engines.toolbelt.custom import Response
from scrapling.fetchers import StealthySession
from scrapling.cli import log
from urllib.parse import urlparse
from datetime import datetime

box_selector = "div #cf_turnstile"
button_selector = "button[id='process_claim_hourly_faucet']"

T = TypeVar('T')


def screenshot_action(func: Callable[[Page], T]) -> Callable[[Page], T]:
    """Decorator that takes before/after screenshots using the decorated function's name and currency from closure.

    This decorator preserves the return type of the decorated function.
    """

    @functools.wraps(func)
    def wrapper(page: Page) -> T:
        # Get the function name
        func_name = func.__name__

        # Extract currency from the closure variables
        currency = "UNK"
        if func.__closure__:
            # Map closure variable names to their values
            closure_vars = func.__code__.co_freevars
            for i, var_name in enumerate(closure_vars):
                if var_name == "currency":
                    currency = func.__closure__[i].cell_contents
                    break

        # Create base filename with timestamp
        timestamp = int(time.time() * 1000)  # milliseconds for uniqueness

        # Ensure screenshots directory exists
        screenshot_dir = Path("screenshots")
        screenshot_dir.mkdir(exist_ok=True)

        # Take BEFORE screenshot
        before_filename = f"{func_name}_{currency}_{timestamp}_before.png"
        page.screenshot(path=str(screenshot_dir / before_filename))

        # Execute the original function
        result = func(page)

        # Take AFTER screenshot
        after_filename = f"{func_name}_{currency}_{timestamp}_after.png"
        page.screenshot(path=str(screenshot_dir / after_filename))

        return result

    return wrapper


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
        tuple[float, float, float]: The balance wagered target.
    """
    balance_selector = res.css(selector="span[class=user_balance]", identifier=f"balance_{currency}")
    wagered_selector = res.css(selector="b[id=total_wagered]", identifier=f"wagered_{currency}")
    target_selector = res.css(selector="b[id=wagering_target]", identifier=f"target_{currency}")

    balance_text = balance_selector.get().text if balance_selector.get() else "NAN"
    wagered_text = wagered_selector.get().text if wagered_selector.get() else "NAN"
    target_text = target_selector.get().text if target_selector.get() else "NAN"

    balance = float(balance_text.strip().replace(",", "").strip())
    wagered = float(wagered_text.strip())
    target = float(target_text.strip())

    log.debug("Balance: %s %s, Wagered: %s, Target: %s", balance, currency, wagered, target)

    return (balance, wagered, target)


def google_oauth_login_page_make() -> tuple[Callable[[Page], None], Callable[[], bool]]:
    """Create a Google OAuth login page action.

    Returns:
        tuple[Callable[[Page], None], Callable[[], bool]]: A tuple containing:
            - The Google OAuth login page action function
            - A function that always returns False (Google OAuth doesn't auto-claim)
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

    def was_claim_attempted() -> bool:
        """Google OAuth login never auto-claims.

        Returns:
            bool: Always False
        """
        return False

    return google_login_page, was_claim_attempted


def login_page_make(
    username: str, password: str, currency: str = "UNK"
) -> tuple[Callable[[Page], None], Callable[[], bool]]:
    """Create a login page action.

    Args:
        username (str): Username for login.
        password (str): Password for login.
        currency (str, optional): The currency code. Defaults to "UNK".

    Returns:
        tuple[Callable[[Page], None], Callable[[], bool]]: A tuple containing:
            - The login page action function
            - A function that returns True if claim was already attempted during login
    """
    claim_attempted = {"value": False}  # Use dict for mutability in closure

    @screenshot_action
    def login_page(page: Page):
        _ = currency  # Force currency into closure for screenshot_action decorator
        login_button_selector = "button[id='process_login']"

        if "login" not in page.url:
            log.warning("Not on login page, current URL: %s", page.url)
            log.warning("Attempting to click the claim button, assuming already logged in.")
            page.click(button_selector)
            claim_attempted["value"] = True
            return

        page.fill("input[id='user_email']", username)
        page.fill("input[id='password']", password)

        try:
            page.locator(box_selector).scroll_into_view_if_needed(timeout=2000)
            page.locator(box_selector).wait_for(state="visible", timeout=2000)
            log.debug("Captcha box detected.")
            page.wait_for_timeout(500)
        except Exception:
            log.debug("No captcha box detected.")

        page.click(login_button_selector)

    def was_claim_attempted() -> bool:
        """Check if the claim button was already clicked during login.

        Returns:
            bool: True if claim was attempted, False otherwise.
        """
        return claim_attempted["value"]

    return login_page, was_claim_attempted


def make_claim_faucet(selector: str, currency: str = "UNK") -> Callable[[Page], None]:
    """Create a claim faucet action.

    Args:
        selector (str): The CSS selector for the claim button.
    """

    @screenshot_action
    def claim_faucet(page: Page):
        _ = currency  # Force currency into closure for screenshot_action decorator
        try:
            page.locator(selector).scroll_into_view_if_needed(timeout=2000)
            page.locator(selector).wait_for(state="visible", timeout=2000)
            page.wait_for_timeout(gaussian_random_delay(mean=500, stddev=100))  # Wait for some time, because.
        except Exception:
            log.info("No captcha box detected.")
            return

        delay = gaussian_random_delay()
        page.click(selector, delay=delay)

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


# def scroll_page(page: Page):
#     page.mouse.wheel(10, 0)
#     page.mouse.move(100, 400)
#     page.mouse.up()


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
        return "{: <21} | {: <8} | {: >15} | {: >15} | {: >15}".format(
            self.url,
            self.currency,
            format(self.balance, ".8f"),
            format(self.wagered, ".8f"),
            format(self.target, ".8f"),
        )

    def update(self, balance: float, wagered: float, target: float):
        self.balance = balance
        self.wagered = wagered
        self.target = target

    def get_env_prefix(self) -> str:
        return url_to_env_prefix(self.url)


def summarize_picks(picks: list[Pick]):
    log.info("{: <21} | {: <8} | {: >15} | {: >15} | {: >15}".format("URL", "Currency", "Balance", "Wagered", "Target"))
    for pick in picks:
        log.info(pick)


def main(
    proxy: str | None = None,
    use_google_oauth: bool = False,
    headless: bool = False,
    skip_claim: bool = False,
    summarize: bool = False,
):
    """
    Main function to run the scraper.
    Args:
        proxy (str | None, optional): Proxy URL to use. Defaults to None.
        use_google_oauth (bool, optional): Whether to use Google OAuth for login. Defaults
        headless (bool, optional): Whether to run in headless mode. Defaults to False.
        skip_claim (bool, optional): Whether to skip the claim step. Defaults to False.
        summarize (bool, optional): Whether to summarize the results. Defaults to False.

    Returns:
        None
    """

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
            login_page, claim_already_attempted = google_oauth_login_page_make()
        else:
            username, password = get_credentials(pick.url)
            login_page, claim_already_attempted = login_page_make(username, password, currency=pick.currency)

        with StealthySession(
            proxy=proxy,
            headless=headless,
            humanize=True,
            solve_cloudflare=True,
            google_search=False,
            additional_args={"user_data_dir": "./logged_in_data"},
        ) as session:
            try:
                login_response: Response = session.fetch(
                    f"{pick.url}login.php",
                    page_action=login_page,
                    wait=5000,
                )

                balance, wagered, target = get_balance_wagered_target(login_response)
                pick.update(balance, wagered, target)
                log.debug("%s", pick)

                if skip_claim:
                    log.info("Skipping claim as per --skip-claim")
                    continue

                if claim_already_attempted():
                    log.info("Skipping claim - already attempted during login (user was already logged in)")
                    continue

                faucet = make_claim_faucet(button_selector, currency=pick.currency)
                _: Response = session.fetch(
                    f"{pick.url}faucet.php",
                    page_action=faucet,
                    wait=5000,
                )

            except Exception as e:
                log.error("Error fetching %s: %s", pick.url, e)
                continue

    if summarize:
        summarize_picks(picks)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--proxy", help="proxy url to use", default=None)
    parser.add_argument("--headless", help="run in headless mode", action="store_true")
    parser.add_argument(
        "--google-oauth",
        action="store_true",
        help="use Google OAuth login instead of username/password",
    )
    parser.add_argument(
        "--skip-claim",
        action="store_true",
        help="skip claiming the faucet (just login and get balance)",
    )
    parser.add_argument("--summarize", help="summarize the results", action="store_true")

    args = parser.parse_args()

    main(args.proxy, args.google_oauth, args.headless, args.skip_claim, args.summarize)
