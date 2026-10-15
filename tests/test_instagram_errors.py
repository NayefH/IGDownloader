import unittest

import instaloader
from requests.exceptions import ConnectionError

from browser_login import instagram_cookies
from instagram_errors import explain


class InstagramErrorTests(unittest.TestCase):
    def test_distinct_failure_causes_without_returning_secrets(self):
        cases = [
            (instaloader.ConnectionException('HTTP 401 secret-cookie'), 'unauthorized'),
            (instaloader.ConnectionException('HTTP 403 secret-cookie'), 'forbidden'),
            (instaloader.ConnectionException('HTTP 429 secret-cookie'), 'rate_limited'),
            (instaloader.AbortDownloadException('challenge_required secret-cookie'), 'confirmation_required'),
            (instaloader.AbortDownloadException('Redirected to login page'), 'session_expired'),
            (instaloader.ProfileNotExistsException('secret-cookie'), 'profile_unavailable'),
            (instaloader.ConnectionException('execution error GraphQL'), 'instagram_api_changed'),
            (ConnectionError('secret-cookie'), 'connection_failed'),
            (KeyError('secret-cookie'), 'instagram_api_changed'),
        ]
        for error, expected in cases:
            with self.subTest(code=expected):
                code, message = explain(error)
                self.assertEqual(code, expected)
                self.assertNotIn('secret-cookie', message)

    def test_browser_cookies_only_include_nonempty_instagram_values(self):
        cookies = [
            {'domain': '.instagram.com', 'name': 'sessionid', 'value': 'id'},
            {'domain': 'www.instagram.com', 'name': 'csrftoken', 'value': 'csrf'},
            {'domain': 'instagram.com.evil.invalid', 'name': 'bad', 'value': 'bad'},
            {'domain': 'notinstagram.com', 'name': 'bad', 'value': 'bad'},
            {'domain': '.instagram.com', 'name': 'empty', 'value': ''},
        ]
        self.assertEqual(instagram_cookies(cookies), {'sessionid': 'id', 'csrftoken': 'csrf'})
