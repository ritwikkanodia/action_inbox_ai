"""Browser boundary: fixed host, explicit origin, CSRF and session context."""
import hmac
from urllib.parse import urlsplit
from flask import g, request, abort
from cloud.config import DEFAULTS
from cloud.identity.crypto import csrf_token
from cloud.identity.types import Forbidden
from cloud.types import Conflict, Rejected

SESSION_COOKIE = '__Host-athena-session'
PRELOGIN_COOKIE = '__Host-athena-prelogin'


def cookie_names(app):
    # No environment switch: only an explicitly testing app permits HTTP cookies.
    if app.testing and app.config['ATHENA_ORIGIN'].startswith('http://'):
        return 'athena-test-session', 'athena-test-prelogin'
    return SESSION_COOKIE, PRELOGIN_COOKIE


def cookie_options(app):
    return dict(secure=not (app.testing and app.config['ATHENA_ORIGIN'].startswith('http://')),
                httponly=True, samesite='Lax', path='/')


def clear_cookies(response, app):
    for name in cookie_names(app):
        response.delete_cookie(name, **cookie_options(app))
    return response


def require_csrf(raw_cookie, supplied, origin, referer, public_origin):
    evidence = origin
    if evidence is None and referer:
        try:
            parsed = urlsplit(referer)
            if parsed.username is not None or parsed.password is not None: raise ValueError()
            evidence = parsed.scheme + '://' + parsed.netloc
        except ValueError:
            raise Forbidden() from None
    if evidence != public_origin or not isinstance(raw_cookie,str) or not raw_cookie:
        raise Forbidden()
    if not isinstance(supplied,str) or not supplied.isascii() or not hmac.compare_digest(csrf_token(raw_cookie),supplied):
        raise Forbidden()


def resolve_request(sessions):
    from flask import current_app
    if 'identity_proof' not in g:
        raw = request.cookies.get(cookie_names(current_app)[0])
        g.identity_proof = sessions.resolve(raw)
    return g.identity_proof


def install_security(app, settings):
    if not settings.public_origin.startswith('https://') and not app.testing:
        raise ValueError('https_required')
    app.config.update(ATHENA_ORIGIN=settings.public_origin,MAX_CONTENT_LENGTH=DEFAULTS.body_bytes,
                      MAX_FORM_MEMORY_SIZE=DEFAULTS.body_bytes,MAX_FORM_PARTS=10)
    @app.before_request
    def browser_boundary():
        if request.host != urlsplit(settings.public_origin).netloc:
            abort(400)
        if not (request.path.startswith('/api/') or request.path == '/logout'):
            return
        proof = resolve_request(app.extensions['identity_sessions'])
        if request.method in ('POST','PUT','PATCH','DELETE'):
            if request.path.startswith('/api/') and not request.is_json:
                raise Rejected('invalid_body')
            raw = request.cookies.get(cookie_names(app)[0])
            csrf = request.headers.get('X-CSRF-Token') or request.form.get('csrf')
            context = request.headers.get('X-Athena-Context') or request.form.get('context_id')
            require_csrf(raw,csrf,request.headers.get('Origin'),request.headers.get('Referer'),settings.public_origin)
            if context != str(proof.context_id):
                raise Conflict('session_context_changed')

    @app.after_request
    def headers(response):
        response.headers.update({
            'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY',
            'Referrer-Policy':'no-referrer' if request.path=='/login' or request.path.startswith('/oauth/') else 'same-origin',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        })
        if settings.public_origin.startswith('https://'):
            response.headers['Strict-Transport-Security']='max-age=31536000'
        return response
