"""Single continuously running product-only worker, managed by launchd."""
import fcntl,time
from .config import ROOT
from .store import Store,now
from .vision import init,enqueue,work,MODEL,api_disabled

def run():
 if api_disabled(ROOT):return
 path=ROOT/'data/porcelain';path.mkdir(parents=True,exist_ok=True)
 with (path/'worker.lock').open('a') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return
  store=Store(ROOT/'data/curio.db');init(store)
  store.set('visual_worker_status','running')
  credential_stamp=(ROOT/'.env').stat().st_mtime_ns
  while True:
   store.set('visual_worker_heartbeat',now())
   try:
    with store.connect() as c:
     # Throttle all background inference after authentication/quota errors.
     error=c.execute("SELECT error FROM visual_jobs WHERE state IN ('retry','failed') AND ((error LIKE '%HTTP 429%' AND next_attempt>?) OR error LIKE '%HTTP 401%' OR error LIKE '%HTTP 403%') ORDER BY id DESC LIMIT 1",(time.time(),)).fetchone()
    if error and ('HTTP 401' in error[0] or 'HTTP 403' in error[0]) and (ROOT/'.env').stat().st_mtime_ns!=credential_stamp:
     credential_stamp=(ROOT/'.env').stat().st_mtime_ns
     with store.connect() as c:c.execute("UPDATE visual_jobs SET state='retry',attempts=0,next_attempt=0,error=NULL WHERE error LIKE '%HTTP 401%' OR error LIKE '%HTTP 403%'")
     error=None
    if error:
     store.set('visual_worker_status','paused_api');time.sleep(60);continue
    store.set('visual_worker_status','running')
    # Finish existing runnable work before fetching more, preventing unbounded queue growth.
    with store.connect() as c:ready=c.execute("SELECT count(*) FROM visual_jobs WHERE state IN ('queued','retry') AND next_attempt<=? AND attempts<3",(time.time(),)).fetchone()[0]
    if not ready:enqueue(store,1,MODEL)
    work(store,1)
   except Exception as error:
    # Never log exception values that may include credentials or API bodies.
    store.set('visual_worker_error',type(error).__name__)
   time.sleep(5)

if __name__=='__main__':run()
