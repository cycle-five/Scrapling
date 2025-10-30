from argparse import ArgumentParser
from dataclasses import dataclass
import pyotp
from typing import Callable, Optional, List
from playwright.sync_api import Page, Response as PlaywrightResponse, Locator, Error as PlaywrightError
from scrapling.fetchers import StealthySession
from scrapling.engines.toolbelt.custom import Response
from scrapling.cli import log
from scrapling_pick import get_credentials, gaussian_random_delay
import sys
import os


sys.path.append(".")


@dataclass
class CasinoAccountState:
    """Represents the current state of a Stake.us casino account."""

    sweeps_coins: float = 0.0  # SC balance
    gold_coins: float = 0.0  # GC balance
    vip_level: str = "None"  # VIP level (Bronze, Silver, Gold, Platinum, Diamond, etc.)
    vip_progress: Optional[float] = None  # Progress to next VIP level (0.0-1.0)

    def __str__(self) -> str:
        return f"SC: {self.sweeps_coins:.2f}, GC: {self.gold_coins:.2f}, VIP: {self.vip_level}"


class Currency:
    """Represents a currency with its associated selectors."""

    def __init__(
        self,
        name: str,
        code: str,
        selectors: List[str],
        is_active_selector: Optional[str] = None,
        activate_selector: Optional[str] = None,
    ):
        self.name = name
        self.code = code
        self.selectors = selectors
        self.is_active_selector = is_active_selector
        self.activate_selector = activate_selector



class CurrencyDisplayConfig:
    """Configuration for currency selectors."""

    def __init__(
        self,
        currencies: List[Currency],
        currency_toggle_dropdown_selector: Optional[str] = None,
        currency_toggle_switch_selector: Optional[str] = None,
    ):
        self.currencies = currencies
        self.currency_toggle_dropdown_selector = currency_toggle_dropdown_selector
        self.currency_toggle_switch_selector = currency_toggle_switch_selector


close_selectors = [
    "#close",
    "div#close",
    "[aria-label='Close']",
    "button[aria-label='Close']",
    ".close",
]


def make_get_casino_account_state(
    currency_display_config: CurrencyDisplayConfig,
) -> callable:
    """makes a function for getting the account state via a closure."""

    def get_casino_account_state(page: Page) -> CasinoAccountState:
        """Parse the casino account state from the page.

        Extracts Stake Cash balance, Gold Coins balance, and VIP level information.

        Args:
            page (Page): The Playwright Page object representing the casino page.

        Returns:
            CasinoAccountState: An object containing all parsed account information.
        """
        # Click to open the dropdown.
        if currency_display_config.currency_toggle_dropdown_selector:
            page.click(
                currency_display_config.currency_toggle_dropdown_selector,
                delay=gaussian_random_delay(),
            )

        sweeps_coins = 0.0
        gold_coins = 0.0
        vip_level = "None"
        vip_progress = None

        # Try to parse Stake Cash balance
        for currency in currency_display_config.currencies:
            for selector in currency.selectors:
                try:
                    element_selector = page.locator(selector)
                    if element_selector.count() > 0:
                        text = element_selector.first.text_content().strip()
                        # Remove currency symbols and parse
                        text = text.replace(currency.code, "").replace(",", "").strip()
                        try:
                            n = float(text)
                            log.info(f"Found {currency.name} balance: {n}")
                            if currency.code == "SC":
                                sweeps_coins = n
                            elif currency.code == "GC":
                                gold_coins = n
                            break
                        except ValueError:
                            continue
                except Exception as e:
                    log.debug(f"Selector {selector} failed for {currency.name}: {e}")
                    continue
            if currency_display_config.currency_toggle_switch_selector:
                # Switch to next currency in dropdown
                page.click(
                    currency_display_config.currency_toggle_switch_selector,
                    delay=gaussian_random_delay(),
                )

        # Click again to close the dropdown
        if currency_display_config.currency_toggle_dropdown_selector:
            page.click(
                currency_display_config.currency_toggle_dropdown_selector,
                delay=gaussian_random_delay(),
            )

        return CasinoAccountState(
            sweeps_coins=sweeps_coins,
            gold_coins=gold_coins,
            vip_level=vip_level,
            vip_progress=vip_progress,
        )

    return get_casino_account_state


