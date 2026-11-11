"""Sign-in through the platform's OpenID Connect provider: the authorization
code flow with PKCE (S256), state and nonce, the ID token verified against the
issuer's keys, and a server-side session in memory.

Two levels only: a user whose role claim holds the operator role controls the
camera, any other signed-in user sees the page and the status. Sessions live
in this process, so a restart signs everybody out.

A session outlives its ID token: once the token has run out, the next request
renews it with the refresh token, so a session lasts as long as the issuer's
own does.
"""
import base64
import hashlib
import hmac
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import jwt
import requests
from flask import Response, redirect, request, session

VIEWER = 'viewer'
OPERATOR = 'operator'

# How long a login may take between /auth/login and the callback.
LOGIN_TTL = 600
TIMEOUT = 10
# Asymmetric only: the issuer's published keys are public.
ALGORITHMS = ['RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384', 'ES512']


def from_env(env):
    """The sign-in the OIDC_* variables describe, or None when OIDC_ISSUER is
    unset and the controller is open."""
    issuer = env.get('OIDC_ISSUER', '').strip()
    if not issuer:
        return None
    for name in ('OIDC_CLIENT_ID', 'OIDC_REDIRECT_URL'):
        if not env.get(name, '').strip():
            raise ValueError(f"{name} is required with OIDC_ISSUER")
    secret = ''
    if env.get('OIDC_CLIENT_SECRET_FILE'):
        with open(env['OIDC_CLIENT_SECRET_FILE']) as f:
            secret = f.read().strip()
    return Auth(
        issuer=issuer,
        client_id=env['OIDC_CLIENT_ID'].strip(),
        redirect_url=env['OIDC_REDIRECT_URL'].strip(),
        client_secret=secret,
        role_claim=env.get('OIDC_ROLE_CLAIM', '').strip() or 'groups',
        scopes=[s.strip() for s in (env.get('OIDC_SCOPES') or 'openid,profile,email').split(',') if s.strip()],
        operator_role=env.get('OIDC_OPERATOR_ROLE', '').strip() or 'visca-ptz-operator',
    )


def _random():
    return secrets.token_urlsafe(32)


def _text(body, code):
    # Plain text, so nothing an issuer puts into an error is rendered as HTML.
    return Response(body, status=code, mimetype='text/plain')


class Grant:
    """What the issuer said about a signed-in user, and until when. expires is
    the ID token's expiry; ends the refresh token's, None when the issuer did
    not say: past expires the grant is renewed, past ends it cannot be."""

    def __init__(self, role, user, id_token, refresh, expires, ends):
        self.role, self.user, self.id_token = role, user, id_token
        self.refresh, self.expires, self.ends = refresh, expires, ends
        # Keeps two requests from renewing one session at once.
        self.lock = threading.Lock()

    def renewable(self, now):
        if not self.refresh:
            return now < self.expires
        return self.ends is None or now < self.ends


