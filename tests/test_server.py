"""Exercise the actual Waitress entry point, without contacting Instagram."""
import json
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path


class ServerStartupTests(unittest.TestCase):
    def test_waitress_starts_and_serves_app_and_auth_state(self):
        process = subprocess.Popen(
            [sys.executable, 'app.py', '--port', '0'],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace',
        )
        try:
            # communicate() has a timeout, so startup failures cannot hang tests.
            import threading
            import queue
            lines = queue.Queue()
            reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
            reader.start()
            line = lines.get(timeout=15)
            self.assertTrue(line.startswith('Instagram Downloader: http://127.0.0.1:'), line)
            url = line.strip().split(' ', 2)[2]
            with urllib.request.urlopen(url, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b'id="login-form"', response.read())
            with urllib.request.urlopen(url + '/api/auth', timeout=5) as response:
                self.assertFalse(json.load(response)['authenticated'])
            try:
                urllib.request.urlopen(urllib.request.Request(
                    url + '/api/auth', method='POST', data=b'{"action":"logout"}',
                    headers={'Content-Type': 'application/json', 'Origin': 'https://other.invalid'}
                ), timeout=5)
                self.fail('Cross-origin request should be rejected')
            except urllib.error.HTTPError as error:
                self.assertEqual(error.code, 403)
                error.close()
        finally:
            process.terminate()
            process.communicate(timeout=5)
