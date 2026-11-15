import copy
import json
import tempfile
import unittest
from pathlib import Path

from english_pipeline.errors import ValidationError
from english_pipeline.personal_summary import SCHEMA, publish_personal_summaries, query_personal_summary
from english_pipeline.summary_source_review import REVIEW_SCHEMA
from english_pipeline.util import file_sha256, object_sha256


class SummarySourceReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.state = self.repo / 'intake'
        self.source = self.repo / 'intake/packages/raw/conversation.json'
        self.source.parent.mkdir(parents=True)
        self.source.write_text(json.dumps({'messages': [
            {'role': 'user', 'content': '我把 waste 认成了 waiter。'},
            {'role': 'assistant', 'content': 'waste 是浪费。'}]}, ensure_ascii=False))
        self.record = {'key': 'word:waste', 'aliases': [], 'visibility': 'answer_free',
                       'teaching_hint': '按当前误译比较 waste 与 waiter。',
                       'episodes': [{'episode_id': 'reviewed-original-1', 'study_date': '2026-10-03',
                                     'first_attempt': '明确说认成 waiter。', 'correction': '助手解释浪费义。',
                                     'later_observation': '未观察到用户复译。',
                                     'evidence': [{'path': str(self.source.relative_to(self.repo)),
                                                   'sha256': file_sha256(self.source),
                                                   'locator': '/messages/0', 'quote': '认成了 waiter'}]}]}
        self.document = {'schema_version': SCHEMA, 'records': [self.record]}

    def manifest(self, document=None, kind='learner_dialogue'):
        return {'schema_version': REVIEW_SCHEMA, 'purpose': 'explicit_summary_maintenance',
                'summary_input_sha256': object_sha256(document or self.document),
                'sources': [{'path': str(self.source.relative_to(self.repo)), 'sha256': file_sha256(self.source),
                             'kind': kind, 'study_dates': ['2026-10-03'],
                             'locators': ['/messages/0', '/messages/1', '/messages/99']}]}

    def test_explicit_source_review_can_update_teaching_without_formal_intake(self):
        source_before = self.source.read_bytes()
        result = publish_personal_summaries(self.repo, self.state, self.document, source_review=self.manifest())
        self.assertEqual(result['formal_write_count'], 0)
        self.assertEqual(self.source.read_bytes(), source_before)
        self.assertFalse((self.repo / 'bank').exists())
        self.assertEqual(query_personal_summary(self.state, keys=['word:waste'])['matched'][0]['episodes'][0]['study_date'], '2026-10-03')

    def test_normal_capture_and_bootstrap_do_not_gain_raw_publication_authority(self):
        with self.assertRaises(ValidationError):
            publish_personal_summaries(self.repo, self.state, self.document)
        with self.assertRaises(ValidationError):
            publish_personal_summaries(self.repo, self.state, self.document, bootstrap_existing_formal=True)

    def test_review_binds_exact_input_and_original_hash(self):
        manifest = self.manifest()
        wrong = copy.deepcopy(self.document)
        wrong['records'][0]['teaching_hint'] = '另一份未绑定的成稿'
        with self.assertRaises(ValidationError):
            publish_personal_summaries(self.repo, self.state, wrong, source_review=manifest)
        self.source.write_text('{}')
        with self.assertRaises(ValidationError):
            publish_personal_summaries(self.repo, self.state, self.document, source_review=manifest)

    def test_assistant_or_formal_advice_alone_is_not_user_evidence(self):
        for kind, locator in [('formal_advice', '/messages/0'), ('learner_dialogue', '/messages/1')]:
            document = copy.deepcopy(self.document)
            document['records'][0]['episodes'][0]['evidence'][0].update(locator=locator)
            document['records'][0]['episodes'][0]['evidence'][0].pop('quote')
            with self.assertRaises(ValidationError):
                publish_personal_summaries(self.repo, self.state, document,
                    source_review=self.manifest(document, kind))

    def test_bad_locator_quote_and_study_date_fail_closed(self):
        for field, value in [('locator', '/messages/99'), ('quote', '从未出现的复述')]:
            document = copy.deepcopy(self.document)
            document['records'][0]['episodes'][0]['evidence'][0][field] = value
            with self.assertRaises(ValidationError):
                publish_personal_summaries(self.repo, self.state, document, source_review=self.manifest(document))
        document = copy.deepcopy(self.document)
        document['records'][0]['episodes'][0]['study_date'] = '2026-10-11'
        with self.assertRaises(ValidationError):
            publish_personal_summaries(self.repo, self.state, document, source_review=self.manifest(document))

    def test_source_review_cannot_remove_existing_answer_protection(self):
        self.document['records'][0]['visibility'] = 'protected'
        publish_personal_summaries(self.repo, self.state, self.document, source_review=self.manifest())
        self.document['records'][0]['visibility'] = 'answer_free'
        publish_personal_summaries(self.repo, self.state, self.document, source_review=self.manifest())
        self.assertEqual(query_personal_summary(self.state, keys=['word:waste'])['matched'], [])

    def test_grouped_message_locators_preserve_both_roles_without_duplicate_file_refs(self):
        ref = self.document['records'][0]['episodes'][0]['evidence'][0]
        ref.pop('locator')
        ref.pop('quote')
        ref['locators'] = ['/messages/0', '/messages/1']
        publish_personal_summaries(self.repo, self.state, self.document, source_review=self.manifest())
        result = query_personal_summary(self.state, keys=['word:waste'])
        self.assertEqual(result['matched'][0]['episodes'][0]['later_observation'], '未观察到用户复译。')

    def test_raw_review_checks_the_summary_version_before_replacing_the_projection(self):
        self.document['records'][0]['expected_summary_sha256'] = None
        publish_personal_summaries(self.repo, self.state, self.document, source_review=self.manifest())
        before = (self.state / 'personal-summary/current.json').read_bytes()
        with self.assertRaises(ValidationError):
            publish_personal_summaries(self.repo, self.state, self.document, source_review=self.manifest())
        self.assertEqual((self.state / 'personal-summary/current.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
