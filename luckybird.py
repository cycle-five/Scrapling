from argparse import ArgumentParser
from typing import Optional, Callable, List, Dict
import sys
import os
import pyotp
import re

sys.path.append(".")

from scrapling.engines.toolbelt.custom import Response
from scrapling.fetchers import StealthySession
from playwright.sync_api import Page, Error as PlaywrightError, Locator, ElementHandle
from playwright._impl._errors import TimeoutError, TargetClosedError
from casino import (
    get_credentials,
    gaussian_random_delay,
    wait_for_load_all_safe,
    log,
    make_handle_google_one_tap_popup,
)

url = "https://luckybird.io/"
login_url = "https://luckybird.io/"
username_selector = (
    "form.el-form:nth-child(2) > div:nth-child(1) > div:nth-child(2) > div:nth-child(1) > input:nth-child(1)"
)
password_selector = (
    "form.el-form:nth-child(2) > div:nth-child(2) > div:nth-child(2) > div:nth-child(1) > input:nth-child(1)"
)
totp_code_selector = ".loginTwoFactor_input > input:nth-child(1)"
login_submit_selector = "button.tw-mt-10"
twofa_submit_selector = ".loginTwoFactor_button"
# Common selectors for Google One Tap close button
close_selectors = [
    "#close",
    "div#close",
    "[aria-label='Close']",
    "button[aria-label='Close']",
    ".close",
]
# daily claim selectors
main_enabled_selector = "section.dailyBonus_page button.el-button--primary:enabled"
buy_btn_selector = "li.tw-hidden:nth-child(2) > p:nth-child(1)"
daily_bonus_tab_selector = ".tw-self-center > div:nth-child(1) > div:nth-child(1) > div:nth-child(5)"
claim_daily_bonus_selector = "button.el-button--primary:not(.is-disabled)"
claim_daily_bonus_disabled_selector = "button.el-button--primary:is-disabled"

# Balance selectors - update sweeps_coins_selector once you identify the correct class
gold_coins_selector = ".gold_color .amount"
sweeps_coins_selector = ".sweeps_color .amount, .sc_color .amount, [class*='sweeps'] .amount"


def luckybird_mtb(page: Page) -> bool:
    """Claim the daily bonus from the MTB section on LuckyBird.io.

    Args:
        page: The Playwright Page object
    Returns:
        bool: True if claim was successful, False otherwise
    """
    from casino import make_modal_tab_button

    mtb = make_modal_tab_button(
        modal_selector=buy_btn_selector,
        tab_selector=daily_bonus_tab_selector,
        btn_selector=claim_daily_bonus_selector,
        close_btn_selector=",".join(close_selectors),
    )
    mtb(page)

def highlight_element_handle(element: ElementHandle):
    """Highlight an element on the page for debugging purposes.

    Args:
        element: The Playwright ElementHandle object
    """
    try:
        element.evaluate(
            """(element) => {
                element.style.border = '3px solid red';
                # element.style.backgroundColor = 'yellow';
                setTimeout(() => { element.style.border = ''; }, 10000);
            }"""
        )
        log.info("Highlighted element with selector: %s", element)
    except PlaywrightError as e:
        log.error("Error highlighting element: %s", str(e))



def highlight_element(page: Page, selector: str):
    """Highlight an element on the page for debugging purposes.

    Args:
        page: The Playwright Page object
        selector: The CSS selector of the element to highlight
    """
    try:
        element: Locator = page.locator(selector).first
        if element.count() > 0:
            element.evaluate(
                """(element) => {
                    element.style.border = '3px solid red';
                    # element.style.backgroundColor = 'yellow';
                    setTimeout(() => { element.style.border = ''; }, 10000);
                }"""
            )
            log.info("Highlighted element with selector: %s", selector)
        else:
            log.warning("No element found to highlight with selector: %s", selector)
    except PlaywrightError as e:
        log.error("Error highlighting element: %s", str(e))

