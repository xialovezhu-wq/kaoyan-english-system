import unittest,tempfile
from pathlib import Path
from english_pipeline.constants import MASTER_HEADER, MASTERED_HEADER

class DirectConversationPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        from tests.english_pipeline.cold_fixture import install_cold_fixture
        install_cold_fixture(self, self.root)
        self.repo = self.root / "repo"
        self.state = self.repo / "intake"
        bank = self.repo / "bank"
        bank.mkdir(parents=True)
        (bank / "master_bank.csv").write_text(
            ",".join(MASTER_HEADER) + "\n", encoding="utf-8"
        )
        (bank / "mastered_items.csv").write_text(
            ",".join(MASTERED_HEADER) + "\n", encoding="utf-8"
        )
        (bank / "sentence_patterns.md").write_text(
            "# Sentence Patterns\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()
