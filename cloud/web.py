"""Production web composition. No local app, test provider or executor switch."""
import logging
from pathlib import Path
from flask import Flask
from cloud.database import Database
from cloud.http import register_routes
from cloud.identity.admission import Admission
from cloud.identity.config import WebSettings
from cloud.identity.flows import Flows
from cloud.identity.google import GoogleAdapter
from cloud.identity.guard import RequestDatabase
from cloud.identity.http import register_identity_routes
from cloud.identity.security import install_security, resolve_request
from cloud.identity.sessions import Sessions
from cloud.identity.transport import BoundedGoogleTransport
from cloud.types import Actor, Unavailable


def disabled_submission():
    raise Unavailable('executor_not_integrated')


def create_web_app(settings: WebSettings):
    settings=WebSettings.from_mapping({
        'ATHENA_DATABASE_URL':settings.database.dsn,'ATHENA_DATABASE_SCHEMA':settings.database.schema,
        'ATHENA_ENVIRONMENT':settings.environment,'ATHENA_PUBLIC_ORIGIN':settings.public_origin,
        'ATHENA_GOOGLE_CLIENT_ID':settings.google_client_id,'ATHENA_GOOGLE_CLIENT_SECRET':settings.google_client_secret,
        'ATHENA_AUTH_FLOW_KEY':settings.flow_key.decode('ascii'),
    })
    root=Path(__file__).resolve().parents[1]
    app=Flask(__name__,template_folder=str(root/'templates'),static_folder=str(root/'static'))
    app.config.update(DEBUG=False,TESTING=False)
    for name in ('oauthlib','google.auth','urllib3'):
        logging.getLogger(name).setLevel(logging.WARNING)
    db=Database(settings.database.dsn,settings.database.schema)
    sessions=Sessions(db)
    guarded=RequestDatabase(db,lambda:resolve_request(sessions))
    install_security(app,settings)
    register_routes(app,guarded,lambda:Actor(resolve_request(sessions).owner_id),
                    submit_gate=disabled_submission,include_ready=False)
    register_identity_routes(app,db,settings,Flows(db,settings.flow_key),Admission(db),sessions,
                             GoogleAdapter(settings,BoundedGoogleTransport()))
    from cloud.pages import Pages, register_page_routes
    register_page_routes(app,Pages(guarded,settings.flow_key),sessions)
    return app