def make_handle_google_one_tap_popup(close_selectors: List[str]) -> Callable[[Page], None]:
    """Generator for handling the google one tap popup

    close
    """
    _ = close_selectors

    def handle_google_one_tap_popup(page: Page) -> None:
        """Handle and close Google One Tap popup if it appears.
        Args:
            page (Page): The Playwright page object.
        """
        # Try to close the Google One Tap popup if it appears
        try:
            # Wait a bit for the Google iframe to load
            page.wait_for_timeout(1500)

            # Check if the Google One Tap container exists
            google_container = page.locator("div#credential_picker_container")
            if google_container.count() > 0:
                log.info("Google One Tap popup detected, attempting to close it")

                # Get the iframe using frame_locator
                iframe = page.frame_locator("div#credential_picker_container >> iframe")

                # Try to find and click the close button inside the iframe
                clicked = False
                for selector in close_selectors:
                    try:
                        # Check if element exists and click it
                        close_button = iframe.locator(selector)
                        if close_button.count() > 0:
                            close_button.click(timeout=3000, delay=gaussian_random_delay())
                            log.info(f"Closed Google popup using selector: {selector}")
                            clicked = True
                            break
                    except Exception as e:
                        log.debug(f"Selector {selector} failed: {e}")
                        continue

                if not clicked:
                    log.warning("Could not find close button in iframe")
            else:
                log.info("No Google One Tap popup detected")
        except Exception as e:
            log.warning("Could not close Google popup (critical): %s", str(e))
            # Do raise - this is not optional, login doesn't work otherwise
            raise e

    return handle_google_one_tap_popup


def wait_for_load_all_safe(page: Page, timeout: int = 500) -> None:
    """Wait for the page to be fully loaded with error handling.
    Args:
        page (Page): The Playwright page object.
        timeout (int): Maximum wait time in milliseconds.
    """
    try:
        # These two are not usually problematic so we don't impose a timeout.
        page.wait_for_load_state("load")
        page.wait_for_load_state("domcontentloaded")
        # This can be considered just a wait for `timeout` ms if networkidle doesn't happen.
        # Which should be considered the most likely case.
        try:
            page.wait_for_load_state("networkidle", timeout=timeout)
        except PlaywrightError as _:
            # Sometimes networkidle doesn't happen, ignore
            pass
    except PlaywrightError as e:
        log.warning("Page did not fully load within timeout: %s", str(e))


def make_modal_tab_button(
    # modal selector, usually wallet button
    modal_selector='button[data-testid="wallet"], button[data-analytics="global-navbar-wallet-button"]',
    # tab selector, usually daily bonus button
    tab_selector='button[data-testid="dailyBonus"]',
    # button selector, usually claim button
    btn_selector="button.justify-center:nth-child(4)",
    # close modal selector
    close_btn_selector='button[data-testid="modal-close"]',
) -> Callable[[Page], None]:
    """Generator for claiming daily bonus via modal, tab, button pattern.
    Args:
        modal_selector (str): Selector for the modal open button.
        tab_selector (str): Selector for the tab button inside the modal.
        btn_selector (str): Selector for the claim button.
        close_btn_selector (str): Selector for the modal close button.
    Returns:
        Callable[[Page], None]: A function that performs the daily bonus claim action on the given page.
    """
    log.debug(
        "selectors: %s, %s, %s, %s",
        modal_selector,
        tab_selector,
        btn_selector,
        close_btn_selector,
    )

    def claim_daily_bonus(page: Page) -> None:
        """Clicks to claim the daily bonus if available.
        Args:
            page (Page): The Playwright page object.

        Returns:
        """

        try:
            page.click(modal_selector, delay=gaussian_random_delay(), timeout=5000)
            page.click(tab_selector, delay=gaussian_random_delay(), timeout=5000)
            claim_btn = page.locator(btn_selector)
            if claim_btn.is_disabled():
                log.info("Daily bonus already claimed.")
            else:
                claim_btn.click(delay=gaussian_random_delay(), timeout=5000)
                wait_for_load_all_safe(page, timeout=3000)
        except PlaywrightError as e:
            log.error("Exception occurred while claiming daily bonus: %s", str(e))
        finally:
            # Close the wallet modal if it's still open
            try:
                close_btn = page.locator(close_btn_selector)
                if close_btn.count() > 0:
                    close_btn.click(delay=gaussian_random_delay(), timeout=10000)
            except PlaywrightError:
                pass

    return claim_daily_bonus


