"""Dedicated, ephemeral Instagram browser. Never read existing browser profiles."""
import time

import instaloader

from instagram_auth import close_loader, new_loader


def instagram_cookies(cookies):
    selected = {}
    for cookie in cookies:
        domain = cookie.get('domain', '').lstrip('.').lower()
        if domain != 'instagram.com' and not domain.endswith('.instagram.com'):
            continue
        if cookie.get('value'):
            selected[cookie['name']] = cookie['value']
    return selected


def run_browser_login(username, cancel, on_ready, on_error):
    loader = None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            # A new context has no access to the user's existing browser sessions.
            browser = playwright.chromium.launch(channel='msedge', headless=False)
            try:
                context = browser.new_context()
                page = context.new_page()
                page.goto('https://www.instagram.com/accounts/login/', wait_until='domcontentloaded', timeout=45000)
                deadline = time.monotonic() + 600
                while not cancel.is_set() and time.monotonic() < deadline:
                    if not context.pages:
                        on_error('Instagram window closed. Try signing in again.')
                        return
                    cookies = instagram_cookies(context.cookies('https://www.instagram.com/'))
                    if cookies.get('sessionid') and cookies.get('csrftoken') and cookies.get('ds_user_id', '').isdigit():
                        loader = new_loader()
                        loader.load_session(username, cookies)
                        # Check the entered account name against the cookie's account ID.
                        # No password or cookie values leave this local process.
                        profile = instaloader.Profile.from_username(loader.context, username)
                        if str(profile.userid) != cookies['ds_user_id']:
                            on_error('Account mismatch. Restart browser login with the correct username.')
                            return
                        if not cancel.is_set():
                            on_ready(loader, username)
                        return
                    page.wait_for_timeout(1000)
                if not cancel.is_set():
                    on_error('Browser login expired. Try again.')
            finally:
                browser.close()
    except ImportError:
        on_error('Browser module missing. Run start.bat to install it.')
    except Exception:
        # Browser exceptions can contain URLs or authentication payloads.
        if not cancel.is_set():
            on_error('Browser login failed. Check Edge is installed and Instagram is reachable.')
    finally:
        close_loader(loader)