class Auth:
    def __init__(self, issuer, client_id, redirect_url, client_secret='', role_claim='groups',
                 scopes=('openid', 'profile', 'email'), operator_role='visca-ptz-operator', now=time.time):
        for name, url in (('OIDC_ISSUER', issuer), ('OIDC_REDIRECT_URL', redirect_url)):
            u = urlsplit(url)
            if u.scheme not in ('http', 'https') or not u.netloc:
                raise ValueError(f"{name} {url!r} is not an http(s) URL")
        u = urlsplit(redirect_url)
        self.issuer, self.client_id, self.client_secret = issuer, client_id, client_secret
        # Given rather than derived from forwarded headers, which a client can
        # forge. Its origin is the one a change has to come from.
        self.redirect_url = redirect_url
        self.origin = f"{u.scheme}://{u.netloc}"
        # A Secure cookie is never sent over plain http, which a local run uses.
        self.secure = u.scheme == 'https'
        self.role_claim, self.scopes, self.operator_role = role_claim, list(scopes), operator_role
        self.now = now
        self._lock = threading.Lock()
        # Discovered on first use and again after a failure, so an issuer that
        # is down at start does not keep the controller from starting.
        self._provider = None
        self._keys = None
        self._sessions = {}

    def _discover(self):
        with self._lock:
            if self._provider is None:
                r = requests.get(self.issuer.rstrip('/') + '/.well-known/openid-configuration', timeout=TIMEOUT)
                r.raise_for_status()
                provider = r.json()
                if provider.get('issuer') != self.issuer:
                    raise ValueError(f"discovery names issuer {provider.get('issuer')!r}, not {self.issuer!r}")
                self._keys = jwt.PyJWKClient(provider['jwks_uri'], timeout=TIMEOUT)
                self._provider = provider
            return self._provider

    def _token(self, **params):
        data = dict(params, client_id=self.client_id)
        if self.client_secret:
            data['client_secret'] = self.client_secret
        r = requests.post(self._discover()['token_endpoint'], data=data, timeout=TIMEOUT)
        if not r.ok:
            raise ValueError(f"token endpoint answered {r.status_code}: {r.text[:200]}")
        return r.json()

    def _grant(self, token, nonce=None, refresh=''):
        """The grant in a token response, its ID token verified and, for a
        sign-in, its nonce checked. A response without a new refresh token
        keeps refresh, the one it was renewed with."""
        raw = token.get('id_token')
        if not raw:
            raise ValueError("the token response holds no ID token")
        self._discover()
        key = self._keys.get_signing_key_from_jwt(raw)
        claims = jwt.decode(raw, key.key, algorithms=ALGORITHMS, audience=self.client_id, issuer=self.issuer,
                            options={'require': ['exp', 'iat', 'sub']})
        if nonce is not None and not hmac.compare_digest(str(claims.get('nonce', '')), nonce):
            raise ValueError("the ID token's nonce is not this sign-in's")
        g = Grant(self._role(claims), self._name(claims), raw, token.get('refresh_token') or refresh,
                  claims['exp'], claims['exp'])
        # Keycloak says how long the refresh token lasts; without that the
        # grant is renewed until the issuer refuses.
        if g.refresh:
            n = int(token.get('refresh_expires_in') or 0)
            g.ends = self.now() + n if n > 0 else None
        return g

    def _role(self, claims):
        """The operator role among the role claim's values makes an operator,
        anything else a viewer. A Keycloak group path counts by its last
        element."""
        v = claims
        for k in self.role_claim.split('.'):
            v = v.get(k) if isinstance(v, dict) else None
        values = [v] if isinstance(v, str) else [s for s in v if isinstance(s, str)] if isinstance(v, list) else []
        if any(s.rsplit('/', 1)[-1] == self.operator_role for s in values):
            return OPERATOR
        return VIEWER

    @staticmethod
    def _name(claims):
        for k in ('preferred_username', 'email', 'sub'):
            if claims.get(k):
                return claims[k]
        return ''

    def current(self):
        """The grant of the browser making this request, renewed first when
        its ID token has run out, or None when it has none or the issuer
        refuses to renew it."""
        sid = session.get('sid')
        if not sid:
            return None
        with self._lock:
            g = self._sessions.get(sid)
        if g is None:
            return None
        with g.lock:
            now = self.now()
            if now < g.expires:
                return g
            if g.renewable(now):
                try:
                    n = self._grant(self._token(grant_type='refresh_token', refresh_token=g.refresh), refresh=g.refresh)
                    g.role, g.user, g.id_token, g.refresh, g.expires, g.ends = n.role, n.user, n.id_token, n.refresh, n.expires, n.ends
                    return g
                except Exception as e:
                    print(f"session of {g.user} not renewed: {e}")
        with self._lock:
            self._sessions.pop(sid, None)
        return None

    def same_origin(self):
        """Whether this request comes from the controller's own page. The
        session cookie alone does not show it: a page elsewhere can make a
        browser send it."""
        origin = request.headers.get('Origin')
        if origin:
            return origin == self.origin
        return request.headers.get('Sec-Fetch-Site') == 'same-origin'

    def login(self):
        try:
            provider = self._discover()
        except Exception as e:
            print(f"sign-in: issuer {self.issuer} not reachable: {e}")
            return _text("the sign-in provider is not reachable", 503)
        state, nonce, verifier = _random(), _random(), _random()
        # The login travels in the browser's signed session cookie, so a
        # callback is accepted only from the browser that started it.
        session['login'] = {'state': state, 'nonce': nonce, 'verifier': verifier,
                            'expires': self.now() + LOGIN_TTL}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        query = urlencode({
            'response_type': 'code', 'client_id': self.client_id, 'redirect_uri': self.redirect_url,
            'scope': ' '.join(self.scopes), 'state': state, 'nonce': nonce,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
        })
        endpoint = provider['authorization_endpoint']
        return redirect(endpoint + ('&' if '?' in endpoint else '?') + query)

    def callback(self):
        # The login is used up whatever comes of it.
        pending = session.pop('login', None)
        if (not pending or self.now() >= pending['expires']
                or not hmac.compare_digest(pending['state'], request.args.get('state', ''))):
            return _text("unknown or expired sign-in, start again at /auth/login", 400)
        if request.args.get('error'):
            return _text("sign-in refused: " + request.args['error'], 403)
        try:
            token = self._token(grant_type='authorization_code', code=request.args.get('code', ''),
                                redirect_uri=self.redirect_url, code_verifier=pending['verifier'])
            g = self._grant(token, nonce=pending['nonce'])
        except requests.RequestException as e:
            print(f"sign-in: issuer {self.issuer} not reachable: {e}")
            return _text("the sign-in provider is not reachable", 503)
        except Exception as e:
            print(f"sign-in failed: {e}")
            return _text("sign-in failed", 403)
        sid = _random()
        with self._lock:
            now = self.now()
            for k in [k for k, v in self._sessions.items() if not v.renewable(now)]:
                del self._sessions[k]
            self._sessions[sid] = g
        session['sid'] = sid
        print(f"signed in: {g.user} as {g.role}")
        return redirect('/')

    def logout(self):
        """Ends the session and, when the issuer advertises an end session
        endpoint, the session at the issuer too."""
        g = self.current()
        sid = session.pop('sid', None)
        with self._lock:
            self._sessions.pop(sid, None)
            end = (self._provider or {}).get('end_session_endpoint')
        if not end or g is None:
            return redirect('/')
        query = urlencode({'id_token_hint': g.id_token, 'post_logout_redirect_uri': self.origin + '/',
                           'client_id': self.client_id})
        return redirect(end + ('&' if '?' in end else '?') + query)
