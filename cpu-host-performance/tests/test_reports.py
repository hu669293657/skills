import json
import re
import unittest
from analyzer.parser import TraceData, Event
from analyzer.metrics import compute_metrics
from analyzer.diagnosis import diagnose
from analyzer.report import render_html, svg_bar_chart, svg_line_chart, _esc, SEV_COLOR, SEV_CN, STATUS_CN


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

    def test_six_explanations_in_section_two_html(self):
        m=sample();h=html(m)
        for text,start,end in ((h,"<h2 id='sec-2'>","<h2 id='sec-3'>"),):
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
        m=sample();h=html(m)
        for text in (h,):
            for name in ('GIL','msprof','function_monitor','NOT_PROVIDED'):
                self.assertIn(name,text)
    def test_missing_wakeup_source_stays_null(self):
        m=sample()
        self.assertIsNone(m['sched']['wakeup_total'])
        for text in (html(m),):
            self.assertNotIn('唤醒总计 <b>0',text)
            self.assertNotIn('唤醒总计 **0',text)

class ReportDisclosureTests(unittest.TestCase):
    def test_selected_legacy_rankings_are_removed_without_losing_metrics(self):
        m=sample()
        m['tasks']['top_migration']=[(1001,'worker',7)]
        original=list(m['tasks']['top_runtime'])
        h=html(m)
        self.assertNotIn('Top 运行时间任务',h)
        self.assertNotIn('Top 迁移任务',h)
        self.assertIn('Top CPU Runtime Threads',h)
        self.assertIn('Top CPU Migration Threads',h)
        self.assertEqual(m['tasks']['top_runtime'],original)
        self.assertEqual(m['tasks']['top_migration'][0][2],7)

    def test_resource_risk_details_are_centralized_in_section_five(self):
        m=sample()
        marker='RESOURCE_RISK_UNIQUE_EVIDENCE'
        m['resources'].update(available=True,risks=[{'type':'MEMORY_PRESSURE','title':'内存风险测试',
            'evidence':marker,'evidence_state':'POTENTIAL','note':'资源候选需验证'}])
        h=html(m)
        five=h.split("<h2 id='sec-5'>",1)[1].split("<h2 id='sec-6'>",1)[0]
        self.assertIn(marker,five)
        self.assertNotIn(marker,h.replace(five,''))
        self.assertNotIn('2.4 机器资源风险',h)
        self.assertEqual(len(m['resources']['risks']),1)

    def test_dictionary_is_one_native_collapsed_help_block(self):
        h=html(sample())
        block=re.search(r'<details id="metric-help" class="report-help">(.*?)</details>',h,re.S)
        self.assertIsNotNone(block)
        self.assertIn('指标解释（每个指标是什么）',block.group(1))
        self.assertIn('事件/术语解释',block.group(1))
        self.assertNotIn('<details',block.group(1))
        self.assertIn('cpu_util',block.group(1))

    def test_full_core_table_collapsed_with_all_records_and_tools(self):
        from analyzer.report_sections import core_table
        h = core_table(sample(256), True)
        self.assertRegex(h, r'<details\b[^>]*class="[^"]*data-details')
        self.assertNotRegex(h, r'<details[^>]*\bopen\b')
        self.assertIn('256', h)
        self.assertIn('data-table-search', h)
        self.assertIn('data-table-sort', h)
        self.assertNotIn('<td>255</td>', h)
        self.assertIn('<td>99</td>', h)
        self.assertIn('report_data.json', h)

    def test_one_combined_frequency_idle_chart_embeds_policy_unique_data(self):
        from analyzer.report_sections import frequency_section
        from analyzer.frequency_analysis import analyze_frequency
        from analyzer.idle_analysis import analyze_idle
        m = sample()
        ev = [Event(1, 7, 'cpu_frequency', fields={'cpu_id': 0, 'state': 1000000}), Event(3, 7, 'cpu_frequency', fields={'cpu_id': 0, 'state': 2000000})]
        m['frequency'] = analyze_frequency(m, ev)
        m['idle'] = analyze_idle(m, [Event(2, 0, 'cpu_idle', fields={'state': 1}), Event(4, 0, 'cpu_idle', fields={'state': -1})])
        h = frequency_section(m, True)
        self.assertEqual(h.count('id="frequency-idle"'), 1)
        match = re.search(r'<script type="application/json" id="frequency-idle-data">(.*?)</script>', h, re.S)
        d = json.loads(match.group(1))
        self.assertEqual(len([s for s in d['series'] if s['kind'] == 'frequency']), 1)
        self.assertEqual(d['window'], [0, 10])
        self.assertTrue(any(v is None for s in d['series'] if s['kind'] == 'idle' for _, v in s['points']))
        for marker in ('fi-numa', 'fi-cpu', 'fi-policy', 'fi-series', 'fi-tooltip', 'MHz', 'Idle Residency'):
            self.assertIn(marker, h)

    def test_collection_evidence_controls_supplementation(self):
        from analyzer.npu_report import affinity_html, binding_html
        integrity = {'actions': [{'category': 'npu_process', 'status': 'PARSE_FAILED', 'reason': '原始输出存在；格式未识别', 'action': '扩展解析器并用已有原始文件重分析', 'recollect': False, 'command': ''}]}
        h = affinity_html({'rows': []}, integrity) + binding_html({'rows': []}, integrity)
        self.assertIn('格式未识别', h)
        self.assertNotIn('最小补采集', h)
        self.assertNotIn('--hostbound', h)

    def test_numa_capacity_distance_and_memory_evidence_are_separate(self):
        from analyzer.report_sections import topology_section
        m=sample(64)
        m['topology']={'nodes':[{'node':0,'cpus':list(range(32)),'distance':[10,20],'memory_kb':{'MemTotal':67108864,'MemFree':33554432},'errors':{}},{'node':1,'cpus':list(range(32,64)),'distance':[20,10],'memory_kb':{'MemTotal':67108864,'MemFree':16777216},'errors':{}}],'distance_node_order':[0,1],'cpu_count':64,'node_count':2}
        h=topology_section(m,True)
        self.assertIn('64',h)
        self.assertIn('64.00 GiB',h)
        self.assertIn('10, 20',h)
        self.assertIn('<details',h)
        self.assertIn('实际页',h)

    def test_irq_sources_beyond_top_ten_remain_offline(self):
        m=sample()
        names=[('irq_source_%d'%i,i+1,.001*(i+1)) for i in range(15)]
        m['irq'].update(top_names=names[:10],all_names=names)
        h=html(m)
        self.assertIn('irq_source_14',h)
        self.assertIn('全部硬中断来源',h)

    def test_npu_cards_keep_conclusions_visible_and_fold_full_evidence(self):
        from tests.test_npu_affinity import metrics, snapshots
        from analyzer.npu_report import affinity_html
        m=metrics(snapshots(),measured=True)
        h=affinity_html(m['npu_affinity'])
        self.assertNotIn('npu-evidence',h)
        self.assertNotIn('建议CPU / NUMA',h)
        self.assertIn('当前 allowed CPU',h)
        self.assertNotIn('依据、来源、内存与线程证据',h)
