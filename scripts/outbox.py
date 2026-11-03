"""发件箱运维（AD-23）：查看、发测试 ping、处理被拒收而卡住的记录。

  uv run python scripts/outbox.py list [--all]       未送达的记录（--all 含已送达，最近 50 条）
  uv run python scripts/outbox.py ping [文字]          入队一条 ping，下一轮采集送出（部署后验证通道用）
  uv run python scripts/outbox.py requeue <seq>       清除该条的拒收标记，下次 flush 重发（修好服务器或契约后用）
  uv run python scripts/outbox.py drop <seq> --yes    删除一条未送达的记录（之后的记录才能继续发；不可撤销）

被拒收的记录（failed_at 有值）会让同步停在它这里（sync_status: blocked），不会跳过。
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from curio.config import ROOT
from curio.store import Store
from curio import sync


def cmd_list(store, show_all, out):
    with store.connect() as c:
        where = '' if show_all else 'WHERE sent_at IS NULL'
        rows = c.execute(f'SELECT seq,kind,created_at,attempts,last_error,sent_at,failed_at FROM outbox {where} '
                         'ORDER BY seq DESC LIMIT 50').fetchall()
    if not rows:
        out('发件箱没有未送达的记录' if not show_all else '发件箱为空')
    for r in reversed(rows):
        state = '已送达 ' + r['sent_at'] if r['sent_at'] else '被拒收（卡住）' if r['failed_at'] else '待发送'
        line = f"#{r['seq']} {r['kind']} {r['created_at']} 尝试 {r['attempts']} 次 · {state}"
        if r['last_error'] and not r['sent_at']:
            line += ' · ' + r['last_error']
        out(line)
    return 0


def cmd_ping(store, message, out):
    payload = {'schema_version': 1}
    if message:
        payload['message'] = message
    with store.connect() as c:
        event_id = sync.enqueue(c, 'ping', payload)
    out(f'已入队 ping：{event_id}（下一轮采集或 flush 时发送）')
    return 0


def cmd_requeue(store, seq, out):
    with store.connect() as c:
        n = c.execute('UPDATE outbox SET failed_at=NULL,last_error=NULL WHERE seq=? AND sent_at IS NULL', (seq,)).rowcount
    if not n:
        out(f'#{seq} 不存在或已送达')
        return 1
    out(f'#{seq} 已重新排队')
    return 0


def cmd_drop(store, seq, yes, out):
    if not yes:
        out('删除不可撤销：确认后加 --yes 再运行')
        return 2
    with store.connect() as c:
        n = c.execute('DELETE FROM outbox WHERE seq=? AND sent_at IS NULL', (seq,)).rowcount
    if not n:
        out(f'#{seq} 不存在或已送达')
        return 1
    out(f'#{seq} 已删除')
    return 0


def main(argv=None, out=print):
    ap = argparse.ArgumentParser(description='发件箱运维')
    ap.add_argument('--db', default=str(ROOT / 'data' / 'curio.db'))
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('list'); p.add_argument('--all', action='store_true')
    p = sub.add_parser('ping'); p.add_argument('message', nargs='?', default='')
    p = sub.add_parser('requeue'); p.add_argument('seq', type=int)
    p = sub.add_parser('drop'); p.add_argument('seq', type=int); p.add_argument('--yes', action='store_true')
    args = ap.parse_args(argv)
    store = Store(args.db)
    try:
        if args.cmd == 'list':
            return cmd_list(store, args.all, out)
        if args.cmd == 'ping':
            return cmd_ping(store, args.message, out)
        if args.cmd == 'requeue':
            return cmd_requeue(store, args.seq, out)
        return cmd_drop(store, args.seq, args.yes, out)
    except sync.ContractError as e:
        out('失败：' + str(e))
        return 1


if __name__ == '__main__':
    sys.exit(main())
