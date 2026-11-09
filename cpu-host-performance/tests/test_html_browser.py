"""Real offline browser checks, using an already installed local engine only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_reports import sample, html


class OfflineBrowserTests(unittest.TestCase):
    def test_core_problems_and_core_table_work_offline(self):
        node = shutil.which('node')
        module = os.environ.get('CPU_REPORT_PLAYWRIGHT', str(Path.home()/'AppData/Roaming/npm/node_modules/playwright/node_modules/playwright-core'))
        browsers = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH', str(Path.home()/'AppData/Local/ms-playwright')))
        # Node sees the browser runtime directory on Windows even in hosts that
        # restrict the Python runtime's filesystem projection.
        probe = "const fs=require('fs'),p=process.argv[1]; console.log(JSON.stringify(fs.existsSync(p)?fs.readdirSync(p).filter(x=>/^chromium-/.test(x)).flatMap(x=>fs.readdirSync(p+'/'+x).filter(y=>/^chrome-win/.test(y)).map(y=>p+'/'+x+'/'+y+'/chrome.exe')):[]))"
        engines = json.loads(subprocess.check_output([node,'-e',probe,str(browsers)],text=True)) if node else []
        installed = [os.environ.get('CPU_REPORT_BROWSER'), 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', shutil.which('chromium'), shutil.which('google-chrome')]
        engines += [path for path in installed if path and Path(path).exists()]
        if not node or not Path(module).exists() or not engines:
            self.skipTest('Existing local Node/playwright Chromium unavailable (no download attempted)')
        m = sample(256)
        with tempfile.TemporaryDirectory(prefix='cpu-report-browser-') as directory:
            report = Path(directory)/'report.html'
            report.write_text(html(m), encoding='utf-8')
            result = subprocess.run([node,str(Path(__file__).with_name('offline_report_browser.js')),module,str(engines[-1]),report.as_uri()],capture_output=True, text=True, encoding='utf-8',timeout=60)
        self.assertEqual(result.returncode,0, result.stdout+'\n'+result.stderr)
        self.assertIn('all offline browser interactions passed',result.stdout)
