"""Local cheap/deep stage records. Phase one has no external model calls."""
import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


class NoopProvider:
    """Local identity provider. Never accesses the network."""
    def screen(self, product, rule_result):
        return dict(rule_result)


def process(connection, product, result, daily_budget=0, cheap=None, deep=None):
    """Reuse fingerprinted stage records; zero budget bypasses ALL providers.

    Phase one only accepts local NoopProvider implementations. There is no paid
    provider, SDK, external runner or automatic model fallback.
    """
    connection.execute("""CREATE TABLE IF NOT EXISTS screening_jobs(
        item_id TEXT, fingerprint TEXT, state TEXT, rule_result TEXT,
        cheap_result TEXT, deep_result TEXT, updated REAL,
        PRIMARY KEY(item_id,fingerprint))""")
    fp=digest({'id':product['item_id'],'price':product.get('listed_price'),
               'currency':product.get('currency'),'rule':result})
    state='excluded' if result['decision']=='skip' else 'ready'
    stages=[]
    current=dict(result)
    if state=='ready':
        for provider in (cheap,deep):
            if daily_budget<=0:
                stages.append({'status':'budget_zero','result':dict(current)})
            else:
                provider=provider or NoopProvider()
                if type(provider) is not NoopProvider:
                    raise ValueError('第一期仅支持 NoopProvider')
                current=provider.screen(product,current)
                stages.append({'status':'noop','result':dict(current)})
    else:stages=[None,None]
    connection.execute("""INSERT OR IGNORE INTO screening_jobs VALUES(?,?,?,?,?,?,?)""",
        (product['item_id'],fp,state,packed(result),packed(stages[0]),packed(stages[1]),time.time()))
    return dict(current)
