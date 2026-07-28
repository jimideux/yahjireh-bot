#!/usr/bin/env python3
"""Decision logger — records every signal the bot would act on,
so dry-run behavior can be reviewed objectively later."""
import json, time
LOG = '/root/trading/decisions.jsonl'

def log_decision(pair, direction, price, extra=None):
    rec = {
        'ts': time.time(),
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'pair': pair,
        'direction': direction,
        'price': price,
    }
    if extra: rec.update(extra)
    try:
        with open(LOG,'a') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception as e:
        print(f'declog error: {e}')

def read_all():
    out=[]
    try:
        for line in open(LOG):
            out.append(json.loads(line))
    except FileNotFoundError:
        pass
    return out

if __name__ == '__main__':
    recs = read_all()
    print(f'{len(recs)} decisions logged')
    for r in recs[-20:]:
        print(f"{r['time']}  {r['pair']:<12}{r['direction']:<6}@ {r.get('price')}")
