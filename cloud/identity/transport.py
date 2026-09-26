"""Fixed-endpoint Google transport with no ambient proxy, redirects or retries."""
import json
import re
import threading
import time
import requests
from cloud.identity.types import ProviderResponse
from cloud.types import Unavailable

TOKEN_URL = 'https://oauth2.googleapis.com/token'
CERT_URL = 'https://www.googleapis.com/oauth2/v1/certs'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'


class BoundedGoogleTransport:
    def __init__(self):
        self._cert_lock = threading.Lock()
        self._certs = None
        self._certs_until = 0

    def request(self, method, url, *, body=None, headers=None):
        if (method, url) not in (('POST', TOKEN_URL), ('GET', CERT_URL)):
            raise Unavailable('identity_provider_unavailable')
        started = time.monotonic()
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.request(method, url, data=body, headers=headers, stream=True,
                                     allow_redirects=False, timeout=(3, 3), verify=True) as response:
                    if response.status_code != 200:
                        raise ValueError()
                    advertised = response.headers.get('Content-Length')
                    if advertised is not None and (int(advertised) < 0 or int(advertised) > 65536):
                        raise ValueError()
                    data = bytearray()
                    for chunk in response.iter_content(chunk_size=1):
                        if time.monotonic() - started > 15 or len(data) + len(chunk) > 65536:
                            raise ValueError()
                        data.extend(chunk)
                    value = json.loads(bytes(data))
                    if not isinstance(value, dict):
                        raise ValueError()
                    return ProviderResponse(200, bytes(data), dict(response.headers))
        except Exception:
            # Network/parse errors may embed URLs, form data or provider content.
            raise Unavailable('identity_provider_unavailable') from None

    def certificate_request(self, url, method='GET', body=None, headers=None, timeout=None):
        if url != CERT_URL or method != 'GET' or body:
            raise Unavailable('identity_provider_unavailable')
        with self._cert_lock:
            now = time.monotonic()
            if self._certs is not None and now < self._certs_until:
                return self._certs
            response = self.request('GET', CERT_URL)
            control = next((v for k, v in response.headers.items() if k.lower() == 'cache-control'), '')
            match = re.search(r'(?:^|,)\s*max-age=(\d+)\b', control)
            ttl = min(3600, int(match.group(1))) if match else 0
            if re.search(r'(?:^|,)\s*(no-store|no-cache)\b', control):
                ttl = 0
            self._certs, self._certs_until = response, time.monotonic() + ttl
            return response
