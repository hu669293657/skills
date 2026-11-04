import json
import re
import unittest
from analyzer.parser import TraceData, Event
from analyzer.metrics import compute_metrics
from analyzer.diagnosis import diagnose
from analyzer.report import render_md, render_html, svg_bar_chart, svg_line_chart, _esc, SEV_COLOR, SEV_CN, STATUS_CN


def sample(n=8):
    td=TraceData();td.snapshot={'cpu_online.txt':'0-%d'%(n-1),'numa.txt':'\n'.join('== /sys/devices/system/node/node%d/cpulist ==\n%d-%d'%(node,node*32,min(n-1,node*32+31)) for node in range((n+31)//32))}
    for c in range(n):
        td.events += [Event(0,c,'sched_switch',fields={'prev_pid':0,'next_pid':1000+c}),Event(2,c,'sched_switch',fields={'prev_pid':1000+c,'next_pid':0}),Event(10,c,'sched_switch',fields={'prev_pid':0,'next_pid':1000+c})]
    return compute_metrics(td)


def html(m):
    return render_html(m,diagnose(m),svg_bar_chart,svg_line_chart,_esc,SEV_COLOR,SEV_CN,STATUS_CN)


class ReportTests(unittest.TestCase):
    def test_all_core_sizes_and_numa(self):
        from analyzer.core_balance import core_data, render_core_balance
        for n in (8,64,256):
            with self.subTest(n=n):
                m=sample(n);d=core_data(m)
                self.assertEqual(len(d['cpus']),n)
                self.assertEqual(d['cpus'][-1]['cpu'],n-1)
                h=render_core_balance(m)
                self.assertIn('core-data',h)
                self.assertIn('core-style',h)
                self.assertIn('core-numa',h)
                self.assertIn('core-metrics',h)
                self.assertIn('core-static',h)
                self.assertNotIn('cdn',h.lower())
                if n==256: self.assertEqual(len(d['nodes']),8)

    def test_missing_metric_null(self):
        from analyzer.core_balance import core_data
        m=sample();m['per_cpu'][7]['util']=None
        d=core_data(m)
        self.assertIsNone(d['cpus'][7]['util'])
        self.assertIsNone(d['cpus'][0]['irq_ratio'])

    def test_html_safe_embedding(self):
        from analyzer.core_balance import safe_json
        self.assertNotIn('</script',safe_json({'name':'</script><script>alert(1)</script>'}))
        self.assertEqual(json.loads(safe_json({'x':'<&\u2028'})),{'x':'<&\u2028'})

    def test_six_explanations_in_section_two_both_formats(self):
        m=sample();h=html(m);md=render_md(m,diagnose(m))
        for text,start,end in ((h,"<h2 id='sec-2'>","<h2 id='sec-3'>"),(md,'## 2. 问题证据','## 3. 优化建议')):
            part=text[text.index(start):text.index(end,text.index(start))]
            for name in ('CPU 平均利用率','单核最高利用率','上下文切换率','调度延迟 P99','硬中断总占比','软中断总占比'):
                self.assertIn(name,part)
            for label in ('指标是什么','为什么需要关注','当前值说明','诊断结论','数据可信度'):
                self.assertGreaterEqual(part.count(label),6)
            self.assertIn('全机每秒',part)
            self.assertIn('样本',part)
            self.assertIn('缺失',part)

    def test_offline_report_no_fake_zero_or_external_scripts(self):
        m=sample(256);h=html(m)
        self.assertNotRegex(h,r'<script[^>]+src=')
        self.assertNotIn('0.00%</b>（0 次）',h)
        self.assertIn('CPU Frequency Analysis',h)
        self.assertIn('CPU Contention Analysis',h)
        self.assertIn('Process / Thread Discovery',h)
    def test_optional_tool_coverage_explicit(self):
        m=sample();h=html(m);md=render_md(m,diagnose(m))
        for text in (h,md):
            for name in ('GIL','msprof','function_monitor','NOT_PROVIDED'):
                self.assertIn(name,text)
    def test_missing_wakeup_source_stays_null(self):
        m=sample()
        self.assertIsNone(m['sched']['wakeup_total'])
        for text in (html(m),render_md(m,diagnose(m))):
            self.assertNotIn('唤醒总计 <b>0',text)
            self.assertNotIn('唤醒总计 **0',text)
