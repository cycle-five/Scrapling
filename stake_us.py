from argparse import ArgumentParser
from dataclasses import dataclass
import pyotp
from typing import Callable, Optional
from playwright.sync_api import (
    Page,
    Response as PlaywrightResponse,
    Locator,
    Error as PlaywrightError,
)
from scrapling.fetchers import StealthySession
from scrapling.engines.toolbelt.custom import Response
from scrapling.cli import log
from scrapling_pick import get_credentials, gaussian_random_delay
import sys
import os

from casino import (
    make_get_casino_account_state,
    CasinoAccountState,
    make_handle_google_one_tap_popup,
    make_mtb_action_factory,
    wait_for_load_all_safe,
)


url = "https://stake.us"
login_url = "https://stake.us/?tab=login&modal=auth"
username_selector = 'input[name="emailOrName"]'
password_selector = 'input[name="password"]'
totp_code_selector = 'input[name="code"]'
login_submit_selector = 'button[type="submit"]'

currency_toggle_selector = 'button[data-testid="coin-toggle"]'
sweeps_coins_selectors = [
    '[data-testid="coin-toggle-currency-sweeps"]',
]
gold_coins_selectors = [
    '[data-testid="coin-toggle-currency-gold"]',
]
# Common selectors for Google One Tap close button
close_selectors = [
    "#close",
    "div#close",
    "[aria-label='Close']",
    "button[aria-label='Close']",
    ".close",
]

# call factory for our generator
make_mtb_action = make_mtb_action_factory(
    username_selector=username_selector,
    password_selector=password_selector,
    login_submit_selector=login_submit_selector,
    totp_code_selector=totp_code_selector,
    sweeps_coins_selectors=sweeps_coins_selectors,
    gold_coins_selectors=gold_coins_selectors,
    close_selectors=close_selectors,
    currency_toggle_dropdown_selector=currency_toggle_selector,
    currency_toggle_switch_selector=None,
)

sys.path.append(".")


def claim_daily_bonus(page: Page) -> None:
    """Clicks to claim the daily bonus if available.
    Args:
        page (Page): The Playwright page object.

    Returns:
    """
    wallet_btn_selector = 'button[data-testid="wallet"], button[data-analytics="global-navbar-wallet-button"]'
    daily_bonus_btn_selector = 'button[data-testid="dailyBonus"]'
    claim_btn_selector = "button.justify-center:nth-child(4)"
    close_btn_selector = 'button[data-testid="modal-close"]'

    try:
        page.click(wallet_btn_selector, delay=gaussian_random_delay(), timeout=5000)
        page.click(
            daily_bonus_btn_selector, delay=gaussian_random_delay(), timeout=5000
        )
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


def main(
    proxy: Optional[str],
    google_oauth: bool = False,
    user_data_dir: Optional[str] = None,
):
    username, password = get_credentials("https://stake.us")
    totp_secret = os.getenv("STAKE_2FA")  # Replace with actual TOTP secret if available
    action_args = {
        "username": username,
        "password": password,
        "totp_secret": totp_secret,
    }
    # mtb_action = make_mtb_action(**action_args)

    additional_args = {}
    if user_data_dir is not None:
        additional_args["user_data_dir"] = user_data_dir

    with StealthySession(
        proxy=proxy,
        headless=False,
        humanize=True,
        load_dom=True,
        google_search=False,
        additional_args=additional_args,
    ) as session:
        _: Response = session.fetch(
            login_url,
            page_action=make_mtb_action(**action_args),
            wait=5000,
        )


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--proxy", help="proxy url to use", default=None)
    parser.add_argument(
        "--google-oauth",
        action="store_true",
        help="use Google OAuth login instead of username/password",
    )
    parser.add_argument(
        "--user-data-dir",
        type=str,
        help="Path to user data directory for browser session",
        default=None,
    )
    args = parser.parse_args()

    main(**vars(args))
