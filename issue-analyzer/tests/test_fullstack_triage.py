#!/usr/bin/env python3
"""Run: python -m unittest discover -s issue-analyzer/tests -v"""
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest

MODULE=pathlib.Path(__file__).resolve().parents[1]/"scripts"/"fullstack_triage.py"
spec=importlib.util.spec_from_file_location("fullstack_triage",MODULE)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class TriageTests(unittest.TestCase):
    def test_critical_event_after_500_repeated_warnings(self):
        with tempfile.TemporaryDirectory() as d:
            p=os.path.join(d,"rank0.log")
            with open(p,"w") as f:
                for _ in range(800): f.write("[ERROR] repeated warning\n")
                f.write("2026-10-17 10:10:01 Out of memory: Killed process 233 (python)\n")
            r=mod.scan(d,retain=2)
            self.assertEqual(r["matched_by_category"]["host_oom"],1)
            self.assertEqual(r["total_matches"],801)
            self.assertTrue(r["coverage"]["examples_truncated"])
            self.assertTrue(r["coverage"]["full_text_scan"])
    def test_no_false_oom_from_signal_only(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d,"worker.log"),"w") as f:f.write("Worker exit code 137\n")
            r=mod.scan(d,retain=10)
            self.assertNotIn("host_oom",r["matched_by_category"])
            self.assertEqual(r["matched_by_category"]["process_exit"],1)
    def test_gz_and_rank(self):
        import gzip
        with tempfile.TemporaryDirectory() as d:
            with gzip.open(os.path.join(d,"service.log.gz"),"wt") as f:f.write("2026-10-17 10:01:01 rank=5 HCCL timed out\n")
            r=mod.scan(d)
            self.assertEqual(r["matched_by_category"]["hccl_timeout"],1)
            self.assertEqual(r["events"][0]["rank"],5)

if __name__=="__main__":unittest.main()
