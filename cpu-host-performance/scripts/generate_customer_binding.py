"""Generate an offline customer-specific script from an extracted hostbound/ capture."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hostbind.customer import build, inspect, render
from hostbind.transaction import load_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hostbound',required=True,type=Path)
    parser.add_argument('--inspect',action='store_true',help='Inventory owners, all threads, hot others and gaps; no policy needed')
    for name in ('assessment','policy','out'):
        parser.add_argument('--'+name,type=Path)
    parser.add_argument('--network',type=Path)
    args=parser.parse_args()
    try:
        if args.inspect:
            print(json.dumps(inspect(args.hostbound),ensure_ascii=True,indent=2));return 0
        if not all((args.assessment,args.policy,args.out)): parser.error('Generation requires --assessment, --policy and --out')
        plan=build(args.hostbound,load_json(args.assessment),load_json(args.policy),
                   load_json(args.network) if args.network else None)
        render(plan,args.out)
        print('Generated %d TID and %d IRQ targets: %s'%(len(plan['actions']),len(plan['irq_actions']),args.out));return 0
    except (ValueError,KeyError,OSError,TypeError,RuntimeError) as exc:
        print('BLOCKED: '+str(exc),file=sys.stderr);return 2


if __name__=='__main__': sys.exit(main())
