import unittest
from unittest.mock import Mock, patch

import instaloader

import app as module
from instagram_auth import InstagramAuth


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        module.auth = InstagramAuth()
        self.client = module.app.test_client()
        self.loader = Mock()
        self.loader.save_session.return_value = {'sessionid': 'secret', 'csrftoken': 'fake-token'}
        self.factory = patch('instagram_auth.new_loader', return_value=self.loader)
        self.factory.start()

    def tearDown(self):
        self.factory.stop()

    def login(self):
        return self.client.post('/api/auth', json={
            'username': '@myaccount', 'password': 'secret-password'
        })

    def test_login_download_session_and_logout(self):
        self.assertFalse(self.client.get('/api/auth').json['authenticated'])
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['authenticated'])
        self.assertEqual(response.json['username'], 'myaccount')
        self.assertNotIn('secret', response.get_data(as_text=True))
        self.loader.login.assert_called_once_with('myaccount', 'secret-password')
        destination = Mock()
        module.auth.apply(destination)
        destination.load_session.assert_called_once_with('myaccount', {'sessionid': 'secret', 'csrftoken': 'fake-token'})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertFalse(self.client.post('/api/auth', json={'action': 'logout'}).json['authenticated'])
        destination.reset_mock()
        module.auth.apply(destination)
        destination.load_session.assert_not_called()
        self.loader.save_session_to_file.assert_not_called()

    def test_two_factor_retry_then_success(self):
        self.loader.login.side_effect = instaloader.TwoFactorAuthRequiredException()
        response = self.login()
        self.assertTrue(response.json['two_factor_required'])
        self.assertFalse(response.json['authenticated'])
        self.loader.close.assert_not_called()
        self.loader.two_factor_login.side_effect = [instaloader.BadCredentialsException(), None]
        wrong = self.client.post('/api/auth', json={'action': 'two_factor', 'code': '123456'})
        self.assertEqual(wrong.status_code, 401)
        self.assertTrue(self.client.get('/api/auth').json['two_factor_required'])
        result = self.client.post('/api/auth', json={'action': 'two_factor', 'code': '654321'})
        self.assertTrue(result.json['authenticated'])
        self.assertFalse(result.json['two_factor_required'])
        self.loader.close.assert_called_once()

    def test_two_factor_expiration_and_cancel(self):
        self.loader.login.side_effect = instaloader.TwoFactorAuthRequiredException()
        self.login()
        module.auth.pending_until = 0
        response = self.client.post('/api/auth', json={'action': 'two_factor', 'code': '123456'})
        self.assertEqual(response.status_code, 400)
        self.loader.two_factor_login.assert_not_called()
        self.login()
        result = self.client.post('/api/auth', json={'action': 'logout'})
        self.assertFalse(result.json['two_factor_required'])

    def test_credentials_and_challenge_errors_do_not_expose_details(self):
        for exception in [instaloader.BadCredentialsException('secret-password'),
                          instaloader.LoginException('challenge: secret-cookie'),
                          RuntimeError('secret-cookie')]:
            with self.subTest(exception=type(exception)):
                self.loader.login.side_effect = exception
                response = self.login()
                self.assertGreaterEqual(response.status_code, 400)
                self.assertNotIn('secret', response.get_data(as_text=True))
                self.assertFalse(self.client.get('/api/auth').json['authenticated'])

    def test_validation_origin_and_busy(self):
        for payload in [[], {'username': 'name'}, {'username': '../path', 'password': 'x'},
                        {'action': 'two_factor', 'code': '<script>'}, {'action': 'unknown'}]:
            self.assertEqual(self.client.post('/api/auth', json=payload).status_code, 400)
        self.assertEqual(self.client.post('/api/auth', data='username=x').status_code, 415)
        self.assertEqual(self.client.post('/api/auth', json={'action': 'logout'},
                                         headers={'Origin': 'https://other.invalid'}).status_code, 403)
        self.assertEqual(self.client.get('/api/auth', headers={'Host': 'other.invalid'}).status_code, 400)
        with module.auth.lock:
            self.assertEqual(self.client.get('/api/auth').status_code, 409)
            self.assertEqual(self.login().status_code, 409)

    def test_expired_code_error_includes_current_form_state(self):
        self.loader.login.side_effect = instaloader.TwoFactorAuthRequiredException()
        self.login()
        module.auth.pending_until = 0
        response = self.client.post('/api/auth', json={'action': 'two_factor', 'code': '123456'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json['two_factor_required'])
        self.assertFalse(response.json['authenticated'])

    def test_pending_auth_is_cleared_after_unrecoverable_code_error(self):
        self.loader.login.side_effect = instaloader.TwoFactorAuthRequiredException()
        self.login()
        self.loader.two_factor_login.side_effect = instaloader.LoginException('challenge')
        response = self.client.post('/api/auth', json={'action': 'two_factor', 'code': '123456'})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.client.get('/api/auth').json['two_factor_required'])

    def test_logout_still_clears_state_if_cleanup_fails(self):
        self.loader.login.side_effect = instaloader.TwoFactorAuthRequiredException()
        self.login()
        self.loader.close.side_effect = OSError('close failed')
        response = self.client.post('/api/auth', json={'action': 'logout'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['two_factor_required'])

    def test_real_instaloader_session_roundtrip_without_network(self):
        source = instaloader.Instaloader(quiet=True)
        destination = instaloader.Instaloader(quiet=True)
        try:
            source.load_session('myaccount', {'csrftoken': 'fake-token', 'sessionid': 'fake-id'})
            module.auth.complete(source, 'myaccount')
            snapshot = module.auth.snapshot()
            module.auth.logout()
            destination.load_session(*snapshot)
            self.assertEqual(destination.context.username, 'myaccount')
            self.assertEqual(destination.save_session()['sessionid'], 'fake-id')
        finally:
            source.close()
            destination.close()

    def test_empty_cookie_is_not_reported_as_successful_login(self):
        for cookies in ({}, {'sessionid': '', 'csrftoken': 'token'}, {'sessionid': 'id'}):
            with self.subTest(cookies=cookies):
                self.loader.save_session.return_value = cookies
                response = self.login()
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json['authenticated'])

    def test_browser_login_completion_and_cancellation(self):
        with patch('instagram_auth.threading.Thread') as thread:
            response = self.client.post('/api/auth', json={'action': 'browser_login', 'username': 'MyAccount'})
            self.assertTrue(response.json['browser_pending'])
            account, cancel, ready, failed = thread.call_args.kwargs['args']
            self.assertEqual(account, 'myaccount')
            self.assertEqual(self.login().status_code, 409)
            ready(self.loader, account)
            self.assertTrue(self.client.get('/api/auth').json['authenticated'])
            self.assertFalse(self.client.get('/api/auth').json['browser_pending'])
            self.client.post('/api/auth', json={'action': 'browser_login', 'username': 'myaccount'})
            account, cancel, ready, failed = thread.call_args.kwargs['args']
            self.client.post('/api/auth', json={'action': 'logout'})
            self.assertTrue(cancel.is_set())
            ready(self.loader, account)
            self.assertFalse(self.client.get('/api/auth').json['authenticated'])

    def test_browser_failure_does_not_destroy_previous_session(self):
        self.login()
        with patch('instagram_auth.threading.Thread') as thread:
            self.client.post('/api/auth', json={'action': 'browser_login', 'username': 'myaccount'})
            account, cancel, ready, failed = thread.call_args.kwargs['args']
            failed('Browser was closed')
        state = self.client.get('/api/auth').json
        self.assertTrue(state['authenticated'])
        self.assertFalse(state['browser_pending'])
        self.assertEqual(state['browser_message'], 'Browser was closed')
