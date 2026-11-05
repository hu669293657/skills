import unittest

from analyzer.npu_parser import parse_npu_info, parse_npu_mapping, parse_npu_topology


class NpuParserTests(unittest.TestCase):
    def test_separate_columns_and_continuation_rows(self):
        result = parse_npu_info('| NPU ID | Chip ID | Process ID | Process name | Process memory(MB) |\n| 2 | 1 | 100 | python worker | 25 |\n|   |   | 101 | helper | 3 |')
        self.assertEqual([(p['npu'], p['chip'], p['pid']) for p in result['processes']], [(2, 1, 100), (2, 1, 101)])

    def test_mapping_device_ids_are_not_cpu_affinity(self):
        result = parse_npu_mapping('| NPU ID | Chip ID | Chip Logic ID | Chip Phy ID |\n| 0 | 1 | 7 | 9 |')
        self.assertEqual(result['devices'][0]['device_id'], 7)
        self.assertEqual(result['devices'][0]['cpu_affinity'], '')

    def test_explicit_numeric_cpu_mapping(self):
        result = parse_npu_mapping('| NPU ID | Chip ID | CPU Affinity | NUMA ID |\n| 0 | 1 | 8-11,20-23 | 2 |')
        self.assertEqual(result['devices'][0]['cpu_affinity'], '8-11,20-23')
        self.assertEqual(result['devices'][0]['numa_id'], 2)

    def test_topology_matrix_numbers_without_affinity_header_are_not_cpu_map(self):
        result = parse_npu_topology('NPU0 NPU1 Distance\nNPU0 X HCCS 0\nNPU1 HCCS X 1')
        self.assertTrue(all(not p['cpu_affinity'] for p in result['devices']))

    def test_empty_and_unsupported_output_remain_distinct(self):
        self.assertEqual(parse_npu_info('No running processes found')['processes'], [])
        self.assertEqual(parse_npu_info('No running processes found')['format'], 'EMPTY')
        self.assertTrue(parse_npu_info('unexpected vendor text')['errors'])

    def test_malformed_process_row_is_not_no_active_process(self):
        result = parse_npu_info('| NPU ID | Chip ID | Process ID | Process name | Process memory(MB) |\n| 0 | 0 | invalid-pid | python | 10 |')
        self.assertTrue(result['errors'])

    def test_numeric_topology_matrix_labels_are_not_cpu_ids(self):
        result = parse_npu_topology('NPU  NPU0  NPU1\n0  X  HCCS\n1  HCCS  X')
        self.assertEqual(result['errors'], [])
        self.assertEqual([p['npu'] for p in result['devices']], [0, 1])
        self.assertEqual([p['cpu_affinity'] for p in result['devices']], ['', ''])

    def test_trailing_numa_affinity_column_is_not_mistaken_for_cpu(self):
        result = parse_npu_topology('NPU NPU0 NPU1 CPU Affinity NUMA Affinity\nNPU0 X HCCS 0-23 0\nNPU1 HCCS X 24-47 1')
        self.assertEqual([(d['cpu_affinity'], d['numa_id']) for d in result['devices']], [('0-23',0),('24-47',1)])

    def test_unlabelled_trailing_value_after_cpu_column_blocks_cpu_guess(self):
        result = parse_npu_topology('NPU NPU0 CPU Affinity Unknown\nNPU0 X 0-23 9')
        self.assertEqual(result['devices'][0]['cpu_affinity'], '')


if __name__ == '__main__':
    unittest.main()
