import os
import pyotp
import random
import functools
import time

from argparse import ArgumentParser
from dataclasses import dataclass
from functools import wraps
from typing import Callable, TypeVar, Any, Dict, List, Optional
from pathlib import Path
from playwright.sync_api import Page, ElementHandle, Locator
from scrapling.engines.toolbelt.custom import Response, Selector
from scrapling.fetchers import StealthySession
from scrapling.cli import log
from urllib.parse import urlparse
from datetime import datetime

PICKS: List["Pick"] = []
box_selector = "div #cf_turnstile"
button_selector = "button[id='process_claim_hourly_faucet']"

T = TypeVar("T")


@dataclass
class AccountState:
    """Represents the current state of a faucet account."""

    balance: float
    wagered: float
    target: float
    remaining_claims: int
    free_spins: int
    time_remaining: Optional[str] = None  # Format: "MM:SS" when countdown is active

    def is_faucet_available(self) -> bool:
        """Check if the faucet is currently available to claim."""
        return self.time_remaining is None

    def to_dict(self) -> Dict[str, Any]:
        """Convert AccountState to a JSON-serializable dictionary."""
        return {
            "balance": self.balance,
            "wagered": self.wagered,
            "target": self.target,
            "remaining_claims": self.remaining_claims,
            "free_spins": self.free_spins,
            "time_remaining": self.time_remaining,
        }


class Pick:
    url: str
    currency: str
    history: list[tuple[datetime, float, float, float, int, int]] = []
    last_update: datetime | None = None
    balance: float = 0.0
    wagered: float = 0.0
    target: float = 0.0
    remaining_claims: int = 0
    free_spins: int = 0
    cooldown_timer: Optional[str] = None

    def __init__(self, url: str, currency: str):
        self.url = url
        self.currency = currency
        self.history = []

    def __str__(self):
        if self.last_update is None:
            return "{: <21} | {: <8} | {: >15} | {: >15} | {: >15} | {: >15} | {: >8} | {: >8} | {: > 15}".format(
                self.url,
                self.currency,
                "N/A",
                "N/A",
                "N/A",
                "N/A",
                "N/A",
                "N/A",
                "N/A",
            )

        if self.history:
            diff = self.balance - self.history[-1][1]
        else:
            diff = 0.0
        return "{: <21} | {: <8} | {: >15} | {: >15} | {: >15} | {: >15} | {: >8} | {: >12} | {: >15}".format(
            self.url,
            self.currency,
            format(self.balance, ".8f"),
            format(self.wagered, ".8f"),
            format(self.target, ".8f"),
            format(diff, ".8f"),
            str(self.remaining_claims),
            str(self.free_spins),
            str(self.cooldown_timer) if self.cooldown_timer else "N/A",
        )

    def update(
        self,
        balance: float = None,
        wagered: float = None,
        target: float = None,
        remaining_claims: int = None,
        free_spins: int = None,
        cooldown_timer: Optional[str] = None,
        account_state: AccountState = None,
    ):
        """Update the Pick with new account state data.

        Args:
            balance: The account balance (ignored if account_state is provided)
            wagered: The amount wagered (ignored if account_state is provided)
            target: The wagering target (ignored if account_state is provided)
            remaining_claims: Number of remaining claims (ignored if account_state is provided)
            free_spins: Number of free spins (ignored if account_state is provided)
            account_state: An AccountState object containing all values (preferred method)
        """
        # If AccountState is provided, use it; otherwise use individual parameters
        if account_state is not None:
            balance = account_state.balance
            wagered = account_state.wagered
            target = account_state.target
            remaining_claims = account_state.remaining_claims
            free_spins = account_state.free_spins
            cooldown_timer = account_state.time_remaining
        elif balance is None or wagered is None or target is None:
            raise ValueError("Either account_state or all individual parameters must be provided")

        # Default values for optional parameters
        if remaining_claims is None:
            remaining_claims = 0
        if free_spins is None:
            free_spins = 0

        if self.last_update is not None:
            self.history.append(
                (self.last_update, self.balance, self.wagered, self.target, self.remaining_claims, self.free_spins)
            )

        self.balance = balance
        self.wagered = wagered
        self.target = target
        self.remaining_claims = remaining_claims
        self.free_spins = free_spins
        self.last_update = datetime.now()
        self.cooldown_timer = cooldown_timer

    def get_history(self) -> list[tuple[datetime, float, float, float, int, int]]:
        return (
            self.history
            + [(self.last_update, self.balance, self.wagered, self.target, self.remaining_claims, self.free_spins)]
            if self.last_update
            else self.history
        )

    def get_env_prefix(self) -> str:
        return url_to_env_prefix(self.url)

    def to_dict(self):
        return pick_to_dict_json_safe(self)

    def write(self, filepath: str):
        import json

        with open(filepath, "w") as f:
            json.dump(self.to_dict(), f, indent=4)

    @staticmethod
    def from_dict(data):
        return json_to_pick(data)

    @staticmethod
    def read(filepath: str):
        import json

        with open(filepath, "r") as f:
            data = json.load(f)
            return Pick.from_dict(data)


