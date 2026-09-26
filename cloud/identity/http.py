"""Identity-only web routes. Provider data is never returned or logged."""
import hashlib
from flask import jsonify, redirect, render_template, request, make_response
from itsdangerous import URLSafeTimedSerializer, BadSignature
import psycopg
from werkzeug.exceptions import HTTPException
from cloud.identity.crypto import new_token, valid_token, hash_token, csrf_token
from cloud.identity.guard import lock_runtime
from cloud.identity.security import cookie_names, cookie_options, clear_cookies, require_csrf, resolve_request
from cloud.identity.types import AuthenticationRequired, Forbidden, AdmissionDenied, RateLimited
from cloud.migrate import MIGRATIONS
from cloud.types import WorkError, Conflict, NotFound, StaleLease, Unavailable


def register_identity_routes(app, db, settings, flows, admission, sessions, google):
    app.extensions['identity_sessions'] = sessions
    signer = URLSafeTimedSerializer(settings.flow_key, salt='athena-prelogin-v1')
    session_name, prelogin_name = cookie_names(app)

    def browser_cookie():
        raw = request.cookies.get(prelogin_name)
        try:
            if not isinstance(raw,str) or len(raw)>4096: raise ValueError()
            payload=signer.loads(raw,max_age=600)
            if not valid_token(payload): raise ValueError()
            return raw
        except (BadSignature,ValueError,TypeError):
            raise Forbidden() from None

    def error_page(status, code):
        if request.path.startswith('/api/'):
            return make_response(jsonify(error=code),status)
        return make_response(render_template('cloud/error.html',status=status),status)

    @app.errorhandler(WorkError)
    def identity_error(error):
        status = (401 if isinstance(error,AuthenticationRequired) else
                  403 if isinstance(error,(Forbidden,AdmissionDenied)) else
                  429 if isinstance(error,RateLimited) or error.code.endswith('_capacity') else
                  404 if isinstance(error,NotFound) else
                  409 if isinstance(error,(Conflict,StaleLease)) else
                  503 if isinstance(error,Unavailable) else 400)
        response = (redirect('/login',303) if status==401 and not request.path.startswith('/api/')
                    else error_page(status,error.code))
        if status==401: clear_cookies(response,app)
        if isinstance(error,RateLimited): response.headers['Retry-After']=str(error.retry_after)
        return response

    @app.errorhandler(psycopg.Error)
    @app.errorhandler(ConnectionError)
    def unavailable(_):
        return error_page(503,'dependency_unavailable')

    @app.errorhandler(Exception)
    def unexpected(error):
        if isinstance(error,HTTPException):
            return error_page(error.code,'invalid_request')
        # No exception text/traceback: provider libraries may embed tokens.
        app.logger.error('cloud_request_failed')
        return error_page(503,'dependency_unavailable')

    @app.get('/login')
    def login():
        with db.transaction() as tx: lock_runtime(tx)
        try:
            raw=browser_cookie()
            old=db.read('SELECT status,epoch FROM auth_flows WHERE browser_hash=%s',(hash_token(raw),))
            if old and old[0]['status']!='pending': raise Forbidden()
        except Forbidden:
            raw=signer.dumps(new_token())
        response=make_response(render_template('cloud/login.html',csrf=csrf_token(raw)))
        response.set_cookie(prelogin_name,raw,max_age=600,**cookie_options(app))
        return response

    @app.post('/oauth/login')
    def start():
        raw=browser_cookie()
        require_csrf(raw,request.form.get('csrf'),request.headers.get('Origin'),None,settings.public_origin)
        flow=flows.begin(raw)
        return redirect(google.authorization_url(flow),302)

    @app.get('/oauth/login/callback')
    def callback():
        if len(request.query_string)>8192: raise Forbidden()
        if any(len(request.args.getlist(k))!=1 for k in request.args):
            raise Forbidden()
        state=request.args.get('state')
        raw=browser_cookie()
        claim=flows.consume(state,raw)
        try:
            if request.args.get('error') is not None: raise Forbidden()
            identity=google.exchange(request.args.get('code'),claim)
            issued=admission.complete(identity,claim,request.cookies.get(session_name))
        except Exception:
            try: flows.fail(claim)
            except (psycopg.Error,ConnectionError): pass
            raise
        response=redirect('/chat',303)
        response.set_cookie(session_name,issued.token,max_age=7*24*60*60,**cookie_options(app))
        response.delete_cookie(prelogin_name,**cookie_options(app))
        return response

    @app.post('/logout')
    def logout():
        sessions.revoke(resolve_request(sessions))
        return clear_cookies(redirect('/login',303),app)

    @app.post('/api/auth/logout-all')
    def logout_all():
        sessions.revoke(resolve_request(sessions),all_sessions=True)
        return clear_cookies(jsonify(status='signed_out'),app)

    @app.get('/live')
    def live():
        return jsonify(status='live')

    @app.get('/ready')
    def ready():
        try:
            with db.transaction() as tx:
                lock_runtime(tx)
                actual={row['version']:row['sha256'] for row in tx.execute('SELECT version,sha256 FROM schema_migrations')}
                expected={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in MIGRATIONS.glob('[0-9]*.sql')}
                if actual!=expected: raise Unavailable('schema_unavailable')
                for table in ('auth_sessions','auth_flows','auth_identities','auth_admission','auth_limits','auth_invites'):
                    from psycopg import sql
                    tx.execute(sql.SQL('SELECT 1 FROM {} LIMIT 0').format(sql.Identifier(table)))
        except (psycopg.Error,ConnectionError,Unavailable,OSError):
            return jsonify(status='unavailable'),503
        return jsonify(status='web_ready')
