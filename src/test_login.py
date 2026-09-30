import logging
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from unittest.mock import Mock, patch

from selenium.webdriver.remote.webelement import WebElement
from webtest import TestApp

from dtos import V1RequestBase
import flaresolverr
import flaresolverr_service as service
import utils


class LoginSite(BaseHTTPRequestHandler):
    submissions = 0

    def log_message(self, *args):
        pass

    def respond(self, body, cookie=False):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        if cookie:
            self.send_header('Set-Cookie', 'demo_logged_in=true; Path=/')
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path == '/already':
            self.respond('success')
        elif self.path == '/missing':
            self.respond('<html><body>No login form</body></html>')
        elif self.path == '/blocked':
            self.respond('<html><head><title>Access denied</title></head><body>Blocked</body></html>')
        elif self.path == '/late-captcha':
            self.respond('''<html><body><script>
                setTimeout(() => {
                    document.title = 'Just a moment...';
                    document.body.innerHTML = '<div id="cf-please-wait">Verify you are human</div>';
                }, 300);
            </script></body></html>''')
        elif self.path == '/turnstile':
            self.respond('<html><body><input name="cf-turnstile-response" value=""></body></html>')
        else:
            self.respond('''<html><body><script>
                setTimeout(() => {
                    document.body.innerHTML = `<form method="post">
                        <input id="inputEmail" name="email" value="old">
                        <input id="inputPassword" name="password" type="password" value="old">
                        <button type="submit">Login</button></form>`;
                }, 300);
            </script></body></html>''')

    def do_POST(self):
        type(self).submissions += 1
        fields = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
        if fields != {'email': ['demo@example.test'], 'password': ['demo-secret']}:
            self.respond('invalid credentials')
        elif self.path == '/mismatch':
            self.respond('unsuccessful')
        elif self.path == '/captcha':
            self.respond('<html><head><title>Just a moment...</title></head>'
                         '<body><div id="cf-please-wait">Verify you are human</div></body></html>')
        else:
            self.respond(' \n success \n ', cookie=True)


