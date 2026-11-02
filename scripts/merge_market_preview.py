"""Idempotently merge preview curation into the main Curio database."""
from pathlib import Path
import json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from curio.config import ROOT
from curio.store import Store
from curio import market
store=Store(ROOT/'data/curio.db')
market.init(store)
print(json.dumps(market.migrate_legacy(ROOT.parent/'outputs/market-feasibility/manual-decisions.sqlite'),ensure_ascii=False))
