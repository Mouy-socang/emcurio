"""Download only public listing thumbnails for the trial shortlist."""
import concurrent.futures
import hashlib
import json
import urllib.request
import urllib.parse
from trial_customer_screen import OUT, render

def fetch(item):
    url = item.get('image') or ''
    if urllib.parse.urlparse(url).scheme != 'https' or urllib.parse.urlparse(url).hostname not in ('i.ebayimg.com', 'img.kleinanzeigen.de'):
        return item['item_id'], None
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'Curio local preview archive/1.0'})
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get_content_type()
            ext = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}.get(content_type)
            if not ext: return item['item_id'], None
            data = response.read(8_000_001)
            if len(data)>8_000_000: return item['item_id'], None
        filename = 'images/' + hashlib.sha256(item['item_id'].encode()).hexdigest()[:20] + ext
        (OUT / filename).write_bytes(data)
        return item['item_id'], filename
    except Exception:
        return item['item_id'], None

if __name__ == '__main__':
    payload = json.loads((OUT/'完整初筛.json').read_text())
    (OUT/'images').mkdir(exist_ok=True)
    selected = [r['item'] for r in payload['results'] if r['item']['item_id'] in payload['shortlist_item_ids']]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        paths = dict(pool.map(fetch, selected))
    for r in payload['results']:
        if paths.get(r['item']['item_id']): r['item']['local_image'] = paths[r['item']['item_id']]
    payload['summary']['local_preview_images'] = sum(bool(v) for v in paths.values())
    (OUT/'完整初筛.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    render(payload)
    print(json.dumps({'downloaded_images': payload['summary']['local_preview_images'], 'requested': len(selected)}, ensure_ascii=False))
