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
    ):
        self.name = name
        self.code = code
        self.selectors = selectors


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


def make_get_casino_account_state(
    page: Page,
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


def make_claim_daily_bonus(
    wallet_btn_selector='button[data-testid="wallet"], button[data-analytics="global-navbar-wallet-button"]',
    daily_bonus_btn_selector='button[data-testid="dailyBonus"]',
    claim_btn_selector="button.justify-center:nth-child(4)",
    close_btn_selector='button[data-testid="modal-close"]',
) -> Callable[[Page], None]:
    log.debug(
        "selectors: %s, %s, %s, %s",
        wallet_btn_selector,
        daily_bonus_btn_selector,
        claim_btn_selector,
        close_btn_selector,
    )

    def claim_daily_bonus(page: Page) -> None:
        """Clicks to claim the daily bonus if available.
        Args:
            page (Page): The Playwright page object.

        Returns:
        """

        try:
            page.click(wallet_btn_selector, delay=gaussian_random_delay(), timeout=5000)
            page.click(daily_bonus_btn_selector, delay=gaussian_random_delay(), timeout=5000)
            claim_btn = page.locator(claim_btn_selector)
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
def make_mtb_action_factory(
    username_selector: str,
    password_selector: str,
    login_submit_selector: str,
    totp_code_selector: str,
    sweeps_coins_selectors: str,
    gold_coins_selectors: str,
    close_selectors: List[str],
    currency_toggle_dropdown_selector: Optional[str],
    currency_toggle_switch_selector: Optional[str],
    # make_claim_daily_bonus (defaults for stake.us)
    wallet_btn_selector: str = 'button[data-testid="wallet"], button[data-analytics="global-navbar-wallet-button"]',
    daily_bonus_btn_selector: str = 'button[data-testid="dailyBonus"]',
    claim_btn_selector: str = "button.justify-center:nth-child(4)",
    close_btn_selector: str = 'button[data-testid="modal-close"]',
) -> Callable[[str, str, Optional[str]], None]:
    def make_mtb_action(username: str, password: str, totp_secret: Optional[str]) -> Callable[[Page], None]:
        """Create a login page action.

        Args:
            username (str): Username for login.
            password (str): Password for login.
            totp_secret (Optional[str]): TOTP secret for 2FA, if applicable.

        Returns:
            Callable[[Page], None]: A function that performs the login action on the given page.
        """

        def mtb_action(page: Page):
            # Initial page load handling popups / etc
            handle_google_one_tap_popup = make_handle_google_one_tap_popup(close_selectors)
            handle_google_one_tap_popup(page)

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

            # Handle TOTP 2FA if applicable
            if totp_secret:
                try:
                    page.wait_for_selector(totp_code_selector, state="visible", timeout=10000)
                    totp_code = pyotp.TOTP(totp_secret).now()
                    page.fill(totp_code_selector, totp_code)
                    page.click(login_submit_selector, delay=gaussian_random_delay())
                except Exception as e:
                    log.error("Error during TOTP entry: %s", str(e))
                    raise e

            # Wait for navigation to complete
            wait_for_load_all_safe(page)

            # Check the login was successful and get balances
            currency_display_config = CurrencyDisplayConfig(
                currencies=[
                    Currency(name="Sweeps Coins", code="SC", selectors=sweeps_coins_selectors),
                    Currency(name="Gold Coins", code="GC", selectors=gold_coins_selectors),
                ],
                currency_toggle_dropdown_selector=currency_toggle_dropdown_selector,
                currency_toggle_switch_selector=currency_toggle_switch_selector,
            )
            get_casino_account_state = make_get_casino_account_state(
                page,
                currency_display_config,
            )
            casino_account_state: CasinoAccountState = get_casino_account_state(page)
            log.info("Login successful. Account State: %s", casino_account_state)

            make_claim_daily_bonus(
                wallet_btn_selector=wallet_btn_selector,
                daily_bonus_btn_selector=daily_bonus_btn_selector,
                claim_btn_selector=claim_btn_selector,
                close_btn_selector=close_btn_selector,
            )(page)

        return mtb_action

    return make_mtb_action


# def main(proxy: Optional[str], google_oauth: bool = False, user_data_dir: Optional[str] = None)
#     username, password = get_credentials("https://stake.us")
#     totp_secret = os.getenv("STAKE_2FA")  # Replace with actual TOTP secret if available
#     action_args = {"username": username, "password": password, "totp_secret": totp_secret}
#     make_mtb_action = make_mtb_action_factory()

#     additional_args = {}
#     if user_data_dir is not None:
#         additional_args["user_data_dir"] = user_data_dir

#     with StealthySession(
#         proxy=proxy,
#         headless=False,
#         humanize=True,
#         load_dom=True,
#         google_search=False,
#         additional_args=additional_args,
#     ) as session:
#         _: Response = session.fetch(
#             login_url,
#             page_action=make_mtb_action(**action_args),
#             wait=5000,
#         )


# if __name__ == "__main__":
#     parser = ArgumentParser()
#     parser.add_argument("--proxy", help="proxy url to use", default=None)
#     parser.add_argument(
#         "--google-oauth",
#         action="store_true",
#         help="use Google OAuth login instead of username/password",
#     )
#     parser.add_argument(
#         "--user-data-dir", type=str, help="Path to user data directory for browser session", default=None
#     )
#     args = parser.parse_args()

#     main(**vars(args))
