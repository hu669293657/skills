import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'analyzer'))
if len(sys.argv) > 1 and sys.argv[1] == 'analyze':
    sys.argv.pop(1)
from analyze_cpu_trace import main
main()
