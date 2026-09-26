"""Recovery always runs; mailbox access requires an explicit injected provider."""
from cloud.gmail import Gmail
from cloud.recovery import Recovery
from cloud.types import Actor


class Scheduler:
    def __init__(self, db, poll=None): self.db,self.poll=db,poll

    def once(self):
        counts=Recovery(self.db).sweep()
        if self.poll is None: return counts
        connections=self.db.read("""SELECT c.id,c.owner_id FROM connections c JOIN owners o ON o.owner_id=c.owner_id
            LEFT JOIN mailbox_checkpoints m ON m.connection_id=c.id
            WHERE c.active AND o.enabled AND (m.connection_id IS NULL OR m.updated_at<clock_timestamp()-interval '30 seconds')
            ORDER BY c.id LIMIT 100""")
        gmail=Gmail(self.db)
        for connection in connections:
            claim=gmail.claim_poll(Actor(connection['owner_id']),connection['id'])
            if claim:
                page=self.poll(claim)  # No transaction is open here.
                counts['ingested']=counts.get('ingested',0)+gmail.ingest_page(claim,page)
        return counts
