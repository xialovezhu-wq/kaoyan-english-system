"""Check callable routes and authorization boundaries instead of instruction wording."""
import sys
import unittest
from pathlib import Path
REPO=Path('/Users/your-user/Documents/kaoyan-english')
if str(REPO) not in sys.path:
    sys.path.insert(0,str(REPO))
from english_pipeline.cli import build_parser

class IntensiveReadingContractTests(unittest.TestCase):
    def test_exact_hash_lookup_and_separate_unit_export_routes(self):
        parser=build_parser()
        query=parser.parse_args(['query-personal-summary','--key','word:advertiser','--key','error_type:lookalike'])
        self.assertEqual(query.key,['word:advertiser','error_type:lookalike'])
        self.assertFalse(query.question_review)
        exported=parser.parse_args(['export-unit-dialogue','--rollout','exact.jsonl','--turn-id','first','--turn-id','last','--output','unit.json'])
        self.assertEqual(exported.turn_id,['first','last'])
        self.assertNotEqual(exported.command,'capture')

    def test_formal_publication_requires_explicit_basis(self):
        parser=build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(['publish-personal-summaries','--input-json','points.json'])
        publication=parser.parse_args(['publish-personal-summaries','--input-json','points.json','--writer-receipt','receipt.json'])
        self.assertEqual(publication.writer_receipt,Path('receipt.json'))
        capture=parser.parse_args(['capture','--source-kind','article','--answer-exposure','answer_free','--review-route','web'])
        self.assertEqual(capture.review_route,'web')
        self.assertFalse(hasattr(capture,'bootstrap_existing_formal'))

if __name__=='__main__':
    unittest.main()