def get_balance(page: Page) -> float:
    """Get the current balance from the page.

    Args:
        page (Page): The Playwright page object.
    Returns:
        float: The current balance.
    """
    balance_selector = "body > header > nav > div.navbar-header > div > div > span"
    balance = 0.0

    balance_element: Optional[ElementHandle] = page.query_selector(selector=balance_selector)

    if balance_element:
        balance_text = balance_element.text_content()
        try:
            balance = float(balance_text.strip().replace(",", "").strip())
        except ValueError:
            log.error("Could not parse balance: %s", balance_text)

    return balance


def bet_and_start_auto(page: Page, stop: bool = False) -> float:
    """Stop any ongoing betting, rebet at 1/100 of balance, and start auto betting on the page.

    Args:
        page (Page): The Playwright page object.
    """
    balance = get_balance(page)
    wager_amount = max(0.00000100, balance / 100.0)  # Bet 1/100th of balance or 0.00000100, whichever is greater

    if stop:
        page.click("#stop_autobet", delay=gaussian_random_delay())
    page.fill("#bet_amount", str(format(wager_amount, ".8f")))
    page.click("#hard", delay=gaussian_random_delay(), force=True)
    page.check("#switch_bet_mode", force=True)
    page.click("#start_autobet", delay=gaussian_random_delay(), force=True)

    return wager_amount


def play_keno(page: Page) -> None:
    """Play keno game on pick sites."""

    # generate 7 random picks between 1 and 40
    picks = random.sample(range(1, 41), 7)
    board_selector = "#keno_table > div.keno_gamecell > div.keno_gamecell_index"

    for pick in picks:
        page.locator(board_selector).filter(has_text="{}".format(pick)).first.click(
            delay=gaussian_random_delay(), timeout=3000
        )

    wager_amount = bet_and_start_auto(page)

    # Loop every 5 seconds and check balance
    while True:
        balance = get_balance(page)
        log.info("Current balance: %f", balance)
        if (balance < wager_amount * 50) or (balance > wager_amount * 150):
            log.info("Rebetting due to balance change.")
            wager_amount = bet_and_start_auto(page, stop=True)
        elif (balance < wager_amount * 20) or (balance > wager_amount * 1000):
            log.info("Insufficient balance to continue playing keno.")
            page.close()
            break

        page.wait_for_timeout(5000)  # Wait for 5 seconds to let the game play out

    log.info("Keno game ended.")


def default_picks() -> list[Pick]:
    """Return the default list of Pick objects.
    Returns:
        list[Pick]: List of default Pick objects.
    """
    return [
        Pick(url="https://tronpick.io/", currency="TRX"),
        Pick(url="https://litepick.io/", currency="LTC"),
        Pick(url="https://polpick.io/", currency="POL"),
        Pick(url="https://bnbpick.io/", currency="BNB"),
        Pick(url="https://dogepick.io/", currency="DOGE"),
        Pick(url="https://solpick.io/", currency="SOL"),
        Pick(url="https://suipick.io/", currency="SUI"),
        Pick(url="https://tonpick.game/", currency="TON"),
    ]


def screenshot_action(func: Callable[[Page], T]) -> Callable[[Page], T]:
    """Decorator that takes before/after screenshots using the decorated function's name and currency from closure.

    This decorator preserves the return type of the decorated function.

    Args:
        func (Callable[[Page], T]): The function to decorate.
    Returns:
        Callable[[Page], T]: The wrapped function with screenshot functionality.
    """

    @functools.wraps(func)
    def wrapper(page: Page) -> T:
        """
        Wrapper function that takes screenshots before and after executing the original function.
        Args:
            page (Page): The Playwright page object.
        Returns:
            T: The return value of the original function.
        """
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

        # Get the account state from the live page.
        account_state = parse_account_state_page(page, currency)
        if account_state:
            PICKS[currency].update(account_state=account_state)

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


