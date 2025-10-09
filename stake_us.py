from argparse import ArgumentParser
import pyotp
from logging import log
from typing import Callable, Optional
from playwright.sync_api import Page, Response
from scrapling.fetchers import StealthySession
from scrapling_pick import get_credentials
import sys

url = "https://stake.us"
login_url = "https://stake.us/?tab=login&modal=auth"
username_selector = 'input[name="emailOrName"]'
password_selector = 'input[name="password"]'

sys.path.append(".")


def make_login_page(username: str, password: str, totp_secret: Optional[str]) -> Callable[[Page], None]:
    """Create a login page action.

    Args:
        username (str): Username for login.
        password (str): Password for login.

    Returns:
        Callable[[Page], None]: A function that performs the login action on the given page.
    """

    def login_page(page: Page):
        page.fill(username_selector, username)
        page.fill(password_selector, password)
        try:
            page.click('button[type="submit"]')
        except Exception as e:
            log.error("Error during login: %s", str(e))
            raise e
        if totp_secret:
            try:
                page.wait_for_selector('input[name="code"]', state="visible", timeout=10000)
                totp_code = pyotp.TOTP(totp_secret).now()
                page.fill('input[name="code"]', totp_code)
                page.click('button[type="submit"]')
            except Exception as e:
                log.error("Error during TOTP entry: %s", str(e))
                raise e

    return login_page


import os


def main(proxy: str | None = None, use_google_oauth: bool = False):
    username, password = get_credentials("https://stake.us")
    totp_secret = os.getenv("STAKE_2FA")  # Replace with actual TOTP secret if available

    with StealthySession(
        proxy=proxy,
        headless=False,
        humanize=True,
        load_dom=True,
        # solve_cloudflare=True,
        # additional_args={"user_data_dir": "./google_logged_in"},
    ) as session:
        session.fetch(
            login_url,
            page_action=make_login_page(username, password, totp_secret),
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
    args = parser.parse_args()
    main(args.proxy, args.google_oauth)
