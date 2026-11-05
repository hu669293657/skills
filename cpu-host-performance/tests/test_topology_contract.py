import json
import unittest
from analyzer.parser import TraceData
from analyzer.metrics import compute_metrics


class TopologyContractTests(unittest.TestCase):
    def test_memory_only_numa_node_remains_in_node_count_and_distance(self):
        td = TraceData()
        td.snapshot = {'hostbound/numa_topology.json': json.dumps({
            'online_cpus': [0], 'online_nodes': [0, 2], 'distance_node_order': [0, 2],
            'nodes': [{'node': 0, 'cpus': [0], 'distance': [10, 30], 'memory_kb': {'MemTotal': 10}},
                      {'node': 2, 'cpus': [], 'distance': [30, 10], 'memory_kb': {'MemTotal': 100}}]})}
        data = compute_metrics(td)['topology']
        self.assertEqual(data['numa_count'], 2)
        self.assertEqual(data['nodes'][1]['cpus'], [])
        self.assertEqual(data['nodes'][1]['total_kb'], 100)
        self.assertEqual(data['nodes'][1]['distance_by_node'], {0: 30, 2: 10})

    def test_sparse_nodes_distance_memory_and_unmapped_cpu(self):
        td = TraceData()
        td.snapshot = {'cpu_online.txt': '0-3', 'hostbound/numa_topology.json': json.dumps({
            'online_cpus': [0, 1, 2, 3], 'distance_node_order': [0, 2],
            'nodes': [{'node': 0, 'cpus': [0, 1], 'distance': [10, 24], 'memory_kb': {'MemTotal': 1024, 'MemFree': 300}},
                      {'node': 2, 'cpus': [2], 'distance': [24, 10], 'memory_kb': {'MemTotal': 2048}, 'errors': {'meminfo': 'partial'}}]})}
        topo = compute_metrics(td)['topology']
        self.assertEqual(topo['cpu_count'], 4)
        self.assertEqual(topo['numa_count'], 2)
        self.assertEqual(topo['cpu_to_numa'], {0: 0, 1: 0, 2: 2})
        self.assertEqual(topo['nodes'][1]['distance_by_node'], {0: 24, 2: 10})
        self.assertIsNone(topo['nodes'][1]['free_kb'])
        self.assertEqual(topo['unmapped_cpus'], [3])

    def test_legacy_numactl_memory_units_and_unknown_node_not_zero(self):
        td = TraceData()
        td.snapshot = {'hostbound/numactl.txt': 'node 3 cpus: 4 5\nnode 3 size: 2 GB\nnode 3 free: 512 MB\nnode distances:\nnode   3\n  3: 10'}
        topo = compute_metrics(td)['topology']
        self.assertEqual(topo['nodes'][0]['total_kb'], 2097152)
        self.assertEqual(topo['nodes'][0]['free_kb'], 524288)
        self.assertEqual(topo['nodes'][0]['distance_by_node'], {3: 10})
        empty = compute_metrics(TraceData())['topology']
        self.assertEqual(empty['nodes'], [])
        self.assertIsNone(empty['numa_count'])


if __name__ == '__main__':
    unittest.main()
