"""Read-only RAG evaluation. Offline commands never initialize production services."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = ('fact', 'paraphrase', 'exact', 'cross_chunk', 'no_answer', 'conflict')
DEFAULT_DATASET = ROOT / 'evals/rag/dataset.json'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write('\n')


def normalize(value):
    return ''.join(value.split())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text_pages(path):
    if path.suffix.lower() == '.pdf':
        from pypdf import PdfReader
        return [page.extract_text() or '' for page in PdfReader(path).pages]
    require(path.suffix.lower() in {'.txt', '.md', '.markdown'}, 'Unsupported evaluation fixture')
    return [path.read_text(encoding='utf-8-sig')]


def load_dataset(path):
    path = Path(path).resolve()
    data = read_json(path)
    require(isinstance(data, dict), 'Dataset must be an object')
    require(data.get('schema_version') == 1, 'Unsupported dataset schema_version')
    require(isinstance(data.get('documents'), list) and data['documents'], 'Missing documents')
    require(isinstance(data.get('cases'), list) and data['cases'], 'Missing cases')
    pages, hashes = {}, {}
    for doc in data['documents']:
        require(isinstance(doc, dict) and {'key', 'path', 'sha256'} <= doc.keys(), 'Invalid document record')
        key = doc['key']
        require(isinstance(key, str) and key.strip() and key not in pages, 'Duplicate/empty document key')
        source = (path.parent / doc['path']).resolve()
        require(source.is_relative_to(path.parent), 'Document escapes dataset directory')
        require(sha256_file(source) == doc['sha256'], f'Corpus hash mismatch: {key}')
        pages[key] = text_pages(source)
        hashes[key] = doc['sha256']
    seen = set()
    for case in data['cases']:
        require(isinstance(case, dict) and 'case_id' in case, 'Invalid case record')
        case_id = case['case_id']
        require(isinstance(case_id, str) and case_id.strip() and case_id not in seen, 'Duplicate/empty case_id')
        seen.add(case_id)
        require(case.get('category') in CATEGORIES, f'Invalid category: {case_id}')
        require(isinstance(case.get('query'), str) and case['query'].strip(), f'Missing query: {case_id}')
        groups = case.get('evidence_groups')
        require(isinstance(groups, list), f'Missing evidence groups: {case_id}')
        require(bool(groups) == (case['category'] != 'no_answer'), f'Invalid no-answer evidence: {case_id}')
        if groups:
            require(isinstance(case.get('reference_answer'), str) and case['reference_answer'].strip(), 'Missing reference answer')
        else:
            require(case.get('reference_answer', '') == '', 'No-answer reference must be empty')
        if case['category'] in {'cross_chunk', 'conflict'}:
            require(len(groups) >= 2, f'Multiple evidence groups required: {case_id}')
        for group in groups:
            require(isinstance(group, list) and group, f'Empty evidence group: {case_id}')
            for evidence in group:
                key = evidence['document']
                require(key in pages and isinstance(evidence.get('text'), str) and normalize(evidence['text']), 'Invalid evidence')
                page = evidence.get('page')
                require(page is None or type(page) is int and 1 <= page <= len(pages[key]), 'Invalid evidence page')
                content = pages[key][page - 1] if page else '\n'.join(pages[key])
                require(normalize(evidence['text']) in normalize(content), f'Evidence not found: {case_id}')
    return {**data, 'dataset_sha256': sha256_file(path), 'corpus_sha256': digest(hashes)}


def validate_bindings(dataset, bindings):
    require(isinstance(bindings.get('environment'), str) and bindings['environment'].strip(), 'Missing environment label')
    documents = bindings.get('documents')
    expected = {d['key']: d['sha256'] for d in dataset['documents']}
    require(isinstance(documents, dict) and set(documents) == set(expected), 'Bindings must exactly match documents')
    ids = set()
    for key, value in documents.items():
        for field in ('document_id', 'revision'):
            require(str(uuid.UUID(value[field])) == value[field], f'Invalid canonical UUID: {key}/{field}')
        require(value['document_id'] not in ids, 'Duplicate bound document ID')
        ids.add(value['document_id'])
        require(value.get('sha256') == expected[key], f'Binding hash mismatch: {key}')


def verify_catalog(bindings, rows):
    """An isolated catalog must contain exactly the bound live documents."""
    expected = {v['document_id']: v for v in bindings['documents'].values()}
    require(len(rows) == len(expected), 'Catalog is not isolated or document is missing')
    require({r['document_id'] for r in rows} == set(expected), 'Catalog IDs mismatch')
    for row in rows:
        value = expected[row['document_id']]
        require(not row.get('deleted_at'), 'Bound document deleted')
        require(row['revision'] == value['revision'], 'Active revision mismatch')
        require(row['sha256'] == value['sha256'], 'Active document hash mismatch')


def code_version():
    def git(*args):
        return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True, stderr=subprocess.DEVNULL).strip()
    try:
        return {'commit': git('rev-parse', 'HEAD'), 'dirty': bool(git('status', '--porcelain'))}
    except (OSError, subprocess.CalledProcessError):
        return {'commit': None, 'dirty': None}


def run_dataset(dataset, bindings, *, search, catalog_loader, runtime):
    validate_bindings(dataset, bindings)
    verify_catalog(bindings, catalog_loader())
    observations = []
    for case in dataset['cases']:
        started = time.perf_counter()
        try:
            hits = search(case['query'], limit=5)
            require(isinstance(hits, list), 'Search must return a list')
            observation = {'status': 'ok', 'hits': hits[:5]}
        except Exception as exc:
            observation = {'status': 'error', 'hits': [], 'error_type': type(exc).__name__, 'error': str(exc)}
        observations.append({'case_id': case['case_id'], **observation,
                             'latency_ms': round((time.perf_counter() - started) * 1000, 3)})
    # A changed corpus invalidates the run, rather than silently mixing revisions.
    verify_catalog(bindings, catalog_loader())
    return {'schema_version': 1, 'run_id': str(uuid.uuid4()),
            'created_at': datetime.now(timezone.utc).isoformat(), 'code': code_version(),
            'dataset_sha256': dataset['dataset_sha256'], 'corpus_sha256': dataset['corpus_sha256'],
            'dataset': dataset, 'bindings': bindings, 'runtime': runtime, 'cases': observations}


def case_scores(case, observation, bindings):
    if observation['status'] == 'error':
        return {'hit': None, 'coverage': None, 'false_recall': None}
    hits = observation['hits'][:5]
    groups = case['evidence_groups']
    covered = sum(any(
        hit.get('document_id') == bindings['documents'][evidence['document']]['document_id']
        and (evidence.get('page') is None or hit.get('page') == evidence['page'])
        and normalize(evidence['text']) in normalize(hit.get('text', ''))
        for evidence in group for hit in hits) for group in groups)
    return {'hit': bool(covered) if groups else None,
            'coverage': covered / len(groups) if groups else None,
            'false_recall': bool(hits) if not groups else None}


def percentile(values, quantile):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * quantile
    low = math.floor(position)
    high = math.ceil(position)
    return round(values[low] + (values[high] - values[low]) * (position - low), 3)


def mean(values):
    return sum(values) / len(values) if values else None


def validate_run(run):
    require(run.get('schema_version') == 1, 'Invalid run schema')
    validate_bindings(run['dataset'], run['bindings'])
    expected = {c['case_id'] for c in run['dataset']['cases']}
    require(expected, 'Run has no cases')
    require(run['dataset_sha256'] == run['dataset']['dataset_sha256'] and
            run['corpus_sha256'] == run['dataset']['corpus_sha256'], 'Run dataset hashes disagree')
    require(len(run['cases']) == len(expected) and {c['case_id'] for c in run['cases']} == expected, 'Run case IDs mismatch')
    for row in run['cases']:
        require(row.get('status') in {'ok', 'error'}, 'Invalid observation status')
        require(isinstance(row.get('hits'), list) and len(row['hits']) <= 5, 'Invalid observation hits')
        require(isinstance(row.get('latency_ms'), (float, int)) and math.isfinite(row['latency_ms']) and row['latency_ms'] >= 0, 'Invalid latency')
        require(row['status'] != 'error' or not row['hits'], 'Failed query cannot have hits')


def build_report(run, reviews=None):
    validate_run(run)
    cases = {c['case_id']: c for c in run['dataset']['cases']}
    rows = [{**observation, **case_scores(cases[observation['case_id']], observation, run['bindings'])}
            for observation in run['cases']]
    checked, seen = [], set()
    for review in reviews or []:
        case_id = review['case_id']
        require(case_id in cases and case_id not in seen, 'Duplicate or unknown review case_id')
        seen.add(case_id)
        require(isinstance(review.get('answer'), str), 'Review answer must be text')
        require(isinstance(review.get('notes', ''), str), 'Review notes must be text')
        for field in ('citations_valid', 'facts_supported', 'conflict_explained'):
            require(review.get(field) is None or type(review[field]) is bool, 'Review judgment must be boolean or null')
        judged = any(review.get(f) is not None for f in ('citations_valid', 'facts_supported', 'conflict_explained'))
        require(not judged or review['answer'].strip(), 'Judgment requires actual answer')
        if review['answer'].strip():
            checked.append(review)
    successes = [r for r in rows if r['status'] == 'ok']
    answerable = [r for r in successes if r['hit'] is not None]
    negatives = [r for r in successes if r['false_recall'] is not None]
    latencies = [r['latency_ms'] for r in rows]
    return {'schema_version': 1, 'run_id': run['run_id'], 'run_sha256': digest(run),
        'metrics': {'case_count': len(rows), 'answerable_success_count': len(answerable),
            'no_answer_success_count': len(negatives), 'error_count': len(rows) - len(successes),
            'hit_at_5': mean([r['hit'] for r in answerable]),
            'evidence_coverage': mean([r['coverage'] for r in answerable]),
            'no_answer_false_recall_rate': mean([r['false_recall'] for r in negatives]),
            'failure_rate': (len(rows) - len(successes)) / len(rows),
            'latency_count': len(latencies), 'p50_ms': percentile(latencies, .5), 'p95_ms': percentile(latencies, .95)},
        'manual': {'status': '未评审' if not checked else '已录入答案；各项仅统计明确判断',
            'reviewed_count': len(checked), **{
                f'{field}_rate': mean([r[field] for r in checked if r.get(field) is not None])
                for field in ('citations_valid', 'facts_supported', 'conflict_explained')},
            'reviews': checked}, 'cases': rows}


def compare_runs(baseline, current):
    for key in ('dataset_sha256', 'corpus_sha256'):
        require(baseline[key] == current[key], f'Cannot compare different {key}')
    old_report, new_report = build_report(baseline), build_report(current)
    old = {r['case_id']: r for r in old_report['cases']}
    require(set(old) == {r['case_id'] for r in new_report['cases']}, 'Cannot compare different case sets')
    rows = []
    for new in new_report['cases']:
        previous = old[new['case_id']]
        if previous['status'] == 'error' or new['status'] == 'error':
            change = 'still_error' if previous['status'] == new['status'] else ('failed' if new['status'] == 'error' else 'recovered')
        else:
            before = previous['coverage'] if previous['coverage'] is not None else 1 - previous['false_recall']
            after = new['coverage'] if new['coverage'] is not None else 1 - new['false_recall']
            change = 'improved' if after > before else 'regressed' if after < before else 'unchanged'
        rows.append({'case_id': new['case_id'], 'change': change,
                     'baseline': {k: previous[k] for k in ('status', 'hit', 'coverage', 'false_recall', 'latency_ms')},
                     'current': {k: new[k] for k in ('status', 'hit', 'coverage', 'false_recall', 'latency_ms')},
                     'latency_delta_ms': round(new['latency_ms'] - previous['latency_ms'], 3)})
    return {'baseline_run': baseline['run_id'], 'current_run': current['run_id'],
            'dataset_sha256': baseline['dataset_sha256'], 'corpus_sha256': baseline['corpus_sha256'],
            'runtime': {'baseline': baseline['runtime'], 'current': current['runtime']},
            'code': {'baseline': baseline['code'], 'current': current['code']},
            'metrics': {'baseline': old_report['metrics'], 'current': new_report['metrics']}, 'cases': rows}


def live_backend():
    """Lazy imports: no API app, chat model, uploads, or explicit collection writes."""
    import ast
    import inspect
    import os
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    import psycopg
    from pymilvus import MilvusClient
    from api.config import settings
    from src.agent.checkpoint import checkpoint_dsn
    from src.agent import knowledge_store as store
    from src.agent.knowledge_ingest import prepare_document
    from src.agent.memory import client as resources
    model_path = Path(os.path.expanduser('~/models/bge-base-zh-v1.5'))
    require(model_path.is_dir(), 'Existing local BGE model is required; no downloads performed')
    # Production get_client can create agent_memory. Fail first if it is absent.
    client = MilvusClient(uri=os.getenv('MILVUS_URI', 'http://localhost:19530'), timeout=10)
    try:
        for name in (resources.MEMORY_COLLECTION, store.KNOWLEDGE_COLLECTION):
            require(client.has_collection(name, timeout=10), f'Required existing collection missing: {name}')
    finally:
        client.close()
    def catalog():
        with psycopg.connect(checkpoint_dsn(settings.database_url), connect_timeout=5,
                             options='-c default_transaction_read_only=on') as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT id::text, revision::text, sha256 FROM knowledge_documents WHERE deleted_at IS NULL')
                return [dict(document_id=i, revision=r, sha256=h) for i, r, h in cur.fetchall()]
    tree = ast.parse(inspect.getsource(prepare_document))
    splitter = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'RecursiveCharacterTextSplitter')
    chunking = {k.arg: ast.literal_eval(k.value) for k in splitter.keywords}
    runtime = {'embedding': {'model': str(model_path), 'config_sha256': {
        p.name: sha256_file(p) for p in model_path.glob('*config*.json')}},
        'chunking': chunking, 'retrieval': {'collection': store.KNOWLEDGE_COLLECTION,
        'metric': 'COSINE', 'limit': 5, 'threshold': store._score_threshold(None),
        'consistency': 'Strong'}}
    return store.search_knowledge_chunks, catalog, runtime, resources.close_memory_resources


def review_template(run):
    return {'run_sha256': digest(run), 'cases': [dict(case_id=c['case_id'], answer='', citations_valid=None,
            facts_supported=None, conflict_explained=None, notes='') for c in run['cases']]}


def markdown_report(report):
    if 'manual' in report:
        lines = ['# RAG 评测报告', '', f"运行：{report['run_id']}", '', '## 自动指标', '']
        lines += [f'- {key}: {value if value is not None else "不适用"}' for key, value in report['metrics'].items()]
        lines += ['', '## 人工评审', '', report['manual']['status'], '']
        lines += [f'- {k}: {v if v is not None else "未评审"}' for k, v in report['manual'].items() if k.endswith('_rate')]
        lines += ['', '| 案例 | 状态 | Hit@5 | 覆盖率 | 无答案误召回 | 耗时 ms |', '| --- | --- | --- | --- | --- | --- |']
        lines += [f"| {r['case_id']} | {r['status']} | {r['hit']} | {r['coverage']} | {r['false_recall']} | {r['latency_ms']} |" for r in report['cases']]
    else:
        lines = ['# RAG 版本对比', '', '运行配置和代码差异见同名 JSON。', '', '| 案例 | 变化 | 耗时差 ms |', '| --- | --- | --- |']
        lines += [f"| {r['case_id']} | {r['change']} | {r['latency_delta_ms']} |" for r in report['cases']]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('validate', 'run'):
        command = commands.add_parser(name)
        command.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
        command.add_argument('--bindings', type=Path, required=name == 'run')
        if name == 'run': command.add_argument('--out', type=Path)
    report = commands.add_parser('report')
    report.add_argument('--run', type=Path, required=True)
    report.add_argument('--reviews', type=Path)
    report.add_argument('--out', type=Path)
    compare = commands.add_parser('compare')
    compare.add_argument('--baseline', type=Path, required=True)
    compare.add_argument('--current', type=Path, required=True)
    compare.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command in ('validate', 'run'):
            dataset = load_dataset(args.dataset)
            bindings = read_json(args.bindings) if args.bindings else None
            if bindings is not None: validate_bindings(dataset, bindings)
            if args.command == 'validate':
                print(json.dumps({'valid': True, 'documents': len(dataset['documents']), 'cases': len(dataset['cases']),
                                  'dataset_sha256': dataset['dataset_sha256'], 'corpus_sha256': dataset['corpus_sha256']}))
                return 0
        output = args.out or ROOT / 'output/rag' / f'{args.command}-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")}.json'
        require(not output.exists(), f'Output already exists: {output}')
        if args.command == 'run':
            require(not output.with_suffix('.reviews.json').exists(), 'Review template already exists')
            search, catalog, runtime, close = live_backend()
            try:
                result = run_dataset(dataset, bindings, search=search, catalog_loader=catalog, runtime=runtime)
            finally:
                close()
            write_json(output, result)
            write_json(output.with_suffix('.reviews.json'), review_template(result))
            print(f'Run: {output}; errors: {sum(r["status"] == "error" for r in result["cases"])}')
            return 1 if any(r['status'] == 'error' for r in result['cases']) else 0
        if args.command == 'report':
            run = read_json(args.run)
            reviews = read_json(args.reviews) if args.reviews else None
            if reviews is not None:
                require(reviews['run_sha256'] == digest(run), 'Reviews belong to a different run')
            result = build_report(run, reviews['cases'] if reviews else None)
        else:
            result = compare_runs(read_json(args.baseline), read_json(args.current))
        markdown = output.with_suffix('.md')
        require(not markdown.exists(), f'Output already exists: {markdown}')
        write_json(output, result)
        with markdown.open('x', encoding='utf-8') as handle:
            handle.write(markdown_report(result))
        print(f'Report: {output}; Markdown: {markdown}')
        return 0
    except Exception as exc:
        # CLI boundary: setup/database failures must not leave a successful exit code.
        print(f'RAG evaluation failed ({type(exc).__name__}): {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