class TestLogin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        utils.get_current_platform()
        utils.get_user_agent()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), LoginSite)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.session, _ = service.SESSIONS_STORAGE.create('local-login-test')
        cls.app = TestApp(flaresolverr.app)

    @classmethod
    def tearDownClass(cls):
        service.SESSIONS_STORAGE.destroy('local-login-test')
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        LoginSite.submissions = 0
        self.session.driver.delete_all_cookies()

    def request(self, path='/login', **overrides):
        payload = {
            'cmd': 'request.get', 'url': self.url + path,
            'session': 'local-login-test', 'maxTimeout': 4000,
            'login': {
                'username': 'demo@example.test', 'password': 'demo-secret',
                'usernameSelector': '#inputEmail', 'passwordSelector': '#inputPassword',
                'submitSelector': 'button[type=submit]', 'successText': 'success',
            },
        }
        payload.update(overrides)
        return self.app.post_json('/v1', payload, expect_errors=True)

    def test_same_url_plain_text_and_cookie(self):
        res = self.request(returnScreenshot=True)
        self.assertEqual(res.status_int, 200)
        solution = res.json['solution']
        self.assertTrue(solution.get('loginSuccess'))
        self.assertEqual(solution['url'], self.url + '/login')
        self.assertEqual(self.session.driver.execute_script('return document.body.innerText').strip(), 'success')
        self.assertIn({'name': 'demo_logged_in', 'value': 'true'},
                      [{'name': c['name'], 'value': c['value']} for c in solution['cookies']])
        self.assertTrue(solution['screenshot'])
        self.assertEqual(LoginSite.submissions, 1)

    def test_already_success_skips_form(self):
        res = self.request('/already', returnOnlyCookies=True)
        self.assertEqual(res.status_int, 200)
        self.assertTrue(res.json['solution'].get('loginSuccess'))
        self.assertNotIn('response', res.json['solution'])
        self.assertEqual(LoginSite.submissions, 0)

    def test_error_codes_and_exact_match(self):
        for path, code in [('/missing', 'LOGIN_FORM_NOT_FOUND'),
                           ('/mismatch', 'LOGIN_SUCCESS_TIMEOUT'),
                           ('/captcha', 'CAPTCHA_UNRESOLVED'),
                           ('/late-captcha', 'CAPTCHA_UNRESOLVED'),
                           ('/blocked', 'CAPTCHA_UNRESOLVED')]:
            with self.subTest(path=path):
                res = self.request(path, maxTimeout=1500)
                self.assertEqual(res.status_int, 500)
                self.assertEqual(res.json.get('errorCode'), code)
        self.assertEqual(LoginSite.submissions, 2)

    def test_invalid_login_params(self):
        for override in [{'login': {}}, {'login': 'invalid'},
                         {'login': {'successText': ' '}}, {'cmd': 'sessions.list'}]:
            with self.subTest(override=override):
                res = self.request(**override)
                self.assertEqual(res.status_int, 500)
                self.assertEqual(res.json.get('errorCode'), 'LOGIN_INVALID_PARAMS')

    def test_credentials_and_response_not_logged(self):
        with self.assertLogs(level=logging.DEBUG) as captured:
            res = self.request()
        self.assertEqual(res.status_int, 200)
        logs = '\n'.join(captured.output)
        for secret in ['demo@example.test', 'demo-secret', 'demo_logged_in', '<html']:
            self.assertNotIn(secret, logs)

    def test_without_login_keeps_existing_behavior(self):
        res = self.request('/already', login=None)
        self.assertEqual(res.status_int, 200)
        self.assertNotIn('loginSuccess', res.json['solution'])

    def test_session_page_load_timeout_is_restored(self):
        before = self.session.driver.timeouts.page_load
        res = self.request('/already')
        self.assertEqual(res.status_int, 200)
        self.assertEqual(self.session.driver.timeouts.page_load, before)

    def test_session_page_load_timeout_is_restored_after_failure(self):
        before = self.session.driver.timeouts.page_load
        res = self.request('/missing', maxTimeout=1000)
        self.assertEqual(res.json.get('errorCode'), 'LOGIN_FORM_NOT_FOUND')
        self.assertEqual(self.session.driver.timeouts.page_load, before)

    def test_replaced_form_elements_are_relocated(self):
        driver = self.session.driver
        replaced = set()

        def replacing(method, key):
            def call(element, *args):
                if key not in replaced:
                    replaced.add(key)
                    driver.execute_script(
                        'arguments[0].replaceWith(arguments[0].cloneNode(true))', element)
                return method(element, *args)
            return call

        with patch.object(WebElement, 'clear', replacing(WebElement.clear, 'clear')), \
                patch.object(WebElement, 'send_keys', replacing(WebElement.send_keys, 'send_keys')), \
                patch.object(WebElement, 'click', replacing(WebElement.click, 'click')):
            res = self.request()
        self.assertEqual(res.status_int, 200)
        self.assertTrue(res.json['solution'].get('loginSuccess'))
        self.assertEqual(LoginSite.submissions, 1)

    def test_explicit_turnstile_timeout_has_captcha_error(self):
        res = self.request('/turnstile', tabs_till_verify=1, maxTimeout=1500)
        self.assertEqual(res.json.get('errorCode'), 'CAPTCHA_UNRESOLVED')


class TestLoginBrowserStartup(unittest.TestCase):
    def test_slow_browser_creation_times_out_and_cleans_up(self):
        for session_id in [None, 'slow-login-startup']:
            with self.subTest(session=session_id):
                release = threading.Event()
                finished = threading.Event()
                driver = Mock()
                driver.quit.side_effect = finished.set

                def create(*args):
                    release.wait(2)
                    return driver

                req = V1RequestBase({'cmd': 'request.get', 'url': 'http://localhost',
                                     'session': session_id, 'login': {}, 'maxTimeout': 50})
                with patch.object(utils, 'get_webdriver', side_effect=create):
                    started = time.monotonic()
                    try:
                        with self.assertRaises(service.LoginError) as error:
                            service._resolve_challenge(req, 'GET')
                        self.assertEqual(error.exception.code, 'LOGIN_FORM_NOT_FOUND')
                        self.assertLess(time.monotonic() - started, 0.5)
                    finally:
                        release.set()
                    self.assertTrue(finished.wait(2))
                self.assertNotIn(session_id, service.SESSIONS_STORAGE.session_ids())


if __name__ == '__main__':
    unittest.main()