def luckybird_daily_initial_popups(page: Page) -> bool:
    """Claim the daily bonus on LuckyBird.io.

    Args:
        page: The Playwright Page object

    Returns:
        bool: True if claim was successful, False otherwise
    """
    # claim_button_selector = "section.dailyBonus_page button.el-button--primary:not(.is-disabled)"
    # modal_selector = "section.commonAlert_page"
    modal_selector = "section.dailyBonus_page"
    close_modal_selector = "section.dailyBonus_page .commonAlert_close"

    try:
        # Wait for the daily bonus modal to appear
        log.info("Waiting for daily bonus modal to appear...")
        page.wait_for_selector(modal_selector, state="visible", timeout=3000)
    except PlaywrightError as e:
        log.warning("Initial alerts popups timed out: %s", str(e))
        return False

    try:
        # Find the claim button that is not disabled
        # enabled_buttons: Locator = page.locator(main_enabled_selector)
        enabled_buttons: List[ElementHandle] = page.query_selector_all(main_enabled_selector)

        n = 0
        while enabled_buttons.count() > 0:
            n += 1
            if n > 3:
                break

            # Click the first available claim button
            log.info("Found enabled button, clicking...")
            highlight_element_handle(enabled_buttons)
            enabled_buttons.first.click(delay=gaussian_random_delay(), timeout=5000, force=True)

            # enabled_buttons: Locator = page.locator(main_enabled_selector)
            enabled_buttons: List[ElementHandle] = page.query_selector_all(main_enabled_selector)
        # Wait for the claim to process
        wait_for_load_all_safe(page, timeout=3000)

        log.info("Successfully claimed daily bonus!")
        return True
    except PlaywrightError as e:
        log.error("Error clicking button for daily: %s", str(e))

    # Try to close the modal if it's still open
    try:
        close_button = page.locator(close_modal_selector)
        if close_button.count() > 0:
            close_button.click(delay=gaussian_random_delay(), timeout=3000)
            log.info("Closed daily bonus modal")
    except PlaywrightError:
        pass


def parse_coin_balances(page: Page) -> Dict[str, Optional[float]]:
    """Parse gold and sweeps coins balances from the LuckyBird page.

    The page toggles between showing GC (Gold Coins) and SC (Sweeps Coins).
    This function clicks to switch between them and captures both values.

    Args:
        page: The Playwright Page object

    Returns:
        Dict with 'gold_coins' and 'sweeps_coins' keys containing float values or None if not found
    """
    balances = {"gold_coins": None, "sweeps_coins": None}

    # Pattern to extract numeric values (handles formats like "1,234.56" or "1234.56")
    number_pattern = re.compile(r"[\d,]+\.?\d*")

    # Currency switcher selector
    currency_switcher_selector = ".tw-currency-img-new"
    active_amount_selector = ".currency-active .amount"

    try:
        # First, get the currently active currency amount
        active_element = page.locator(active_amount_selector).first
        if active_element.count() > 0:
            text = active_element.text_content()
            if text:
                match = number_pattern.search(text)
                if match:
                    value_str = match.group().replace(",", "")
                    first_value = float(value_str)

                    # Determine which currency is currently active
                    gold_active = page.locator(".gold_color.currency-active").count() > 0

                    if gold_active:
                        balances["gold_coins"] = first_value
                        log.info("Found Gold Coins balance: %s", balances["gold_coins"])
                    else:
                        balances["sweeps_coins"] = first_value
                        log.info("Found Sweeps Coins balance: %s", balances["sweeps_coins"])

        # Now click to toggle to the other currency
        try:
            # Click on the currency switcher area
            switcher = page.locator(currency_switcher_selector).first
            switcher.click(delay=gaussian_random_delay(), timeout=3000)

            # Wait a moment for the toggle
            page.wait_for_timeout(500)

            # Get the newly active currency amount
            active_element = page.locator(active_amount_selector).first
            if active_element.count() > 0:
                text = active_element.text_content()
                if text:
                    match = number_pattern.search(text)
                    if match:
                        value_str = match.group().replace(",", "")
                        second_value = float(value_str)

                        # Determine which currency is now active
                        gold_active = page.locator(".gold_color.currency-active").count() > 0

                        if gold_active:
                            balances["gold_coins"] = second_value
                            log.info("Found Gold Coins balance: %s", balances["gold_coins"])
                        else:
                            balances["sweeps_coins"] = second_value
                            log.info("Found Sweeps Coins balance: %s", balances["sweeps_coins"])

            # Optional: Click again to restore original currency display
            switcher.click(delay=gaussian_random_delay(), timeout=3000)

        except Exception as e:
            log.warning("Error toggling currency display: %s", str(e))

    except Exception as e:
        log.error("Error parsing coin balances: %s", str(e))

    return balances