# MTB
#
# Modal,    Tab, Button; (Button)
# Click,  Click,  Click;  (Click)
#  Open, Switch,  Claim;   (Exit)
#
# Interface Objects
# User Interaction
# Affect Effected
#
def make_login_action_factory(
    username_selector: str,
    password_selector: str,
    login_submit_selector: str,
    totp_code_selector: Optional[str] = None,
    totp_submit_selector: Optional[str] = None,
    pre_login_form_callback: Optional[Callable[[Page], None]] = make_handle_google_one_tap_popup(close_selectors),
    post_login_form_callback: Optional[Callable[[Page], None]] = None,
) -> Callable[[str, str, Optional[str]], Callable[[Page], None]]:
    """Generator for creating a login action factory.
    Args:
        username_selector (str): Selector for the username input field.
        password_selector (str): Selector for the password input field.
        login_submit_selector (str): Selector for the login submit button.
        totp_code_selector (str): Selector for the TOTP code input field.
        pre_login_form_callback (Optional[Callable[[Page], None]]): Optional callback for handling
            any pre-login forms or popups.
        post_login_form_callback (Optional[Callable[[Page], None]]): Optional callback for handling
            any post-login forms.
    Returns:
        Callable[[str, str, Optional[str]], None]: A function that creates a login action.
    """

    def login_action_factory(username: str, password: str, totp_secret: Optional[str]) -> Callable[[Page], None]:
        """Create a login page action.

        Args:
            username (str): Username for login.
            password (str): Password for login.
            totp_secret (Optional[str]): TOTP secret for 2FA, if applicable.

        Returns:
            Callable[[Page], None]: A function that performs the login action on the given page.
        """

        def login_action(page: Page):
            # Initial page load handling popups / etc
            if pre_login_form_callback:
                pre_login_form_callback(page)

            # Fill in login form and submit
            page.fill(username_selector, username)
            page.fill(password_selector, password)

            try:
                page.click(login_submit_selector, delay=gaussian_random_delay(), timeout=10000)
            except Exception as e:
                log.error("Error during login: %s", str(e))
                raise e

            # Wait for navigation to complete
            wait_for_load_all_safe(page)

            # Optional post-login form handling
            if post_login_form_callback:
                post_login_form_callback(page)

            # Handle TOTP 2FA if applicable
            if totp_secret and totp_code_selector and totp_submit_selector:
                try:
                    page.wait_for_selector(totp_code_selector, state="visible", timeout=10000)
                    totp_code = pyotp.TOTP(totp_secret).now()
                    page.fill(totp_code_selector, totp_code)
                    page.click(totp_submit_selector, delay=gaussian_random_delay())
                except Exception as e:
                    log.error("Error during TOTP entry: %s", str(e))
                    raise e

            # Wait for navigation to complete
            wait_for_load_all_safe(page)

        return login_action

    return login_action_factory


def get_arg_parser(description: str = "Generic Daily Bonus Claimer") -> ArgumentParser:
    """Get argument parser for command-line options.
    Args:
        description (str): Description for the argument parser
    Returns:
        ArgumentParser: Configured argument parser
    """
    parser = ArgumentParser(description=description)
    parser.add_argument(
        "--proxy",
        type=str,
        default=None,
        help="Proxy server to use (e.g., http://user:pass@host:port)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (no GUI)",
    )
    parser.add_argument(
        "--google-oauth",
        action="store_true",
        help="Enable Google OAuth handling (if applicable)",
    )
    parser.add_argument(
        "--skip-claim",
        action="store_true",
        help="Skip claiming the daily bonus",
    )
    parser.add_argument(
        "--user-data-dir",
        type=str,
        default=None,
        help="Path to user data directory for browser session",
    )
    return parser
