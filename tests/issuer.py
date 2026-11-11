"""An OpenID Connect provider for the sign-in tests: discovery, keys, and a
token endpoint that checks the PKCE verifier against the challenge it was
shown. POST /control sets what it answers. It runs as a process of its own,
outside the eventlet-patched test process. Prints its port, then serves."""
import base64
import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
STATE = {'roles': ['visca-ptz-operator'], 'refuse_refresh': False, 'nonce': None, 'challenge': None,
         'token_nonce': None, 'audience': 'visca-ptz.p-1.ptz'}


def tokens(url, nonce):
    now = int(time.time())
    claims = {'iss': url, 'aud': STATE['audience'], 'sub': 'u1', 'preferred_username': 'jo',
              'iat': now, 'exp': now + 300, 'roles': STATE['roles']}
    if nonce is not None:
        claims['nonce'] = nonce
    return {'access_token': 'a', 'token_type': 'Bearer', 'expires_in': 300,
            'refresh_token': 'refresh', 'refresh_expires_in': 1800,
            'id_token': jwt.encode(claims, KEY, algorithm='RS256', headers={'kid': 'k'})}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/realms/dmf"

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = self.url()
        if self.path == '/realms/dmf/.well-known/openid-configuration':
            self.reply(200, {'issuer': url, 'authorization_endpoint': url + '/auth',
                             'token_endpoint': url + '/token', 'jwks_uri': url + '/jwks',
                             'end_session_endpoint': url + '/logout'})
        elif self.path == '/realms/dmf/jwks':
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
            self.reply(200, {'keys': [dict(jwk, kid='k', use='sig', alg='RS256')]})
        else:
            self.reply(404, {})

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length'])).decode()
        if self.path == '/control':
            STATE.update(json.loads(body))
            return self.reply(200, STATE)
        form = {k: v[0] for k, v in parse_qs(body).items()}
        if form.get('grant_type') == 'authorization_code':
            verifier = form.get('code_verifier', '')
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            if form.get('code') != 'code' or digest != STATE['challenge']:
                return self.reply(400, {'error': 'invalid_grant'})
            nonce = STATE['nonce'] if STATE['token_nonce'] is None else STATE['token_nonce']
            return self.reply(200, tokens(self.url(), nonce))
        if form.get('grant_type') == 'refresh_token':
            if STATE['refuse_refresh'] or form.get('refresh_token') != 'refresh':
                return self.reply(400, {'error': 'invalid_grant'})
            return self.reply(200, tokens(self.url(), None))
        self.reply(400, {'error': 'unsupported_grant_type'})


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()
