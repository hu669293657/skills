# CPU Host Performance Skill

Customer collection: `bash scripts/cpu_trace_collect.sh --duration 30 --output .`.

Offline analysis: `python3 analyzer/analyze_cpu_trace.py cpu_trace_*.tar.gz --output report`.

Dependencies are Bash/root/tracefs on the customer host and Python 3 standard library locally. It does not install software, use network, alter CPU governor/affinity/IRQ affinity, or retain ftrace configuration.
