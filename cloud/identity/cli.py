"""Explicit, audited cloud administration; never enables execution or loads dotenv."""
import argparse
import json
import os
from uuid import UUID
import psycopg
from cloud.database import Database
from cloud.identity.config import DatabaseSettings
from cloud.identity.admission import Admission
from cloud.identity.sessions import Sessions
from cloud.migrate import migrate
from cloud.types import WorkError


def main(argv=None, db=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for name, option in (('invite','email'),('revoke-invite','invite-id'),
                         ('disable-user','owner-id'),('revoke-sessions','owner-id')):
        command=sub.add_parser(name)
        command.add_argument('--'+option,required=True)
        command.add_argument('--actor',required=True)
        command.add_argument('--reason',required=True)
    sub.add_parser('purge')
    sub.add_parser('migrate')
    args=parser.parse_args(argv)
    try:
        if db is None:
            settings=DatabaseSettings.from_mapping(os.environ)
            db=Database(settings.dsn,settings.schema)
        if args.command=='migrate':
            migrate(db); result={'result':'migrated'}
        elif args.command=='invite':
            identifier=Admission(db).invite(args.email,args.actor,args.reason)
            result={'result':'invited','id':str(identifier)}
        elif args.command=='revoke-invite':
            Admission(db).revoke_invite(UUID(args.invite_id),args.actor,args.reason)
            result={'result':'invitation_revoked'}
        elif args.command=='disable-user':
            Sessions(db).disable(args.owner_id,args.actor,args.reason)
            result={'result':'identity_disabled'}
        elif args.command=='revoke-sessions':
            Sessions(db).revoke_owner(args.owner_id,args.actor,args.reason)
            result={'result':'sessions_revoked'}
        else:
            result={'result':'purged','counts':Sessions(db).purge()}
        print(json.dumps(result))
        return 0
    except (WorkError,ValueError,TypeError,psycopg.Error,ConnectionError,OSError):
        print(json.dumps({'result':'operation_failed'}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