def parse_account_state_page(page: Page, currency: str = "UNK") -> Optional[AccountState]:
    """Parse the current account state from the faucet page.

    Extracts balance, wagering progress, remaining claims, free spins, and countdown timer
    information from the page.

    Args:
        page (Page): The Playwright Page object for querying dynamic content. Defaults to None.
        currency (str, optional): The currency code for logging. Defaults to "UNK".

    Returns:
        AccountState: An AccountState object containing all parsed account information.
    """
    flipclock_selector: str = "#faucet_countdown_clock .clock li.flip-clock-active"
    time_remaining: Optional[str] = None
    try:
        balance_element: Optional[ElementHandle] = page.query_selector(selector="span[class=user_balance]")
        wagered_element: Optional[ElementHandle] = page.query_selector(selector="b[id=total_wagered]")
        target_element: Optional[ElementHandle] = page.query_selector(selector="b[id=wagering_target]")
        remaining_claims_element: Optional[ElementHandle] = page.query_selector(
            selector="b[class=faucet_claims_remaining]"
        )
        free_spins_element: Optional[ElementHandle] = page.query_selector(selector="span[id=free_spins]")

        flipclock_locator: Locator = page.locator(flipclock_selector)

        def parse_flipclock(page: Page, flipclock_selector: str) -> Optional[str]:
            time_remaining = None
            # Wait for flipclock to be populated by JavaScript (max 3 seconds)
            page.wait_for_selector(flipclock_selector, timeout=3000, state="attached")

            # Query the flipclock digits from the live DOM
            active_digits_elements = page.query_selector_all(flipclock_selector)

            log.info("Found %d active digit elements from Page", len(active_digits_elements))

            if active_digits_elements and len(active_digits_elements) >= 4:
                # Extract text from each digit element
                digits_text = [elem.inner_text().strip() for elem in active_digits_elements[:4]]
                log.info("Digit texts from Page: %s", digits_text)

                try:
                    # Each element should contain a single digit
                    minute_tens = digits_text[0][0] if digits_text[0] else "0"
                    minute_ones = digits_text[1][0] if digits_text[1] else "0"
                    second_tens = digits_text[2][0] if digits_text[2] else "0"
                    second_ones = digits_text[3][0] if digits_text[3] else "0"

                    time_remaining = f"{minute_tens}{minute_ones}:{second_tens}{second_ones}"
                    log.info("Flipclock time remaining (from Page): %s (MM:SS)", time_remaining)
                except (IndexError, AttributeError) as e:
                    log.warning("Failed to parse flipclock digits from Page: %s", e)
            else:
                log.info("No active flipclock found - faucet may be ready to claim")

            return time_remaining

        if flipclock_locator.count() >= 0:
            time_remaining = parse_flipclock(page, flipclock_selector)

        balance_text = balance_element.text_content() if balance_element else "0.0"
        wagered_text = wagered_element.text_content() if wagered_element else "0.0"
        target_text = target_element.text_content() if target_element else "0.0"
        remaining_claims_text = remaining_claims_element.text_content() if remaining_claims_element else "0"
        free_spins_text = free_spins_element.text_content() if free_spins_element else "0"

        # Parse numeric values
        balance = float(balance_text.strip().replace(",", "").strip())
        wagered = float(wagered_text.strip())
        target = float(target_text.strip())
        remaining_claims = int(remaining_claims_text.strip())
        free_spins = int(free_spins_text.strip())

    except Exception as e:
        log.info("Could not find flipclock in live DOM (faucet likely ready): %s", str(e)[:100])
        return None

    return AccountState(
        balance=balance,
        wagered=wagered,
        target=target,
        remaining_claims=remaining_claims,
        free_spins=free_spins,
        time_remaining=time_remaining,
    )


