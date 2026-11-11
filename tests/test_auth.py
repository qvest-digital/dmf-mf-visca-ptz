import os
import socket
import subprocess
import sys
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import app
import auth as signin

import requests

ORIGIN = 'http://localhost'


class Issuer:
    """tests/issuer.py in a process of its own."""

    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), 'issuer.py')],
                                     stdout=subprocess.PIPE, text=True)
        port = int(self.proc.stdout.readline())
        self.base = f"http://127.0.0.1:{port}"
        self.url = self.base + '/realms/dmf'

    def set(self, **state):
        requests.post(self.base + '/control', json=state, timeout=5).raise_for_status()

    def close(self):
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()


class SignIn(unittest.TestCase):
    """With OIDC_ISSUER set, the page, the API and the socket need a sign-in;
    /healthz does not, since the kubelet probes it without one."""

    def setUp(self):
        self.issuer = Issuer()
        self.addCleanup(self.issuer.close)
        self.camera = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.camera.bind(('127.0.0.1', 0))
        self.camera.settimeout(0.5)
        self.addCleanup(self.camera.close)
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        os.environ.update({'VISCA_HOST': '127.0.0.1', 'VISCA_PORT': str(self.camera.getsockname()[1]),
                           'VISCA_LOCAL_PORT': '0'})
        os.environ.pop('VISCA_PROTOCOL', None)
        app.ptz = app.ViscaController()
        self.addCleanup(lambda: app.ptz.sock and app.ptz.sock.close())
        self.clock = [time.time()]
        self.auth = signin.Auth(issuer=self.issuer.url, client_id='visca-ptz.p-1.ptz',
                                redirect_url=ORIGIN + '/auth/callback', role_claim='roles',
                                now=lambda: self.clock[0])
        previous = app.auth
        app.auth = self.auth
        self.addCleanup(setattr, app, 'auth', previous)
        self.client = app.app.test_client()

    def sign_in(self):
        r = self.client.get('/auth/login')
        self.assertEqual(r.status_code, 302)
        query = {k: v[0] for k, v in parse_qs(urlsplit(r.location).query).items()}
        self.assertTrue(r.location.startswith(self.issuer.url + '/auth?'))
        self.assertEqual((query['code_challenge_method'], query['redirect_uri'], query['client_id']),
                         ('S256', ORIGIN + '/auth/callback', 'visca-ptz.p-1.ptz'))
        self.issuer.set(nonce=query['nonce'], challenge=query['code_challenge'])
        return self.client.get('/auth/callback', query_string={'state': query['state'], 'code': 'code'})

    def received(self):
        packets = []
        try:
            while True:
                packets.append(self.camera.recv(64))
        except socket.timeout:
            return packets

    def command(self, **headers):
        return self.client.post('/api/command', json={'cmd': 'stop'}, headers=headers)

    def test_without_a_session_nothing_reaches_the_camera(self):
        r = self.client.get('/')
        self.assertEqual((r.status_code, r.location), (302, '/auth/login'))
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        self.assertEqual(self.client.post('/api/connect', headers={'Origin': ORIGIN}).status_code, 401)
        self.assertEqual(self.command(Origin=ORIGIN).status_code, 401)
        sio = app.socketio.test_client(app.app, flask_test_client=self.client)
        self.assertFalse(sio.is_connected())
        self.assertEqual(self.received(), [])

    def test_healthz_needs_no_session(self):
        r = self.client.get('/healthz')
        self.assertEqual((r.status_code, r.data), (200, b'ok'))

    def test_an_operator_drives_the_camera_over_the_api_and_the_socket(self):
        r = self.sign_in()
        self.assertEqual((r.status_code, r.location), (302, '/'))
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'/auth/logout', page.data)
        self.assertEqual(self.client.get('/api/status').status_code, 200)
        self.assertEqual(self.command(Origin=ORIGIN).status_code, 200)
        self.assertEqual(self.received()[-1][8:], bytes.fromhex('8101 0601 0000 0303 ff'))
        sio = app.socketio.test_client(app.app, flask_test_client=self.client)
        self.assertTrue(sio.is_connected())
        sio.emit('ptz_command', {'cmd': 'zoom_stop'})
        self.assertEqual(self.received()[-1][8:], bytes.fromhex('8101 0407 00 ff'))

    def test_a_command_has_to_come_from_the_controllers_own_page(self):
        """The session cookie travels with a request any page in that browser
        makes; Origin, or Sec-Fetch-Site without one, tells them apart."""
        self.sign_in()
        self.assertEqual(self.command().status_code, 403)
        self.assertEqual(self.command(Origin='https://elsewhere.example').status_code, 403)
        self.assertEqual(self.command(**{'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        self.assertEqual(self.received(), [])
        self.assertEqual(self.command(**{'Sec-Fetch-Site': 'same-origin'}).status_code, 200)

    def test_a_user_without_the_operator_role_only_looks(self):
        self.issuer.set(roles=['switchframe-viewer'])
        self.sign_in()
        self.assertEqual(self.client.get('/api/status').status_code, 200)
        r = self.command(Origin=ORIGIN)
        self.assertEqual(r.status_code, 403)
        self.assertIn('visca-ptz-operator', r.get_json()['error'])
        sio = app.socketio.test_client(app.app, flask_test_client=self.client)
        self.assertTrue(sio.is_connected())
        events = {e['name']: e['args'][0] for e in sio.get_received()}
        self.assertTrue(events['camera_config']['locked'])
        self.assertFalse(events['connection_status']['success'])
        sio.emit('ptz_command', {'cmd': 'stop'})
        sio.emit('connect_camera', {'mode': 'sony_udp'})
        self.assertEqual(self.received(), [], 'a viewer neither connects nor moves the camera')

    def test_a_group_path_counts_by_its_last_element(self):
        self.auth.role_claim = 'groups'
        claims = {'groups': ['/studio/visca-ptz-operator']}
        self.assertEqual(self.auth._role(claims), signin.OPERATOR)
        self.assertEqual(self.auth._role({'groups': ['/visca-ptz-operator-x']}), signin.VIEWER)
        self.assertEqual(self.auth._role({}), signin.VIEWER)

    def test_a_callback_only_completes_the_sign_in_this_browser_started(self):
        r = self.client.get('/auth/callback', query_string={'state': 'forged', 'code': 'code'})
        self.assertEqual(r.status_code, 400)
        self.client.get('/auth/login')
        r = self.client.get('/auth/callback', query_string={'state': 'forged', 'code': 'code'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_an_id_token_for_another_sign_in_is_refused(self):
        self.issuer.set(token_nonce='replayed')
        self.assertEqual(self.sign_in().status_code, 403)
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_an_expired_id_token_is_renewed_with_the_refresh_token(self):
        self.sign_in()
        self.clock[0] += 600
        self.assertEqual(self.client.get('/api/status').status_code, 200)

    def test_a_session_the_issuer_will_not_renew_ends(self):
        self.sign_in()
        sio = app.socketio.test_client(app.app, flask_test_client=self.client)
        sio.get_received()
        self.issuer.set(refuse_refresh=True)
        self.clock[0] += 600
        self.received()
        sio.emit('ptz_command', {'cmd': 'stop'})
        self.assertFalse(sio.is_connected(), 'the page is told to sign in again and the socket dropped')
        self.assertEqual(self.received(), [])
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_sign_out_ends_the_session_here_and_at_the_issuer(self):
        self.sign_in()
        r = self.client.get('/auth/logout')
        self.assertTrue(r.location.startswith(self.issuer.url + '/logout?'))
        query = {k: v[0] for k, v in parse_qs(urlsplit(r.location).query).items()}
        self.assertEqual(query['post_logout_redirect_uri'], ORIGIN + '/')
        self.assertIn('id_token_hint', query)
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_an_unreachable_issuer_is_a_clear_error_not_a_crash(self):
        closed = socket.socket()
        closed.bind(('127.0.0.1', 0))
        port = closed.getsockname()[1]
        closed.close()
        app.auth = signin.Auth(issuer=f'http://127.0.0.1:{port}/realms/dmf', client_id='c',
                               redirect_url=ORIGIN + '/auth/callback')
        r = self.client.get('/auth/login')
        self.assertEqual(r.status_code, 503)
        self.assertIn(b'not reachable', r.data)
        self.assertEqual(self.client.get('/healthz').status_code, 200)


class Configuration(unittest.TestCase):
    def test_off_without_an_issuer(self):
        self.assertIsNone(signin.from_env({}))

    def test_an_issuer_needs_a_client_and_a_callback(self):
        base = {'OIDC_ISSUER': 'https://id.example.com/realms/dmf', 'OIDC_CLIENT_ID': 'c',
                'OIDC_REDIRECT_URL': 'https://ptz.example.com/auth/callback'}
        for missing in ('OIDC_CLIENT_ID', 'OIDC_REDIRECT_URL'):
            env = {k: v for k, v in base.items() if k != missing}
            with self.assertRaisesRegex(ValueError, missing):
                signin.from_env(env)
        with self.assertRaisesRegex(ValueError, 'OIDC_ISSUER'):
            signin.from_env(dict(base, OIDC_ISSUER='id.example.com'))
        a = signin.from_env(dict(base, OIDC_SCOPES='openid, roles'))
        self.assertEqual((a.origin, a.secure, a.scopes, a.role_claim, a.operator_role),
                         ('https://ptz.example.com', True, ['openid', 'roles'], 'groups', 'visca-ptz-operator'))


class Open(unittest.TestCase):
    """Without OIDC_ISSUER nothing asks for a sign-in."""

    def setUp(self):
        previous = app.auth
        app.auth = None
        self.addCleanup(setattr, app, 'auth', previous)

    def test_the_page_is_open_and_offers_no_sign_out(self):
        client = app.app.test_client()
        r = client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b'/auth/logout', r.data)
        self.assertEqual(client.get('/auth/login').status_code, 404)


if __name__ == '__main__':
    unittest.main()
