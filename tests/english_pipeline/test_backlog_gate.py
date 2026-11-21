import unittest
from english_pipeline.archive import VolumeContract
from english_pipeline.util import atomic_write_json,file_sha256

class BacklogGateTests(unittest.TestCase):
    def _volume_contract(self) -> VolumeContract:
        archive_root = self.root / "T9-Data"
        sentinel = archive_root / "00_迁移管理" / "状态" / "volume-sentinel.json"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel_payload = {
            "volume_name": "TEST-T9",
            "volume_uuid": "TEST-UUID",
            "expected_mount_point": str(archive_root),
            "stop_if_mount_suffix_exists": str(self.root / "T9-Data 1"),
        }
        atomic_write_json(sentinel, sentinel_payload)
        return VolumeContract(
            archive_root=archive_root,
            sentinel_sha256=file_sha256(sentinel),
            volume_name="TEST-T9",
            volume_uuid="TEST-UUID",
            rejected_mount_path=self.root / "T9-Data 1",
        )
