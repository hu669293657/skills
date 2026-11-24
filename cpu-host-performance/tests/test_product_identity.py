import json
import tempfile
import unittest
from pathlib import Path

from analyzer.product_identity import DMI, NPU, detect_product, read_product


class ProductIdentityTests(unittest.TestCase):
    def test_model_names_and_aliases(self):
        for model, expected in (
            ('Atlas 800T A2', 'A2'), ('Atlas 900T PoD A2', 'A2'), ('A800I 2UP', 'A2'),
            ('Atlas9000 RCK A3', 'A3'), ('A9000 RCK A3', 'A3'), ('A800I-A3', 'A3'),
            ('Atlas 800I A5', 'A5'), ('Atlas 950 SuperPoD', 'A5')
        ):
            with self.subTest(model=model):
                result = detect_product({'hostbound/' + DMI: model})
                self.assertEqual((result['generation'], result['status']), (expected, 'identified'))
                self.assertEqual(result['evidence'][0]['value'], model)

    def test_only_product_fields_are_device_evidence(self):
        result = detect_product({DMI: 'unknown OEM',
                                 NPU: 'Firmware Version: A2\nProduct Type : Atlas 800I A3\nSerial Number: A5'})
        self.assertEqual(result['generation'], 'A3')
        for model in ('A3', 'ACME A3', 'Atlas 800', 'Ascend 910A3', '910B3', 'DB-A3-100'):
            with self.subTest(model=model):
                self.assertEqual(detect_product({DMI: model})['status'], 'unknown')
        for value in ('This device does not support querying product (Atlas A3)',
                      'Chip Name: Ascend910B3\nFirmware Version: A3',
                      'Product Type: Atlas 800I A30', 'Product Type: ASCEND910A3'):
            with self.subTest(value=value):
                self.assertEqual(detect_product({NPU: value})['status'], 'unknown')

    def test_conflicting_or_failed_sources(self):
        for npu in ('Product Type: A3', 'Product Type: Atlas 800T A2\nProduct Name: A800T A3',
                    'Product Type: Atlas 800T A2/A3'):
            with self.subTest(npu=npu):
                data = detect_product({DMI: 'Atlas 800I A2', NPU: npu})
                self.assertEqual((data['status'], data['generation']), ('conflict', 'unknown'))
        snapshot = {NPU: 'Product Type: Atlas 800I A3\nI/O error',
                    'collection_manifest.json': json.dumps({'entries': [
                        {'raw_path': 'hostbound/' + NPU, 'status': 'EXECUTION_FAILED', 'returncode': 1}]})}
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(read_product(folder)['status'], 'unknown')
            for name, raw in snapshot.items():
                (Path(folder) / name).write_text(raw, encoding='utf-8')
            self.assertEqual(detect_product(snapshot), read_product(folder))
            self.assertEqual(read_product(folder)['status'], 'unknown')
            (Path(folder) / DMI).write_text('Atlas 800I A5', encoding='utf-8')
            self.assertEqual(read_product(folder)['generation'], 'A5')
