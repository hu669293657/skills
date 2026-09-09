# -*- coding: utf-8 -*-
"""python -m cpu_host_performance 入口"""
import sys
from .analyze_cpu_trace import main

if __name__ == "__main__":
    sys.exit(main())