def parse_account_state_res(res: Response, currency: str = "UNK") -> AccountState:
    """Parse the current account state from the faucet page response.

    Extracts balance, wagering progress, remaining claims, free spins, and countdown timer
    information from the page response.

    Args:
        res (Response): The Response object from the faucet page.
        currency (str, optional): The currency code for logging. Defaults to "UNK".

    Returns:
        AccountState: An AccountState object containing all parsed account information.
    """
    # Select all account state elements
    balance_selector = res.css(selector="span[class=user_balance]", identifier=f"balance_{currency}")
    wagered_selector = res.css(selector="b[id=total_wagered]", identifier=f"wagered_{currency}")
    target_selector = res.css(selector="b[id=wagering_target]", identifier=f"target_{currency}")
    remaining_claims_selector = res.css(
        selector="b[class=faucet_claims_remaining]", identifier=f"remaining_claims_{currency}"
    )
    free_spins_selector = res.css(selector="span[id=free_spins]", identifier=f"free_spins_{currency}")
    # countdown_selector = res.css(
    #     selector='div[id="faucet_countdown_clock"] li[class=flip-clock-active]', identifier=f"countdown_{currency}"
    # )
    countdown_selector = res.css(selector='div[id="faucet_countdown_clock"]', identifier=f"countdown_{currency}")

    # Extract text values with defaults
    balance_text = balance_selector.get().text if balance_selector.get() else "0.0"
    wagered_text = wagered_selector.get().text if wagered_selector.get() else "0.0"
    target_text = target_selector.get().text if target_selector.get() else "0.0"
    remaining_claims_text = remaining_claims_selector.get().text if remaining_claims_selector.get() else "0"
    free_spins_text = free_spins_selector.get().text if free_spins_selector.get() else "0"

    # Parse numeric values
    balance = float(balance_text.strip().replace(",", "").strip())
    wagered = float(wagered_text.strip())
    target = float(target_text.strip())
    remaining_claims = int(remaining_claims_text.strip())
    free_spins = int(free_spins_text.strip())

    # Parse countdown timer if active
    time_remaining: Optional[str] = None

    # If Page object is provided, use it to query the live DOM (with JavaScript-generated content)
    # Fallback to Response object parsing (may not work for JavaScript-generated content)
    countdown_element: Selector = countdown_selector.get()
    log.info("countdown_element exists: %s (using Response fallback)", countdown_element is not None)
    if countdown_element:
        # Parse the flipclock value - extract minutes and seconds from the flip clock
        active_digits_selector = res.css(
            selector="#faucet_countdown_clock li.flip-clock-active",
            identifier=f"flipclock_digits_{currency}",
        )

        active_digits = active_digits_selector.get_all()
        log.info("Found %d active digit elements (from Response)", len(active_digits) if active_digits else 0)

        if active_digits and len(active_digits) >= 4:
            # Extract the 4 digits: MM:SS
            try:
                minute_tens = active_digits[0].text.strip()[0]
                minute_ones = active_digits[1].text.strip()[0]
                second_tens = active_digits[2].text.strip()[0]
                second_ones = active_digits[3].text.strip()[0]

                time_remaining = f"{minute_tens}{minute_ones}:{second_tens}{second_ones}"
                log.info("Flipclock time remaining (from Response): %s (MM:SS)", time_remaining)
            except (IndexError, AttributeError) as e:
                log.warning("Failed to parse flipclock digits: %s", e)
        else:
            log.info("No flipclock digits found - faucet likely ready to claim")

    return AccountState(
        balance=balance,
        wagered=wagered,
        target=target,
        remaining_claims=remaining_claims,
        free_spins=free_spins,
        time_remaining=time_remaining,
    )


def google_oauth_login_page_make() -> Callable[[Page], None]:
    """Create a Google OAuth login page action.

    Returns:
        tuple[Callable[[Page], None], Callable[[], bool]]: A tuple containing:
            - The Google OAuth login page action function
            - A function that always returns False (Google OAuth doesn't auto-claim)
    """

    def google_login_page(page: Page):
        """
        Perform Google OAuth login on the given page.
        Args:
            page (Page): The Playwright page object.
        Returns:
            None
        """
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


