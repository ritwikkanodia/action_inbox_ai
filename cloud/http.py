"""Thin receipt/status handlers. Authentication never comes from request fields."""
from dataclasses import asdict
from functools import wraps
from uuid import UUID
from flask import jsonify, request
import psycopg
from werkzeug.exceptions import HTTPException, BadRequest, RequestEntityTooLarge

from cloud.control import Control
from cloud.types import Actor, Conflict, NotFound, Rejected, StaleLease, Submission, Unavailable, WorkError
from cloud.work import Work


def body(allowed, required=()):
    value = request.get_json()
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required).issubset(value):
        raise Rejected('invalid_body')
    return value


def readiness(db, principal):
    if principal is None: return {'reason':'authentication_not_integrated'}, 503
    if not db.read('SELECT enabled FROM runtime WHERE singleton')[0]['enabled']: return {'reason':'work_disabled'}, 503
    return {'status':'ready_for_synthetic_testing'}, 200


def register_routes(app, db, principal):
    work, control = Work(db), Control(db)
    def authenticated(function):
        @wraps(function)
        def handle(*args, **kwargs):
            actor = principal() if principal else None
            if not isinstance(actor, Actor): return jsonify(error='authentication_required'), 401
            return function(actor, *args, **kwargs)
        return handle

    @app.errorhandler(WorkError)
    def work_error(error):
        status = (404 if isinstance(error,NotFound) else 409 if isinstance(error,(Conflict,StaleLease))
                  else 503 if isinstance(error,Unavailable) else 429 if error.code.endswith('_capacity') else 400)
        return jsonify(error=error.code), status

    @app.errorhandler(psycopg.Error)
    @app.errorhandler(ConnectionError)
    def database_error(_): return jsonify(error='database_unavailable'), 503

    @app.errorhandler(BadRequest)
    @app.errorhandler(RequestEntityTooLarge)
    @app.errorhandler(ValueError)
    @app.errorhandler(TypeError)
    @app.errorhandler(UnicodeError)
    def bad_body(_): return jsonify(error='invalid_body'), 400

    @app.get('/ready')
    def ready(): return readiness(db, principal)

    @app.post('/api/work/conversations')
    @authenticated
    def create_conversation(actor):
        value = body({'kind','todo_id'}, {'kind'})
        cid = work.create_conversation(actor, value['kind'], UUID(value['todo_id']) if value.get('todo_id') else None)
        return jsonify(id=str(cid)), 201

    @app.get('/api/work/conversations/<uuid:cid>')
    @authenticated
    def snapshot(actor, cid): return jsonify(work.snapshot(actor, cid))

    @app.post('/api/work/conversations/<uuid:cid>/messages')
    @authenticated
    def submit(actor, cid):
        value = body({'text','generation','request_key','from_suggestion'}, {'text','generation','request_key'})
        receipt = work.accept(actor, Submission(cid, value['generation'], UUID(value['request_key']), value['text'], value.get('from_suggestion',False)))
        return jsonify(asdict(receipt)), 200 if receipt.replayed else 202

    @app.post('/api/work/jobs/<uuid:job_id>/cancel')
    @authenticated
    def cancel(actor, job_id): return jsonify(state=control.cancel(actor, job_id))

    @app.post('/api/work/conversations/<uuid:cid>/reset')
    @authenticated
    def reset(actor, cid):
        value = body({'generation'}, {'generation'})
        return jsonify(generation=control.reset(actor, cid, value['generation']))
