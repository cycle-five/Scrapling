from argparse import ArgumentParser
from typing import Optional, Callable, List, Dict
import sys
import os
import pyotp
import re

from scrapling.engines.toolbelt.custom import Response
from scrapling.fetchers import StealthySession
from playwright.sync_api import Page, Error as PlaywrightError, Locator, ElementHandle
from playwright.sync_api import expect
from playwright._impl._errors import TimeoutError, TargetClosedError
from casino import (
    get_credentials,
    gaussian_random_delay,
    get_arg_parser,
    wait_for_load_all_safe,
    log,
    make_handle_google_one_tap_popup,
    make_login_action_factory,
    CurrencyDisplayConfig,
    Currency,
    make_generic_accept_or_close_modals,
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
modal_selector = "section.dailyBonus_page"
close_modal_selector = "section.dailyBonus_page .commonAlert_close"

buy_btn_selector = "li.tw-hidden:nth-child(2) > p:nth-child(1)"
daily_bonus_tab_selector = ".tw-self-center > div:nth-child(1) > div:nth-child(1) > div:nth-child(5)"
claim_daily_bonus_selector = "button.el-button--primary:not(.is-disabled)"
claim_daily_bonus_disabled_selector = "button.el-button--primary:is-disabled"

# Balance selectors - update sweeps_coins_selector once you identify the correct class
gold_coins_selectors = [".gold_color .amount"]
sweeps_coins_selectors = [".sweeps_color .amount", ".sc_color .amount", "[class*='sweeps'] .amount"]

currency_display_config = CurrencyDisplayConfig(
    currencies=[
        Currency(name="Sweeps Coins", code="SC", selectors=sweeps_coins_selectors, activate_selector=".sweeps_color"),
        Currency(name="Gold Coins", code="GC", selectors=gold_coins_selectors, activate_selector=".gold_color"),
    ],
    currency_toggle_dropdown_selector=None,
    currency_toggle_switch_selector=None,
)


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
    currency_switcher_selector = ".currency-disabled"
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


login_action_factory: Callable[[str, str, Optional[str]], Callable[[Page], None]] = make_login_action_factory(
    username_selector=username_selector,
    password_selector=password_selector,
    login_submit_selector=login_submit_selector,
    totp_code_selector=totp_code_selector,
    totp_submit_selector=twofa_submit_selector,
    pre_login_form_callback=lambda page: page.click(
        'div[id="tab-login"]', delay=gaussian_random_delay(), timeout=10000
    ),
)


def main(
    headless: bool = False,
    google_oauth: bool = False,
    skip_claim: bool = False,
    proxy: Optional[str] = None,
    user_data_dir: Optional[str] = None,
):
    # Get LuckyBird credentials
    username, password, totp_secret = get_credentials("https://luckybird.io", twofa=True)

    additional_args = {}
    if user_data_dir is not None:
        additional_args["user_data_dir"] = user_data_dir

    login_action: Callable[[Page], None] = login_action_factory(username, password, totp_secret)
    accept_or_claim_modals: Callable[[Page], bool] = make_generic_accept_or_close_modals(
        main_enabled_selector, modal_selector, close_modal_selector
    )

    def luckybird_action(page: Page) -> None:
        # Perform login
        login_action(page)
        wait_for_load_all_safe(page)

        # Claim daily bonus unless skipped
        if not skip_claim:
            claimed = accept_or_claim_modals(page)
            if claimed:
                log.info("Daily bonus claimed successfully.")
            else:
                log.info("No daily bonus available to claim.")

        # Parse and log coin balances
        balances = parse_coin_balances(page)
        log.info("Final Coin Balances: %s", balances)

    with StealthySession(
        proxy=proxy,
        headless=headless,
        humanize=True,
        load_dom=True,
        google_search=False,
        additional_args=additional_args,
    ) as session:
        # Login to LuckyBird and claim daily bonus
        _: Response = session.fetch(login_url, page_action=luckybird_action, wait=5000, timeout=60000)


if __name__ == "__main__":
    parser = get_arg_parser(description="LuckyBird Daily Bonus Claimer")
    args = parser.parse_args()

    main(**vars(args))
