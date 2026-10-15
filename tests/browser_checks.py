"""Real Edge browser, real local HTTP server; only Instagram is simulated.

Run: python -m unittest discover -s tests -p browser_checks.py -v
"""
import io
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import instaloader
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

import app as module


class BrowserChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel='msedge', headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        module.jobs.clear()
        module.auth = module.InstagramAuth()
        self.patches = [patch.object(module, 'ROOT', Path(self.tmp.name)),
                        patch('instagram_auth.new_loader', side_effect=self.loader),
                        patch('app.new_loader', side_effect=self.loader),
                        patch.object(instaloader.Profile, 'from_username', side_effect=self.profile)]
        for item in self.patches:
            item.start()
        self.server = make_server('127.0.0.1', 0, module.app, threaded=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.context = self.browser.new_context(accept_downloads=True)
        self.page = self.context.new_page()
        self.page.set_default_timeout(8000)
        self.page.set_default_navigation_timeout(15000)
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def loader(self):
        loader = Mock()
        loader.context.username = None
        loader.save_session.return_value = {'csrftoken': 'fake', 'sessionid': 'fake'}
        def login(username, password):
            if password == 'wrong':
                raise instaloader.BadCredentialsException()
            raise instaloader.TwoFactorAuthRequiredException()
        def code(value):
            if value == '000000':
                raise instaloader.BadCredentialsException()
        loader.login.side_effect = login
        loader.two_factor_login.side_effect = code
        loader.load_session.side_effect = lambda username, cookies: setattr(loader.context, 'username', username)
        loader.download_pic.side_effect = lambda stem, url, date: Path(stem + '.jpg').write_bytes(b'test-photo')
        return loader

    def profile(self, context, username):
        post = SimpleNamespace(typename='GraphImage', is_video=False, shortcode='test',
                               date_utc=datetime(2026, 1, 1), url='https://example.invalid/photo.jpg')
        return SimpleNamespace(is_private=False, mediacount=1, get_posts=lambda: iter([post]))

    def open(self):
        self.page.goto(self.url)
        self.page.locator('#login-form').wait_for(state='visible')

    def start_login(self, password='correct'):
        self.page.locator('#login-username').fill('myaccount')
        self.page.locator('#login-password').fill(password)
        self.page.locator('#login-submit').click()

    def test_login_retry_two_factor_download_and_logout(self):
        self.open()
        self.start_login('wrong')
        self.page.locator('#auth-error').wait_for(state='visible')
        self.assertEqual(self.page.locator('#login-password').input_value(), '')
        self.start_login()
        self.page.locator('#two-factor-form').wait_for(state='visible')
        self.page.locator('#two-factor-code').fill('000000')
        self.page.locator('#code-submit').click()
        self.page.locator('#auth-error').wait_for(state='visible')
        self.page.locator('#two-factor-code').fill('123456')
        self.page.locator('#code-submit').click()
        self.page.wait_for_function("document.querySelector('#auth-state').textContent.includes('Signed in as')")
        self.page.reload()
        self.page.wait_for_function("document.querySelector('#auth-state').textContent.includes('Signed in as')")
        self.page.locator('#username').fill('example')
        with self.page.expect_download() as download_info:
            self.page.locator('#submit').click()
        download = download_info.value
        self.assertEqual(download.suggested_filename, 'example_photos.zip')
        with zipfile.ZipFile(io.BytesIO(Path(download.path()).read_bytes())) as archive:
            self.assertEqual(archive.read(archive.namelist()[0]), b'test-photo')
        self.page.locator('#logout').click()
        self.page.locator('#login-form').wait_for(state='visible')
        self.page.set_viewport_size({'width': 375, 'height': 812})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertEqual(self.errors, [])

    def test_profile_preview_and_combined_sources(self):
        avatar = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMcQAAAAASUVORK5CYII='
        with patch.object(module, 'profile_preview', return_value={'username': 'example', 'avatar': avatar}):
            self.open()
            self.page.locator('#username').fill('example')
            self.page.locator('#find-profile').click()
            self.page.locator('#profile-avatar').wait_for(state='visible')
            self.assertEqual(self.page.locator('#profile-name').inner_text(), '@example')
            module.auth.username = 'viewer'
            module.auth.session = {'sessionid': 'fake'}
            self.page.locator('input[value="stories"]').check()
            self.page.locator('input[value="highlights"]').check()
            self.page.locator('input[value="both"]').check()
            with patch.object(module, 'archive_photos') as worker:
                with self.page.expect_response('**/api/jobs') as response:
                    self.page.locator('#submit').click()
                job = module.jobs[response.value.json()['id']]
                self.assertEqual(job['sources'], ['posts', 'stories', 'highlights'])
                self.assertEqual(job['media_type'], 'both')
                self.assertEqual(worker.call_args.args[4], ['posts', 'stories', 'highlights'])
                self.assertTrue(self.page.locator('input[value="stories"]').is_disabled())
            self.assertEqual(self.errors, [])

    def test_cancel_download_restores_media_selection(self):
        released = threading.Event()
        def worker(job_id, *args):
            released.wait(10)
            module.update(job_id, status='cancelled', message='Download cancelled.')
        try:
            with patch.object(module, 'archive_photos', side_effect=worker):
                self.open()
                self.page.locator('input[value="videos"]').check()
                self.page.locator('#username').fill('example')
                self.page.locator('#submit').click()
                self.page.locator('#cancel-download').wait_for(state='visible')
                self.assertTrue(self.page.locator('input[value="photos"]').is_disabled())
                with self.page.expect_response('**/cancel') as response:
                    self.page.locator('#cancel-download').click()
                self.assertTrue(response.value.json()['cancel_requested'])
                released.set()
                self.page.wait_for_function("!document.querySelector('#submit').disabled")
                self.assertFalse(self.page.locator('#cancel-download').is_visible())
                self.assertFalse(self.page.locator('#zip-link').is_visible())
                self.page.locator('input[value="both"]').check()
                self.assertEqual(self.errors, [])
        finally:
            released.set()

    def test_media_choices_are_sent_to_server(self):
        self.open()
        for choice in ('photos', 'videos', 'both'):
            self.page.locator(f'input[name="media_type"][value="{choice}"]').check()
            self.page.locator('#username').fill('example')
            with patch.object(module, 'archive_photos') as worker:
                with self.page.expect_response('**/api/jobs') as response:
                    self.page.locator('#submit').click()
                result = response.value.json()
                self.assertEqual(module.jobs[result['id']]['media_type'], choice)
                self.assertEqual(worker.call_args.args[3], choice)
            module.jobs.clear()
            self.page.evaluate('sessionStorage.clear()')
            self.page.reload()
            self.page.locator('#login-form').wait_for(state='visible')
        self.page.set_viewport_size({'width': 375, 'height': 812})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertEqual(self.errors, [])

    def test_old_job_after_server_restart_allows_a_new_download(self):
        self.page.add_init_script("sessionStorage.setItem('archiveJob', 'missing')")
        self.page.goto(self.url)
        self.page.wait_for_function("!document.querySelector('#submit').disabled")
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('archiveJob')"))
        self.page.locator('#username').fill('example')
        with self.page.expect_download():
            self.page.locator('#submit').click()
        self.assertEqual(self.errors, [])

    def test_expired_two_factor_immediately_shows_login_again(self):
        self.open()
        self.start_login()
        self.page.locator('#two-factor-form').wait_for(state='visible')
        module.auth.pending_until = 0
        self.page.locator('#two-factor-code').fill('123456')
        self.page.locator('#code-submit').click()
        self.page.locator('#auth-error').wait_for(state='visible')
        self.page.locator('#login-form').wait_for(state='visible')
        self.assertFalse(self.page.locator('#two-factor-form').is_visible())
        self.assertEqual(self.errors, [])

    def test_disabled_browser_storage_does_not_break_login_or_download(self):
        self.page.add_init_script("Storage.prototype.getItem = Storage.prototype.setItem = Storage.prototype.removeItem = () => { throw new DOMException('Storage blocked', 'SecurityError'); }")
        self.open()
        self.page.locator('#username').fill('example')
        with self.page.expect_download():
            self.page.locator('#submit').click()
        self.assertEqual(self.errors, [])

    def test_status_network_failure_can_be_retried(self):
        self.open()
        failed = False
        def fail_once(route):
            nonlocal failed
            if not failed and not route.request.url.endswith('/download'):
                failed = True
                route.abort()
            else:
                route.continue_()
        self.page.route('**/api/jobs/*', fail_once)
        self.page.locator('#username').fill('example')
        self.page.locator('#submit').click()
        self.page.locator('#retry').wait_for(state='visible')
        with self.page.expect_download():
            self.page.locator('#retry').click()
        self.assertFalse(self.page.locator('#submit').is_disabled())
        self.assertEqual(self.errors, [])

    def test_login_network_failure_restores_status_and_can_retry(self):
        self.open()
        failed = False
        def fail_once(route):
            nonlocal failed
            if not failed and route.request.method == 'POST':
                failed = True
                route.abort()
            else:
                route.continue_()
        self.page.route('**/api/auth', fail_once)
        self.start_login()
        self.page.locator('#auth-error').wait_for(state='visible')
        self.assertNotIn('verarbeitet', self.page.locator('#auth-state').inner_text())
        self.assertEqual(self.page.locator('#login-password').input_value(), '')
        self.start_login()
        self.page.locator('#two-factor-form').wait_for(state='visible')
        self.assertEqual(self.errors, [])

    def test_browser_login_button_imports_session_and_updates_ui(self):
        def browser_login(username, cancel, ready, failed):
            ready(self.loader(), username)
        with patch('browser_login.run_browser_login', side_effect=browser_login):
            self.open()
            self.page.locator('#browser-username').fill('myaccount')
            self.page.locator('#browser-submit').click()
            self.page.wait_for_function("document.querySelector('#auth-state').textContent.includes('Signed in as')")
            self.assertTrue(module.auth.state()['authenticated'])
            self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
