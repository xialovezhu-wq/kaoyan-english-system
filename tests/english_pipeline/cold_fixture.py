"""Explicit fixture-only replacement for the central T9 volume and registry API."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def install_cold_fixture(test, root: Path):
    cold = (root / 'cold-volume' / '02_英语' / '网页交接与过程资料').resolve()
    cold.mkdir(parents=True, exist_ok=True)
    records = []

    def ensure(path, subject):
        resolved = Path(path).resolve()
        if subject != 'english' or not resolved.is_relative_to(cold.parent):
            raise ValueError('fixture rejects non-T9 output')
        return resolved

    def register(path, *, subject, purpose, aliases=()):
        ensure(path, subject)
        receipt = cold / '.test-registry' / (str(len(records)) + '.json')
        receipt.parent.mkdir(exist_ok=True)
        receipt.write_text(json.dumps({'root': str(path)}))
        row = {'status': 'registered', 'path': str(path), 'purpose': purpose, 'aliases': list(aliases),
               'receipt_path': str(receipt), 'receipt_sha256': hashlib.sha256(receipt.read_bytes()).hexdigest()}
        records.append(row)
        return row

    central = SimpleNamespace(ensure_output_path=ensure, register_tree=register)
    for patcher in (patch('english_pipeline.cold_storage.COLD_ROOT', cold),
                    patch('english_pipeline.cold_storage._central', return_value=central)):
        patcher.start()
        test.addCleanup(patcher.stop)
    test.cold_root, test.cold_records, test.cold_api = cold, records, central