def make_luckybird_action(
    # close initial pops
    close_selectors: List[str],
    # login
    username: str,
    password: str,
    totp_secret: Optional[str],
    # options
    skip_claim: bool = False,
) -> Callable[[Page], None]:
    """Create a luckybird page action that logs in and claims daily bonus.

    Args:
        username: Login username for LuckyBird
        password: Login password for LuckyBird
        totp_secret: Optional TOTP secret for 2FA

    Returns:
        Callable page action function
    """

    def luckybird_action(page: Page):
        # Handle any popups that might appear
        try:
            # make_handle_google_one_tap_popup(close_selectors)(page)
            page.click('div[id="tab-login"]', delay=gaussian_random_delay(), timeout=10000)
        except Exception:
            # Google popup handling is optional for luckybird
            pass

        # Fill in login form and submit
        log.info("Logging into LuckyBird.io...")
        page.fill(username_selector, username)
        page.fill(password_selector, password)

        try:
            page.click(login_submit_selector, delay=gaussian_random_delay(), timeout=10000)
        except Exception as e:
            log.error("Error during login: %s", str(e))
            raise e

        # Handle TOTP 2FA if applicable
        if totp_secret:
            try:
                page.wait_for_selector(totp_code_selector, state="visible", timeout=10000)
                totp_code = pyotp.TOTP(totp_secret).now()
                page.fill(totp_code_selector, totp_code)
                page.click(twofa_submit_selector, delay=gaussian_random_delay(), timeout=10000)
            except Exception as e:
                log.error("Error during TOTP entry: %s", str(e))
                raise e

        # Wait for navigation to complete
        wait_for_load_all_safe(page)
        log.info("Successfully logged into LuckyBird.io")

        # Parse and log current coin balances
        balances = parse_coin_balances(page)
        log.info(
            "Current balances - Gold Coins: %s, Sweeps Coins: %s",
            balances["gold_coins"],
            balances["sweeps_coins"],
        )

        # Claim the daily bonus
        if not skip_claim:
            claimed = luckybird_daily_initial_popups(page)
            if not claimed:
                luckybird_mtb(page)

    return luckybird_action


def main(
    proxy: Optional[str],
    headless: bool = False,
    google_oauth: bool = False,
    skip_claim: bool = False,
    user_data_dir: Optional[str] = None,
):
    # Get LuckyBird credentials
    username, password = get_credentials("https://luckybird.io")
    totp_secret = os.getenv("LUCKYBIRD_2FA")

    additional_args = {}
    if user_data_dir is not None:
        additional_args["user_data_dir"] = user_data_dir

    with StealthySession(
        proxy=proxy,
        headless=headless,
        humanize=True,
        load_dom=True,
        google_search=False,
        additional_args=additional_args,
    ) as session:
        # Login to LuckyBird and claim daily bonus
        _: Response = session.fetch(
            login_url,
            page_action=make_luckybird_action(close_selectors, username, password, totp_secret, skip_claim),
            wait=5000,
        )

def get_arg_parser(description: str = "Generic Daily Bonus Claimer") -> ArgumentParser:
    """Get argument parser for command-line options.
    Args:
        description: Description for the argument parser
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

if __name__ == "__main__":
    parser = get_arg_parser(description="LuckyBird Daily Bonus Claimer")
    args = parser.parse_args()

    main(**vars(args))
