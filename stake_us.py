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

# sys.path.append(".")

from casino import (
    get_arg_parser,
    CasinoAccountState,
    CurrencyDisplayConfig,
    Currency,
    make_get_casino_account_state,
    make_modal_tab_button,
    make_login_action_factory,
    make_handle_google_one_tap_popup,
    # make_casino_action_factory,
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
wallet_btn_selector = 'button[data-testid="wallet"], button[data-analytics="global-navbar-wallet-button"]'
daily_bonus_btn_selector = 'button[data-testid="dailyBonus"]'
claim_btn_selector = "button.justify-center:nth-child(4)"
close_btn_selector = 'button[data-testid="modal-close"]'

login_action_factory = make_login_action_factory(
    username_selector=username_selector,
    password_selector=password_selector,
    login_submit_selector=login_submit_selector,
    totp_code_selector=totp_code_selector,
    totp_submit_selector=login_submit_selector,
)

# define currency display configuration
currency_display_config = CurrencyDisplayConfig(
    currencies=[
        Currency(name="Sweeps Coins", code="SC", selectors=sweeps_coins_selectors),
        Currency(name="Gold Coins", code="GC", selectors=gold_coins_selectors),
    ],
    currency_toggle_dropdown_selector=currency_toggle_selector,
    currency_toggle_switch_selector=None,
)
# create the get account state function using the factory
get_casino_account_state = make_get_casino_account_state(
    currency_display_config,
)

claim_bonus_action = make_modal_tab_button(
    modal_selector=wallet_btn_selector,
    tab_selector=daily_bonus_btn_selector,
    btn_selector=claim_btn_selector,
    close_btn_selector=close_btn_selector,
)


def main(
    proxy: Optional[str],
    headless: Optional[bool] = False,
    google_oauth: Optional[bool] = False,
    skip_claim: Optional[bool] = False,
    user_data_dir: Optional[str] = None,
):
    headless = headless if headless is not None else False
    username, password, totp_secret = get_credentials("https://stake.us")
    action_args = {
        "username": username,
        "password": password,
        "totp_secret": totp_secret,
    }
    # mtb_action = make_mtb_action(**action_args)

    additional_args = {}
    if user_data_dir is not None:
        additional_args["user_data_dir"] = user_data_dir

    login_action = login_action_factory(**action_args)

    def casino_action(page: Page) -> None:
        # Perform login
        login_action(page)
        wait_for_load_all_safe(page)

        # Get account state
        account_state: CasinoAccountState = get_casino_account_state(page)
        log.info("Account State: %s", account_state)

        # Claim daily bonus
        if not skip_claim:
            claim_bonus_action(page)
        wait_for_load_all_safe(page)

        # You can add more actions here as needed

    with StealthySession(
        proxy=proxy,
        headless=headless,
        humanize=True,
        load_dom=True,
        google_search=False,
        additional_args=additional_args,
    ) as session:
        _: Response = session.fetch(
            login_url,
            page_action=casino_action,
            wait=5000,
        )


if __name__ == "__main__":
    parser = get_arg_parser(description="Stake.us Casino Automation")
    args = parser.parse_args()
    main(**vars(args))
