"""Synthetic lifecycle: deadline/heartbeat authority is checked outside executor code."""
import threading
import time

from cloud.config import DEFAULTS
from cloud.leases import Leases
from cloud.telemetry import emit
from cloud.todos import Todos
from cloud.types import Rejected, StaleLease


class Worker:
    def __init__(self, db, executor, generator, *, config=DEFAULTS, stop_event=None):
        self.db,self.executor,self.generator,self.config=db,executor,generator,config
        self.stop_event=stop_event or threading.Event()

    def execute(self, claim):
        if claim is None: return
        leases=Leases(self.db,self.config)
        try:
            payload=leases.payload(claim)
            context=leases.context(claim) if payload['kind']!='gmail_generation' else None
        except StaleLease: return
        started=time.monotonic()
        emit('attempt_started',job_id=claim.job_id,attempt=claim.attempt)
        finished, result=threading.Event(),{}
        component=self.generator if payload['kind']=='gmail_generation' else self.executor
        def run():
            try:
                result['value']=(component.generate(payload['input']) if payload['kind']=='gmail_generation' else component.run(claim,context))
            except Exception:
                result['failed']=True
            finally: finished.set()
        thread=threading.Thread(target=run,daemon=True,name='athena-synthetic-executor')
        thread.start()
        next_heartbeat=time.monotonic()+self.config.heartbeat_seconds
        while not finished.wait(min(.1,self.config.heartbeat_seconds)):
            authority=True
            if self.stop_event.is_set():
                authority=False
                leases.fail(claim,True,'worker_shutdown')
            elif time.monotonic()>=next_heartbeat:
                try: authority=leases.heartbeat(claim)
                except Exception: authority=False
                next_heartbeat=time.monotonic()+self.config.heartbeat_seconds
            if not authority:
                if hasattr(component,'stop'): component.stop(claim)
                thread.join(timeout=.2)
                emit('lease_lost',job_id=claim.job_id,attempt=claim.attempt)
                return
        if self.stop_event.is_set():
            leases.fail(claim,True,'worker_shutdown')
            return
        try:
            if result.get('failed'): leases.fail(claim,True,'executor_failed')
            elif payload['kind']=='gmail_generation':
                decision,todo=result['value']
                Todos(self.db).record_generation(claim,decision,todo)
            elif not isinstance(result['value'],str): leases.fail(claim,False,'invalid_executor_output')
            else: leases.finish(claim,result['value'])
        except StaleLease: pass
        except (Rejected,ValueError,TypeError): leases.fail(claim,False,'invalid_executor_output')
        emit('attempt_finished',job_id=claim.job_id,attempt=claim.attempt,seconds=time.monotonic()-started)
