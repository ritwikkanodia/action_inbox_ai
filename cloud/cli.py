"""Explicit runner-owned local synthetic roles; production startup is refused."""
import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
from uuid import UUID
from psycopg.conninfo import conninfo_to_dict

from cloud.database import Database
from cloud.dispatch import Dispatcher,Receiver
from cloud.recovery import Recovery
from cloud.scheduler import Scheduler
from cloud.synthetic import SyntheticExecutor,SyntheticGenerator
from cloud.telemetry import metrics
from cloud.types import Envelope
from cloud.worker import Worker


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role',choices=('worker','scheduler','dispatcher','metrics','restore-begin','restore-check','restore-resume'))
    parser.add_argument('--synthetic',action='store_true')
    parser.add_argument('--schema')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--epoch',type=UUID)
    parser.add_argument('--check')
    parser.add_argument('--reviewer')
    parser.add_argument('--reason')
    args=parser.parse_args()
    if not args.synthetic: parser.error('production_runner_not_integrated')
    dsn=os.environ.get('ATHENA_VERIFY_DSN','')
    marker=os.environ.get('ATHENA_VERIFY_MARKER','')
    fields=conninfo_to_dict(dsn)
    if (not re.fullmatch('[a-f0-9]{64}',marker) or not re.fullmatch('verify_[a-f0-9]{32}',args.schema or '')
        or fields.get('host')!='127.0.0.1' or fields.get('dbname')!='athena_verify' or fields.get('user')!='athena_verify'):
        parser.error('runner_owned_local_configuration_required')
    db=Database(dsn,args.schema)
    stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,lambda *_: stop.set())
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    worker=Worker(db,SyntheticExecutor(),SyntheticGenerator(),stop_event=stop)
    recovery=Recovery(db)
    while not stop.is_set():
        if args.role=='scheduler': Scheduler(db).once()
        elif args.role=='dispatcher': Dispatcher(db,lambda envelope: None).once()
        elif args.role=='worker':
            # Local broker-delivery simulation only. The production transport
            # remains ServiceBusTransport and requires a separately approved host.
            rows=db.read("""SELECT o.* FROM outbox o JOIN jobs j ON j.id=o.job_id
                WHERE o.published_at IS NOT NULL AND NOT o.quarantined AND j.state IN ('queued','retry_pending')
                ORDER BY o.created_at LIMIT 100""")
            for row in rows:
                claim=Receiver(db).handle(Envelope(row['version'],row['id'],row['job_id'],row['epoch']),'local-synthetic')
                if claim: worker.execute(claim); break
        elif args.role=='metrics': print(json.dumps(metrics(db))); return 0
        elif args.role=='restore-begin': print(str(recovery.begin_restore())); return 0
        elif args.role=='restore-check': recovery.review_check(args.epoch,args.check,args.reviewer,args.reason); return 0
        elif args.role=='restore-resume': recovery.resume_after_review(args.epoch,args.reviewer,args.reason); return 0
        if args.once: break
        stop.wait(30 if args.role=='scheduler' else 1)
    return 0


if __name__=='__main__': raise SystemExit(main())
