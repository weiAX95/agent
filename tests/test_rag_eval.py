"""RAG scoring and read-only orchestration, without databases or model calls."""
import copy
import contextlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from tools import rag_eval as rag

ROOT = Path(__file__).resolve().parents[1]


class RagEvalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'a.md').write_text('条件一。\n条件二。', encoding='utf-8')
        self.data = {'schema_version': 1, 'documents': [
            {'key': 'a', 'path': 'a.md', 'sha256': rag.sha256_file(self.root / 'a.md')}],
            'cases': [self.case('one', [[{'document': 'a', 'text': '条件一。'}]])]}
        self.binding = {'environment': 'isolated-test', 'documents': {'a': {
            'document_id': str(uuid.uuid4()), 'revision': str(uuid.uuid4()),
            'sha256': self.data['documents'][0]['sha256']}}}
        self.catalog = [dict(self.binding['documents']['a'], deleted_at=None)]

    def case(self, name, groups, category='fact'):
        return {'case_id': name, 'category': category, 'query': '测试问题',
                'evidence_groups': groups, 'reference_answer': '条件一' if groups else ''}

    def load(self, data=None):
        path = self.root / 'dataset.json'
        path.write_text(json.dumps(data or self.data), encoding='utf-8')
        return rag.load_dataset(path)

    def hit(self, text='条件一。', document_id=None):
        return {'document_id': document_id or self.binding['documents']['a']['document_id'],
                'chunk_id': str(uuid.uuid4()), 'text': text, 'score': .8, 'page': None, 'title': 'a'}

    def run_fake(self, dataset=None, search=None, loader=None):
        return rag.run_dataset(dataset or self.load(), self.binding,
            search=search or (lambda query, **kw: [self.hit()]),
            catalog_loader=loader or (lambda: copy.deepcopy(self.catalog)),
            runtime={'embedding': 'test', 'chunking': {}, 'retrieval': {'limit': 5}})

    def test_checked_in_corpus_has_four_cases_per_category(self):
        dataset = rag.load_dataset(ROOT / 'evals/rag/dataset.json')
        self.assertEqual(len(dataset['cases']), 24)
        for category in rag.CATEGORIES:
            self.assertEqual(sum(c['category'] == category for c in dataset['cases']), 4)

    def test_duplicate_id_invalid_category_missing_evidence_and_changed_hash(self):
        variants = []
        d = copy.deepcopy(self.data); d['cases'] *= 2; variants.append(d)
        d = copy.deepcopy(self.data); d['cases'][0]['category'] = 'invalid'; variants.append(d)
        d = copy.deepcopy(self.data); d['cases'][0]['evidence_groups'][0][0]['text'] = '不存在'; variants.append(d)
        d = copy.deepcopy(self.data); d['documents'][0]['sha256'] = '0' * 64; variants.append(d)
        d = copy.deepcopy(self.data); d['cases'][0]['category'] = 'no_answer'; variants.append(d)
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.load(data)

    def test_binding_requires_exact_documents_unique_uuid_and_matching_hash(self):
        dataset = self.load()
        for change in ('missing', 'uuid', 'hash', 'extra'):
            b = copy.deepcopy(self.binding)
            if change == 'missing': b['documents'] = {}
            if change == 'uuid': b['documents']['a']['document_id'] = 'bad'
            if change == 'hash': b['documents']['a']['sha256'] = '0' * 64
            if change == 'extra': b['documents']['b'] = b['documents']['a']
            with self.subTest(change=change), self.assertRaises(ValueError):
                rag.validate_bindings(dataset, b)

    def test_catalog_mismatch_or_drift_stops_run(self):
        for field in ('revision', 'sha256', 'deleted_at'):
            bad = copy.deepcopy(self.catalog)
            bad[0][field] = 'changed'
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.run_fake(loader=lambda: bad)
        calls = iter([self.catalog, []])
        with self.assertRaises(ValueError):
            self.run_fake(loader=lambda: next(calls))
        with self.assertRaises(ValueError):
            self.run_fake(loader=lambda: self.catalog + [dict(self.catalog[0], document_id=str(uuid.uuid4()))])

    def test_whitespace_matching_partial_coverage_and_document_identity(self):
        d = copy.deepcopy(self.data)
        d['cases'][0]['category'] = 'cross_chunk'
        d['cases'][0]['evidence_groups'].append([{'document': 'a', 'text': '条件二。'}])
        run = self.run_fake(self.load(d), search=lambda *a, **k: [self.hit('条 件 一 。')])
        metrics = rag.build_report(run)['metrics']
        self.assertEqual(metrics['hit_at_5'], 1)
        self.assertEqual(metrics['evidence_coverage'], .5)
        run = self.run_fake(search=lambda *a, **k: [self.hit(document_id=str(uuid.uuid4()))])
        self.assertEqual(rag.build_report(run)['metrics']['hit_at_5'], 0)

    def test_no_answer_false_recall_empty_and_error_have_separate_denominators(self):
        d = copy.deepcopy(self.data)
        d['cases'] += [self.case('empty', [], 'no_answer'), self.case('false', [], 'no_answer'),
                       self.case('error', [], 'no_answer')]
        outputs = iter([[self.hit()], [], [self.hit()], RuntimeError('unavailable')])
        def search(*args, **kwargs):
            result = next(outputs)
            if isinstance(result, Exception): raise result
            return result
        run = self.run_fake(self.load(d), search=search)
        metrics = rag.build_report(run)['metrics']
        self.assertEqual(metrics['failure_rate'], .25)
        self.assertEqual(metrics['no_answer_false_recall_rate'], .5)
        self.assertEqual(metrics['no_answer_success_count'], 2)
        self.assertEqual(run['cases'][-1]['status'], 'error')

    def test_reviews_missing_is_unreviewed_and_invalid_reviews_rejected(self):
        run = self.run_fake()
        report = rag.build_report(run)
        self.assertEqual(report['manual']['reviewed_count'], 0)
        self.assertIsNone(report['manual']['facts_supported_rate'])
        review = {'case_id': 'one', 'answer': '回答', 'citations_valid': True,
                  'facts_supported': False, 'conflict_explained': None, 'notes': ''}
        report = rag.build_report(run, [review])
        self.assertEqual(report['manual']['facts_supported_rate'], 0)
        for reviews in ([review, review], [dict(review, case_id='unknown')],
                        [dict(review, citations_valid='yes')]):
            with self.assertRaises(ValueError): rag.build_report(run, reviews)

    def test_compare_rejects_changed_inputs_and_tracks_regression(self):
        old = self.run_fake()
        current = copy.deepcopy(old)
        current['cases'][0]['hits'] = []
        current['runtime']['retrieval']['threshold'] = .6
        comparison = rag.compare_runs(old, current)
        self.assertEqual(comparison['cases'][0]['change'], 'regressed')
        self.assertNotEqual(comparison['runtime']['baseline'], comparison['runtime']['current'])
        for key in ('dataset_sha256', 'corpus_sha256'):
            changed = copy.deepcopy(current); changed[key] = 'different'
            with self.assertRaises(ValueError): rag.compare_runs(old, changed)

    def test_run_uses_top_five_and_records_failure_without_hiding_it(self):
        def search(query, *, limit):
            self.assertEqual(limit, 5)
            return [self.hit()] * 7
        run = self.run_fake(search=search)
        self.assertEqual(len(run['cases'][0]['hits']), 5)
        self.assertGreaterEqual(run['cases'][0]['latency_ms'], 0)

    def test_no_answer_empty_result_and_full_conflict_coverage(self):
        d = copy.deepcopy(self.data)
        d['cases'] = [self.case('none', [], 'no_answer'), self.case('both', [
            [{'document': 'a', 'text': '条件一。'}],
            [{'document': 'a', 'text': '条件二。'}]], 'conflict')]
        outputs = iter([[], [self.hit('条件一。条件二。')]])
        report = rag.build_report(self.run_fake(self.load(d), search=lambda *a, **k: next(outputs)))
        self.assertEqual(report['metrics']['no_answer_false_recall_rate'], 0)
        self.assertEqual(report['metrics']['evidence_coverage'], 1)

    def test_cli_report_compare_review_binding_and_no_overwrite(self):
        run = self.run_fake()
        source = self.root / 'run.json'
        rag.write_json(source, run)
        reviews = self.root / 'reviews.json'
        rag.write_json(reviews, rag.review_template(run))
        output = self.root / 'report.json'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rag.main(['report', '--run', str(source), '--reviews', str(reviews), '--out', str(output)]), 0)
            self.assertEqual(rag.main(['report', '--run', str(source), '--out', str(output)]), 2)
            self.assertEqual(rag.main(['compare', '--baseline', str(source), '--current', str(source),
                                       '--out', str(self.root / 'comparison.json')]), 0)
            wrong = rag.review_template(run); wrong['run_sha256'] = 'wrong'
            wrong_path = self.root / 'wrong.json'; rag.write_json(wrong_path, wrong)
            self.assertEqual(rag.main(['report', '--run', str(source), '--reviews', str(wrong_path),
                                      '--out', str(self.root / 'wrong-report.json')]), 2)
        self.assertIn('未评审', output.with_suffix('.md').read_text())
        self.assertFalse((self.root / 'wrong-report.json').exists())

    def test_cli_run_persists_errors_closes_resources_and_does_not_overwrite(self):
        self.load()
        bindings_path = self.root / 'bindings.json'
        rag.write_json(bindings_path, self.binding)
        output = self.root / 'result.json'
        def fail(*args, **kwargs): raise RuntimeError('search unavailable')
        close = Mock()
        with patch.object(rag, 'live_backend', return_value=(fail, lambda: self.catalog, {}, close)) as backend:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                arguments = ['run', '--dataset', str(self.root / 'dataset.json'),
                             '--bindings', str(bindings_path), '--out', str(output)]
                self.assertEqual(rag.main(arguments), 1)
                backend.assert_called_once()
                self.assertEqual(rag.main(arguments), 2)
                backend.assert_called_once()
        close.assert_called_once()
        self.assertEqual(rag.read_json(output)['cases'][0]['status'], 'error')
        self.assertTrue(output.with_suffix('.reviews.json').exists())

    def test_cli_validate_stays_offline(self):
        self.load()
        with patch.object(rag, 'live_backend', side_effect=AssertionError('must stay offline')):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(rag.main(['validate', '--dataset', str(self.root / 'dataset.json')]), 0)

    def test_expanded_page_range_matches_only_pages_inside_the_evidence_span(self):
        case=self.case('page',[[{'document':'a','text':'第二页事实','page':2}]])
        hit=dict(self.hit('第二页事实'),page=1,page_end=2)
        observation={'status':'ok','hits':[hit]}
        self.assertTrue(rag.case_scores(case,observation,self.binding)['hit'])
        hit['page_end']=1
        self.assertFalse(rag.case_scores(case,observation,self.binding)['hit'])
