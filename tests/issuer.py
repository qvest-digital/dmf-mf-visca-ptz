"""An OpenID Connect provider for the sign-in tests: discovery, keys, and a
token endpoint that checks the PKCE verifier against the challenge it was
shown. POST /control sets what it answers, including ID tokens that a
client has to refuse. It runs as a process of its own,
outside the eventlet-patched test process. Prints its port, then serves."""
import base64
import hashlib
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
STATE = {
    'roles': ['visca-ptz-operator'], 'refuse_refresh': False, 'refresh_status': None,
    'nonce': None, 'challenge': None, 'token_nonce': None,
    # What the ID token says and how it is signed.
    'audience': 'visca-ptz', 'iss': None, 'sub': 'u1', 'iat_offset': 0,
    'kid': 'k', 'alg': 'RS256', 'other_key': False,
    # The issuer discovery names; None is the issuer's own URL.
    'discovery_issuer': None,
}


def id_token(url, nonce):
    now = int(time.time())
    claims = {'iss': STATE['iss'] or url, 'aud': STATE['audience'], 'sub': STATE['sub'],
              'preferred_username': 'jo', 'iat': now + STATE['iat_offset'], 'exp': now + 300,
              'roles': STATE['roles']}
    if nonce is not None:
        claims['nonce'] = nonce
    if STATE['alg'] == 'none':
        return jwt.encode(claims, None, algorithm='none', headers={'kid': STATE['kid']})
    if STATE['alg'] == 'HS256':
        # Signed with the public key as an HMAC secret: the classic
        # algorithm-confusion forgery. PyJWT refuses to make one, so by hand.
        secret = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=')
        signing = b64(json.dumps({'alg': 'HS256', 'typ': 'JWT', 'kid': STATE['kid']}).encode()) + b'.' + b64(
            json.dumps(claims).encode())
        return (signing + b'.' + b64(hmac.new(secret, signing, hashlib.sha256).digest())).decode()
    key = OTHER_KEY if STATE['other_key'] else KEY
    return jwt.encode(claims, key, algorithm='RS256', headers={'kid': STATE['kid']})


def tokens(url, nonce):
    return {'access_token': 'a', 'token_type': 'Bearer', 'expires_in': 300,
            'refresh_token': 'refresh', 'refresh_expires_in': 1800, 'id_token': id_token(url, nonce)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/realms/test"

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = self.url()
        if self.path == '/realms/test/.well-known/openid-configuration':
            self.reply(200, {'issuer': STATE['discovery_issuer'] or url, 'authorization_endpoint': url + '/auth',
                             'token_endpoint': url + '/token', 'jwks_uri': url + '/jwks',
                             'end_session_endpoint': url + '/logout'})
        elif self.path == '/realms/test/jwks':
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
            if STATE['refresh_status']:
                return self.reply(STATE['refresh_status'], {})
            if STATE['refuse_refresh'] or form.get('refresh_token') != 'refresh':
                return self.reply(400, {'error': 'invalid_grant'})
            return self.reply(200, tokens(self.url(), None))
        self.reply(400, {'error': 'unsupported_grant_type'})


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()
