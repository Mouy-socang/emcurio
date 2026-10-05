"""Read-only public product feed; never send customer records or credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]


def feed(root=ROOT, offset=0, limit=3):
    reports = sorted((root / 'data').glob('trial_screening_*/完整初筛.json'), reverse=True)
    if not reports:
        return {'items': [], 'note': '尚无客户证据试筛报告。'}
    report = reports[0]
    payload = json.loads(report.read_text())
    c = sqlite3.connect(f'file:{root / "data/curio.db"}?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    try:
        available = {r['item_id']: dict(r) for r in c.execute("""
            SELECT item_id,title,listed_price,total_price,currency,buying,url,checked_at
            FROM items i WHERE status='active' AND agent_decision!='skip'
            AND NOT EXISTS(SELECT 1 FROM cases c WHERE c.item_id=i.item_id)
        """)}
    finally:
        c.close()
    selected = []
    for result in payload['results']:
        if result['bucket'] != '优先复核': continue
        row = available.get(result['item']['item_id'])
        if not row: continue
        attrs = result.get('candidate_attributes', {})
        row['code'] = 'C' + hashlib.sha256(row['item_id'].encode()).hexdigest()[:8].upper()
        row['reason'] = '历史客户/订单有相似文字线索：' + '、'.join(attrs.get('maker', []) + attrs.get('decor', []) + attrs.get('form', []))
        row['image_file'] = ''
        relative = result['item'].get('local_image')
        if relative:
            image = (report.parent / relative).resolve()
            if image.is_relative_to(report.parent.resolve()) and image.is_file(): row['image_file'] = str(image)
        selected.append(row)
    return {'items': selected[offset:offset+limit], 'total': len(selected),
            'note': '历史试筛候选，非本轮新增；价格/状态读取本地数据库，未实时鉴定。试筛时间：' + payload['summary']['generated_at']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--offset', type=int, default=0)
    args = parser.parse_args()
    print(json.dumps(feed(offset=max(0, args.offset)), ensure_ascii=False))
