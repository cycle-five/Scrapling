from argparse import ArgumentParser
from typing import Optional, Callable, List
import sys
import os
import pyotp

sys.path.append(".")

from scrapling.engines.toolbelt.custom import Response
from scrapling.fetchers import StealthySession
from playwright.sync_api import Page, Error as PlaywrightError
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


def claim_luckybird_daily(page: Page) -> bool:
    """Claim the daily bonus on LuckyBird.io.

    Args:
        page: The Playwright Page object

    Returns:
        bool: True if claim was successful, False otherwise
    """
    daily_modal_selector = "section.dailyBonus_page"
    claim_button_selector = "section.dailyBonus_page button.el-button--primary:not(.is-disabled)"
    close_modal_selector = "section.dailyBonus_page .commonAlert_close"

    try:
        # Wait for the daily bonus modal to appear
        log.info("Waiting for daily bonus modal to appear...")
        page.wait_for_selector(daily_modal_selector, state="visible", timeout=10000)

        # Find the claim button that is not disabled
        claim_buttons = page.locator(claim_button_selector)

        if claim_buttons.count() == 0:
            log.info("No claimable daily bonus available (already claimed or not ready)")
            return False

        # Click the first available claim button
        log.info("Found claimable daily bonus, clicking claim button...")
        claim_buttons.first.click(delay=gaussian_random_delay(), timeout=5000)

        # Wait for the claim to process
        wait_for_load_all_safe(page, timeout=3000)

        log.info("Successfully claimed daily bonus!")
        return True

    except PlaywrightError as e:
        log.warning("Could not claim daily bonus: %s", str(e))
        return False
    finally:
        # Try to close the modal if it's still open
        try:
            close_button = page.locator(close_modal_selector)
            if close_button.count() > 0:
                close_button.click(delay=gaussian_random_delay(), timeout=3000)
                log.info("Closed daily bonus modal")
        except PlaywrightError:
            pass


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

        # Claim the daily bonus
        if not skip_claim:
            claim_luckybird_daily(page)

    return luckybird_action


def main(
    proxy: Optional[str],
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
        headless=False,
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
    parser.add_argument(
        "--skip-claim",
        action="store_true",
        help="Skip the claim portion of the code.",
    )
    args = parser.parse_args()

    main(**vars(args))
