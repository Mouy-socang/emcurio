"""曝光衰减（故事 28；AD-15）：读发布历史，得每件货"在已发布 R12 上累计挂了几天"与排序扣分。

- 只读 `publishes`（非 dropped，按发布编号）与 `item_codes`；不新增表、不写库。规则在 curio/judge.exposure_*
  （经 curio/rules.py），参数在 config/scoring.yaml 的 exposure 段。
- 某次发布的 rank JSON 解析不了：跳过那次发布（记日志），不报错。
- 综合分不变，扣分只进排序键（02"按分数"、07 封存的 R12 预测）；03 每行显示天数与"还没卖出"提示。
"""
import json
import logging
from datetime import datetime, timezone

from . import rules
from .config import ROOT, scoring_rules_for
from .schedule import BERLIN

log = logging.getLogger(__name__)


def _when(text):
    t = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def publish_history(c, until=None):
    """[(发布时刻, [货号…])]：非 dropped 的发布（按发布编号）；until（UTC 时刻）给出时只取在它之前的。"""
    out = []
    for r in c.execute("SELECT publish_no, created_at, rank FROM publishes WHERE status!='dropped' ORDER BY publish_no"):
        try:
            at = _when(r['created_at'])
            codes = json.loads(r['rank'])
            if not isinstance(codes, list) or not all(isinstance(x, str) for x in codes):
                raise ValueError('rank 不是货号列表')
        except (TypeError, ValueError):
            log.warning('发布 %s 的 rank 解析失败，曝光天数跳过这次发布', r['publish_no'])
            continue
        if until is not None and at > until:
            continue
        out.append((at, codes))
    return out


def _utcnow():
    return datetime.now(timezone.utc)


def params_for(root=ROOT):
    return scoring_rules_for(root)['exposure']


def days_by_code(c, now=None):
    """{货号: 累计挂牌天数}（只含 > 0 的）。now：带时区的时刻，缺省为现在；按柏林日期计。"""
    now = (now or _utcnow()).astimezone(BERLIN)
    return rules.exposure_days(publish_history(c, now), now)


def by_item(c, params=None, now=None, root=ROOT):
    """{item_id: dict(days, penalty)}：只含挂过的货。params 为 exposure 段（缺省读 scoring.yaml）。"""
    params = params or params_for(root)
    days = days_by_code(c, now)
    if not days:
        return {}
    out = {}
    for r in c.execute('SELECT code, item_id FROM item_codes'):
        d = days.get(r[0])
        if d:
            out[r[1]] = dict(days=d, penalty=rules.exposure_penalty(d, params))
    return out


def penalty_of(exp, item_id):
    e = exp.get(item_id)
    return e['penalty'] if e else 0


def sort_key(score, penalty):
    return rules.exposure_sort_key(score, penalty)
