"""One local account, kept in memory; never persist passwords or cookies."""
import threading
import time

import instaloader


class AuthenticationExpired(ValueError):
    pass


class InvalidSession(ValueError):
    pass


def close_loader(loader):
    if loader is not None:
        try:
            loader.close()
        except Exception:
            # Cleanup must never leave a job or authentication state stuck.
            pass


def new_loader():
    return instaloader.Instaloader(
        quiet=True, download_videos=False, download_video_thumbnails=False,
        save_metadata=False, post_metadata_txt_pattern="", max_connection_attempts=1,
        request_timeout=30,
    )


class InstagramAuth:
    def __init__(self):
        self.lock = threading.Lock()
        self.username = None
        self.session = None
        self.pending = None
        self.pending_username = None
        self.pending_until = 0
        self.browser_cancel = None
        self.browser_message = None

    def clear_pending(self):
        close_loader(self.pending)
        self.pending = None
        self.pending_username = None
        self.pending_until = 0

    def state(self):
        if self.pending is not None and time.monotonic() > self.pending_until:
            self.clear_pending()
        return dict(authenticated=self.session is not None, username=self.username,
                    two_factor_required=self.pending is not None,
                    browser_pending=self.browser_cancel is not None,
                    browser_message=self.browser_message)

    def complete(self, loader, username):
        cookies = loader.save_session()
        if not cookies.get('sessionid') or not cookies.get('csrftoken'):
            raise InvalidSession('No valid session. Use browser login.')
        self.session = cookies
        self.username = username
        self.clear_pending()

    def login(self, username, password):
        if self.browser_cancel is not None:
            raise ValueError('Browser login in progress. Complete or cancel it.')
        self.clear_pending()
        loader = new_loader()
        try:
            loader.login(username, password)
            self.complete(loader, username)
        except instaloader.TwoFactorAuthRequiredException:
            self.pending = loader
            self.pending_username = username
            self.pending_until = time.monotonic() + 600
        finally:
            if self.pending is not loader:
                close_loader(loader)
        return self.state()

    def two_factor(self, code):
        if not self.state()['two_factor_required']:
            raise AuthenticationExpired('Login expired. Sign in again.')
        loader = self.pending
        self.complete_after_code(loader, code)
        return self.state()

    def complete_after_code(self, loader, code):
        try:
            loader.two_factor_login(code)
            self.complete(loader, self.pending_username)
        except instaloader.BadCredentialsException:
            raise
        except Exception:
            self.clear_pending()
            raise

    def logout(self):
        if self.browser_cancel is not None:
            self.browser_cancel.set()
            self.browser_cancel = None
        self.browser_message = None
        self.clear_pending()
        self.username = None
        self.session = None
        return self.state()

    def begin_browser(self, username):
        from browser_login import run_browser_login
        if self.browser_cancel is not None:
            return self.state()
        self.clear_pending()
        cancel = threading.Event()
        self.browser_cancel = cancel
        self.browser_message = None

        def ready(loader, account):
            with self.lock:
                if self.browser_cancel is cancel and not cancel.is_set():
                    try:
                        self.complete(loader, account)
                        self.browser_message = 'Browser session imported.'
                    except InvalidSession:
                        self.browser_message = 'No valid session. Try again.'
                    finally:
                        self.browser_cancel = None

        def failed(message):
            with self.lock:
                if self.browser_cancel is cancel:
                    self.browser_message = message
                    self.browser_cancel = None

        try:
            threading.Thread(target=run_browser_login, args=(username, cancel, ready, failed), daemon=True).start()
        except Exception:
            self.browser_cancel = None
            self.browser_message = 'Instagram window closed. Try signing in again.'
        return self.state()

    def apply(self, loader):
        snapshot = self.snapshot()
        if snapshot is not None:
            loader.load_session(*snapshot)

    def snapshot(self):
        if not self.lock.acquire(blocking=False):
            raise BlockingIOError('Authentication in progress')
        try:
            if self.browser_cancel is not None or self.pending is not None:
                raise BlockingIOError('Authentication in progress')
            return (self.username, dict(self.session)) if self.session is not None else None
        finally:
            self.lock.release()
