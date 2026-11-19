"""Generate an offline review proposal; never invokes taskset or writes sysfs."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hostbind.custom import recommend


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    try:
        result=recommend(json.loads(args.input.read_text(encoding='utf-8-sig')))
        with args.output.open('x',encoding='utf-8') as handle:
            json.dump(result,handle,ensure_ascii=False,indent=2,allow_nan=False)
        print(result['status']+': '+str(args.output))
        for issue in result['issues']:print(issue)
        return 0 if result['status']=='PROPOSAL' else 2
    except (OSError,ValueError,TypeError) as exc:
        print('ERROR: '+str(exc),file=sys.stderr);return 2


if __name__=='__main__':sys.exit(main())
