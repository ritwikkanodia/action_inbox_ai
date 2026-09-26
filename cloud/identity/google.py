"""Google identity-only exchange. JWT verification is delegated to google-auth."""
import hmac
import time
from google.auth import jwt
from google.oauth2 import id_token
from oauthlib.oauth2 import WebApplicationClient

from cloud.identity.admission import normalize_email
from cloud.identity.crypto import hash_token
from cloud.identity.types import Forbidden, GoogleIdentity
from cloud.identity.transport import AUTH_URL, TOKEN_URL
from cloud.types import Unavailable


class GoogleAdapter:
    def __init__(self, settings, transport):
        self.settings, self.transport = settings, transport

    def authorization_url(self, start):
        return WebApplicationClient(self.settings.google_client_id).prepare_request_uri(
            AUTH_URL, redirect_uri=self.settings.public_origin + '/oauth/login/callback',
            scope=['openid', 'email', 'profile'], state=start.state, nonce=start.nonce,
            code_challenge=start.challenge, code_challenge_method='S256')

    def exchange(self, code, claim):
        if (not isinstance(code, str) or not 1 <= len(code) <= 4096
                or not code.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in code)):
            raise Forbidden()
        try:
            client = WebApplicationClient(self.settings.google_client_id)
            body = client.prepare_request_body(
                code=code, redirect_uri=self.settings.public_origin + '/oauth/login/callback',
                code_verifier=claim.verifier, client_secret=self.settings.google_client_secret)
            response = self.transport.request('POST', TOKEN_URL, body=body.encode('ascii'),
                headers={'Content-Type':'application/x-www-form-urlencoded', 'Accept':'application/json'})
            if response.status != 200 or len(response.data) > 65536:
                raise ValueError()
            parsed = client.parse_request_body_response(response.data.decode('utf-8'))
            token = parsed.get('id_token')
            if not isinstance(token, str) or not 1 <= len(token.encode()) <= 16384:
                raise ValueError()
            if jwt.decode_header(token).get('alg') != 'RS256':
                raise ValueError()
            claims = id_token.verify_oauth2_token(token, self.transport.certificate_request,
                audience=self.settings.google_client_id, clock_skew_in_seconds=0)
            if (claims.get('aud') != self.settings.google_client_id
                    or ('azp' in claims and claims['azp'] != self.settings.google_client_id)
                    or claims.get('iss') not in ('accounts.google.com', 'https://accounts.google.com')
                    or type(claims.get('iat')) is not int or type(claims.get('exp')) is not int
                    or not claims['iat'] <= time.time() < claims['exp']
                    or claims.get('email_verified') is not True):
                raise ValueError()
            sub, nonce = claims.get('sub'), claims.get('nonce')
            if (not isinstance(sub, str) or not 1 <= len(sub) <= 255 or not sub.isascii()
                    or any(ord(c) < 33 or ord(c) > 126 for c in sub)
                    or not isinstance(nonce, str) or not hmac.compare_digest(hash_token(nonce), claim.nonce_hash)):
                raise ValueError()
            email = normalize_email(claims.get('email'))
            name, domain = claims.get('name', email), claims.get('hd')
            if (not isinstance(name, str) or len(name) > 200
                    or (domain is not None and (not isinstance(domain, str) or not 1 <= len(domain) <= 253
                                                or not domain.isascii() or any(c.isspace() for c in domain)))):
                raise ValueError()
            return GoogleIdentity(sub, email, name, domain)
        except Unavailable:
            raise
        except Exception:
            raise Forbidden() from None