def login_page_make(username: str, password: str, currency: str = "UNK") -> Callable[[Page], None]:
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
        """
        Perform login on the given page.
        Args:
            page (Page): The Playwright page object.
        Returns:
            None
        """
        _ = currency  # Force currency into closure for screenshot_action decorator
        login_button_selector = "button[id='process_login']"

        if "login" not in page.url:
            log.warning("Not on login page, current URL: %s", page.url)
            # log.warning("Attempting to click the claim button, assuming already logged in.")
            # page.click(button_selector)
            # claim_attempted["value"] = True
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

        page.click(login_button_selector, delay=gaussian_random_delay())

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
        currency (str, optional): The currency code. Defaults to "UNK".
    Returns:
        Callable[[Page], None]: The claim faucet action function.
    """

    @screenshot_action
    def claim_faucet(page: Page):
        """
        Perform claim action on the given page.
        Args:
            page (Page): The Playwright page object.
        Returns:
            None
        """
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


def pick_to_dict_json_safe(pick_obj: Pick) -> Dict[str, Any]:
    """Convert Pick object to JSON-safe dict

    Args:
        pick_obj (Pick): The Pick object to convert.
    Returns:
        Dict[str, Any]: The JSON-safe dict representation of the Pick object.
    """
    # Skipping the cooldown timer on purpose here because it's an ephemeral attribute.
    data = {
        "url": pick_obj.url,
        "currency": pick_obj.currency,
        "balance": pick_obj.balance,
        "wagered": pick_obj.wagered,
        "history": [
            (dt.isoformat(), balance, wagered, target, remaining_claims, free_spins)
            for dt, balance, wagered, target, remaining_claims, free_spins in pick_obj.history
        ],
        "last_update": pick_obj.last_update.isoformat() if pick_obj.last_update else None,
        "target": pick_obj.target,
        "remaining_claims": pick_obj.remaining_claims,
        "free_spins": pick_obj.free_spins,
        # "cooldown_timer": pick_obj.cooldown_timer  <-- NO!
    }
    return data


def json_to_pick(data: Dict[str, Any]) -> Pick:
    """Convert JSON-safe dict to Pick object.
    Args:
        data (Dict[str, Any]): The JSON-safe dict representation of the Pick object.
    Returns:
        Pick: The Pick object.
    """
    pick_obj = Pick(url=data["url"], currency=data["currency"])
    pick_obj.balance = data["balance"]
    pick_obj.wagered = data["wagered"]
    pick_obj.history = [
        (datetime.fromisoformat(dt), balance, wagered, target, remaining_claims, free_spins)
        for dt, balance, wagered, target, remaining_claims, free_spins in data["history"]
    ]
    pick_obj.last_update = datetime.fromisoformat(data["last_update"]) if data["last_update"] else None
    pick_obj.target = data["target"]
    pick_obj.remaining_claims = data.get("remaining_claims", 0)  # Use .get() for backward compatibility
    pick_obj.free_spins = data.get("free_spins", 0)  # Use .get() for backward compatibility
    return pick_obj


def save_picks(picks: list[Pick], directory: str = "picks_data"):
    """Save the picks to JSON files in the specified directory.
    Args:
        picks (list[Pick]): List of Pick objects to save.
        directory (str, optional): Directory to save the JSON files. Defaults to "picks_data".
    """
    os.makedirs(directory, exist_ok=True)
    for pick in picks:
        filename = f"{pick.get_env_prefix()}.json"
        filepath = os.path.join(directory, filename)
        pick.write(filepath)
        log.info("Saved pick data to %s", filepath)


def load_picks(directory: str = "picks_data") -> list[Pick]:
    """
    Load the picks from JSON files in the specified directory.
    Args:
        directory (str, optional): Directory to load the JSON files from. Defaults to
        "picks_data".
    Returns:
        list[Pick]: List of loaded Pick objects.
    """
    picks = []
    if not os.path.exists(directory):
        return picks

    for filename in os.listdir(directory):
        if filename.endswith(".json"):
            filepath = os.path.join(directory, filename)
            try:
                pick = Pick.read(filepath)
                picks.append(pick)
                log.info("Loaded pick data from %s", filepath)
            except Exception as e:
                log.error("Failed to load pick data from %s: %s", filepath, e)
    return picks


def check_logged_in(res: Response) -> bool:
    """
    Check if the user is logged in by looking for a logout link in the response.
    Args:
        res (Response): The response object to check.
    Returns:
        bool: True if logged in, False otherwise.
    """
    logout_link = res.css(selector="li[class='lg_logout_btn']", identifier="logout_btn")
    return logout_link.get() is not None


def summarize_picks(picks: List[Pick]):
    """
    Summarize the picks by printing a table of their current status.
    Args:
        picks (list[Pick]): List of Pick objects to summarize.
    """
    log.info(
        "{: <21} | {: <8} | {: >15} | {: >15} | {: >15} | {: >15} | {: >8} | {: >12} | {: >15}".format(
            "URL", "Currency", "Balance", "Wagered", "Target", "Diff", "Claims", "Bonus Spins", "Cooldown Timer"
        )
    )
    for pick in picks:
        log.info(pick)


def main(
    picks: List[Pick],
    proxy: str | None = None,
    use_google_oauth: bool = False,
    user_data_dir: str | None = None,
    headless: bool = False,
    skip_claim: bool = False,
    play_keno: bool = False,
    summarize: bool = False,
):
    """
    Main function to run the scraper.
    Args:
        picks (List[Pick], required): List of Pick objects to process. Defaults to [].
        proxy (str | None, optional): Proxy URL to use. Defaults to None.
        use_google_oauth (bool, optional): Whether to use Google OAuth for login. Defaults
        user_data_dir (str | None, optional): Path to user data directory. Defaults to None.
        headless (bool, optional): Whether to run in headless mode. Defaults to False.
        skip_claim (bool, optional): Whether to skip the claim step. Defaults to False.
        play_keno (bool, optional): Whether to play keno after claiming. Defaults to False.
        summarize (bool, optional): Whether to summarize the results. Defaults to False.

    Returns:
        None
    """
    finished_picks: List[Pick] = []
    while len(picks) > 0:
        pick = picks.pop(0)
        # Choose login method
        if use_google_oauth:
            login_page, claim_already_attempted = google_oauth_login_page_make()
        else:
            username, password = get_credentials(pick.url)
            login_page, claim_already_attempted = login_page_make(username, password, currency=pick.currency)

        additional_args = {}
        if user_data_dir is not None:
            additional_args["user_data_dir"] = user_data_dir
        else:
            additional_args = {}

        with StealthySession(
            proxy=proxy,
            headless=headless,
            humanize=True,
            solve_cloudflare=True,
            google_search=False,
            additional_args=additional_args,
        ) as session:
            try:
                login_response: Response = session.fetch(
                    f"{pick.url}login.php", page_action=login_page, wait=5000, timeout=30000
                )

                if check_logged_in(login_response):
                    finished_picks.append(pick)
                    log.info("Logged in to %s successfully", pick.url)
                else:
                    log.error("Failed to log in to %s", pick.url)
                    picks.append(pick)  # Re-add to the end of the list to try again later
                    continue

                # Check if Response object has a page attribute
                # page_obj = getattr(login_response, "page", None) or getattr(login_response, "_page", None)
                # log.info("Page object found in login_response: %s", page_obj is not None)
                # account_state = parse_account_state_res(login_response, currency=pick.currency, page=page_obj)
                # pick.update(account_state=account_state)
                log.debug("%s", pick)

                if skip_claim:
                    log.info("Skipping claim as per --skip-claim")
                    continue

                if claim_already_attempted():
                    log.info("Skipping claim - already attempted during login (user was already logged in)")
                    continue

                faucet = make_claim_faucet(button_selector, currency=pick.currency)
                _: Response = session.fetch(f"{pick.url}faucet.php", page_action=faucet, wait=5000, timeout=30000)

                log.info("About to play keno on %s", pick.url)
                # Check if Response object has a page attribute
                if play_keno:
                    _: Response = session.fetch(
                        f"{pick.url}keno.php", page_action=play_keno, timeout=0, solve_cloudflare=False
                    )

                log.info("Finished processing %s", pick.url)

            except Exception as e:
                log.error("Error fetching %s: %s", pick.url, e)
                continue

    if summarize:
        summarize_picks(finished_picks)

    save_picks(finished_picks)


def filter_picks(all_picks: List[Pick], only: List[str], skip: List[str]) -> List[Pick]:
    """
    Filter picks based on 'only' and 'skip' lists.
    Args:
        all_picks (List[Pick]): List of all Pick objects.
        only (List[str]): List of currency codes to run only.
        skip (List[str]): List of currency codes to skip.
    Returns:
        List[Pick]: Filtered list of Pick objects to run.
    """
    if only:
        to_run = [pick for pick in all_picks if pick.currency in only]
    elif skip:
        to_run = [pick for pick in all_picks if pick.currency not in skip]
    else:
        to_run = all_picks

    if not to_run:
        log.warning("No picks to run after applying --only/--skip filters.")
    return to_run


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--proxy", help="proxy url to use", default=None)
    parser.add_argument("--user-data-dir", help="path to user data directory", default=None)
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
    parser.add_argument(
        "--play-keno",
        help="play keno game after claiming the faucet",
        action="store_true",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--skip", help="List of picks by currency to skip", nargs="+", default=[])
    group.add_argument("--only", help="List of picks by currency to run only", nargs="+", default=[])

    args = parser.parse_args()

    all_picks = load_picks()
    if not all_picks:
        all_picks = default_picks()
    picks_to_run: List[Pick] = filter_picks(all_picks, args.only, args.skip)
    PICKS = {x.currency: x for x in picks_to_run}

    main(
        picks_to_run,
        args.proxy,
        args.google_oauth,
        args.user_data_dir,
        args.headless,
        args.skip_claim,
        args.play_keno,
        args.summarize,
    )
