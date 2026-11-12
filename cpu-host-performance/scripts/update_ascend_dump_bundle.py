"""Refresh the native collector embedded in the single-file Bash distribution."""
import base64
import gzip
from pathlib import Path
import textwrap


def main():
    scripts = Path(__file__).resolve().parent
    source = scripts.parents[1] / 'ascend-dump-analyzer/references/msprechecker_dump.py'
    collector = scripts / 'cpu_trace_collect.sh'
    contents = collector.read_text(encoding='utf-8')
    marker = "<<'MSPRECHECKER_PAYLOAD'\n"
    start = contents.index(marker) + len(marker)
    end = contents.index('\nMSPRECHECKER_PAYLOAD\n', start)
    raw = source.read_bytes().replace(b'\r\n', b'\n')
    payload = base64.b64encode(gzip.compress(raw, mtime=0)).decode('ascii')
    updated = contents[:start] + textwrap.fill(payload, 76) + contents[end:]
    if updated != contents:
        collector.write_bytes(updated.encode('utf-8'))


if __name__ == '__main__':
    main()
