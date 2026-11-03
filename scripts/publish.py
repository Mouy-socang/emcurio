"""设置今日榜与发布快照（故事 10 的临时入口；故事 11 用后台排榜与预览界面替代，命令保留）。

  uv run python scripts/publish.py rank                       显示当前的榜
  uv run python scripts/publish.py rank CO-0001 … CO-0010     按名次设置榜（至多 10 件，须都在商品池里；不足 10 件不能发布）
  uv run python scripts/publish.py publish                    生成完整快照（转码图片）并入发件箱；下一轮采集或 flush 时
                                                              先传图、经服务器确认，再提交快照
  uv run python scripts/publish.py publish --flush            入队后立刻 flush 一次

同一时刻只允许一个发布：另一个发布进行中时直接拒绝。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from curio import publish, sync
from curio.config import ROOT
from curio.store import Store


def cmd_rank(store, codes, out):
    with store.connect() as c:
        if codes:
            publish.set_rank(c, codes)
        current = publish.rank(c)
    if codes:
        out(f'榜已设为 {len(current)} 件' + ('' if len(current) == publish.RANK_SIZE else
                                            f'（还差 {publish.RANK_SIZE - len(current)} 件才能发布）'))
    out('当前的榜：' + (' '.join(current) if current else '（空）'))
    return 0


def cmd_publish(store, root, out, fetch=None, flush=False):
    r = publish.publish(store, root, fetch=fetch)
    line = (f'已入队发布 #{r["publish_no"]}：榜 {r["rank"]} 件、商品池 {r["pool"]} 件、墓碑 {r["tombstones"]} 件、'
            f'图片 {r["images"]} 张')
    if r['image_failures']:
        line += f'（{r["image_failures"]} 张图片未能转码，已跳过）'
    if flush:
        s = sync.flush(store, root)
        line += f'；同步：{s["sync_status"]}' + (f'（{s["sync_error"]}）' if s.get('sync_error') else '')
    out(line)
    return 0


def main(argv=None, out=print, fetch=None):
    ap = argparse.ArgumentParser(description='设置今日榜与发布快照')
    ap.add_argument('--db', default=str(ROOT / 'data' / 'curio.db'))
    ap.add_argument('--root', default=str(ROOT), help='仓库根（.env 与本机已下载图片所在）')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('rank')
    p.add_argument('codes', nargs='*')
    p = sub.add_parser('publish')
    p.add_argument('--flush', action='store_true')
    args = ap.parse_args(argv)
    store = Store(args.db)
    try:
        if args.cmd == 'rank':
            return cmd_rank(store, args.codes, out)
        return cmd_publish(store, Path(args.root), out, fetch, args.flush)
    except (publish.PublishError, sync.ContractError) as e:
        out('失败：' + str(e))
        return 1


if __name__ == '__main__':
    sys.exit(main())
