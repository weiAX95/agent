"""Offline checks for ranking, structured chunking, evidence validation and archives."""
import os
import tempfile
import unittest
import uuid
import io
from pathlib import Path
from unittest.mock import patch

from src.agent.knowledge_ranking import fuse_candidates, query_tokens
from src.agent.knowledge_archive import archive_bytes, read_archive
from src.agent.knowledge_citations import validate_knowledge_answer
from src.agent.knowledge_ingest import prepare_document


class KnowledgePipelineTests(unittest.TestCase):
    def hit(self, index, text, doc='one'):
        return dict(document_id=doc, chunk_id=str(index), text=text, score=.8, title='test', page=1)

    def test_tokens_preserve_codes_and_chinese_pairs(self):
        self.assertIn('e-q17', query_tokens('E-Q17 如何处理？'))
        self.assertIn('冷却', query_tokens('冷却液不足'))

    def test_fusion_removes_duplicate_text_and_preserves_document_diversity(self):
        vector = [self.hit(i, f'片段{i}') for i in range(6)]
        lexical = [self.hit('target', 'E-Q17 冷却液不足', 'two'), self.hit('duplicate', '片段0')]
        hits = fuse_candidates('E-Q17', vector, lexical, limit=5, per_document=3)
        self.assertEqual(hits[0]['document_id'], 'two')
        self.assertEqual(len([h for h in hits if h['document_id'] == 'one']), 3)
        self.assertEqual(len({h['text'] for h in hits}), len(hits))

    def test_structure_preserves_heading_and_markdown_table(self):
        text = '# 设备\n\n## 限制\n\n| 项目 | 条件 |\n| --- | --- |\n| 运输 | 需要隔热箱 |\n'
        with patch.dict(os.environ, {'KNOWLEDGE_CHUNKING_VERSION':'2'}):
            doc = prepare_document('test.md', text.encode())
        self.assertTrue(doc.chunks[0].section_path)
        self.assertIn('需要隔热箱', doc.chunks[0].text)
        self.assertEqual(doc.raw, text.encode())

    def test_citation_checks_do_not_claim_semantic_validation(self):
        result = validate_knowledge_answer('需要签字。[K1]', {'K1'})
        self.assertTrue(result['valid'])
        self.assertEqual(result['support_status'], 'not_checked')
        self.assertFalse(validate_knowledge_answer('可以直接启动。[K9]', {'K1'})['valid'])
        self.assertFalse(validate_knowledge_answer('可以直接启动。', {'K1'})['valid'])
        self.assertFalse(validate_knowledge_answer('需要签字。[K1]', set())['valid'])

    def test_semantic_verification_fails_closed(self):
        verifier = lambda answer, evidence: {'supported': False, 'reason': '原文要求停机'}
        result = validate_knowledge_answer('可以启动。[K1]', {'K1'}, evidence=[{'text':'必须停机'}], verifier=verifier)
        self.assertFalse(result['valid'])
        self.assertEqual(result['support_status'], 'unsupported')
        def failed(*args): raise RuntimeError('timeout')
        self.assertFalse(validate_knowledge_answer('可以启动。[K1]', {'K1'}, verifier=failed)['valid'])

    def test_archive_is_content_checked_and_uuid_scoped(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'KNOWLEDGE_ARCHIVE_DIR':tmp}):
            revision = uuid.uuid4()
            sha = archive_bytes(revision, b'private fixture')
            self.assertEqual(read_archive(revision, sha), b'private fixture')
            with self.assertRaises(ValueError): read_archive(revision, '0'*64)
            with self.assertRaises(ValueError): archive_bytes('../bad', b'x')
            with self.assertRaises(ValueError): archive_bytes(revision, b'changed')

    def test_long_table_repeats_header_without_losing_rows(self):
        text = '# 条款\n\n| 项目 | 条件 |\n| --- | --- |\n' + '\n'.join(
            f'| 项目{i} | 必须满足对应的运输条件{i} |' for i in range(100))
        with patch.dict(os.environ, {'KNOWLEDGE_CHUNKING_VERSION':'2'}):
            doc = prepare_document('table.md',text.encode())
        self.assertGreater(len(doc.chunks),1)
        self.assertTrue(all('| 项目 | 条件 |' in c.text for c in doc.chunks))
        self.assertIn('运输条件99', '\n'.join(c.text for c in doc.chunks))

    def test_scan_ocr_is_opt_in_and_can_use_local_provider(self):
        from pypdf import PdfWriter
        from src.agent.knowledge_ingest import DocumentValidationError
        writer=PdfWriter();writer.add_blank_page(width=100,height=100)
        stream=io.BytesIO();writer.write(stream);raw=stream.getvalue()
        with patch.dict(os.environ, {'KNOWLEDGE_OCR':'0'}):
            with self.assertRaises(DocumentValidationError):prepare_document('scan.pdf',raw)
        with patch.dict(os.environ, {'KNOWLEDGE_OCR':'1'}), patch(
                'src.agent.knowledge_ocr.ocr_page',return_value='合成扫描文字。'):
            self.assertEqual(prepare_document('scan.pdf',raw).chunks[0].text,'合成扫描文字。')

    def test_missing_ocr_binary_fails_before_rendering(self):
        from src.agent.knowledge_ocr import ocr_page
        with patch('shutil.which',return_value=None):
            with self.assertRaisesRegex(ValueError,'Tesseract'):ocr_page(b'',0)

    def test_embedding_fingerprint_tracks_nested_config_changes(self):
        from src.agent.knowledge_versions import embedding_version
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config.json').write_text('{}')
            (root/'1_Pooling').mkdir();(root/'1_Pooling/config.json').write_text('{}')
            first=embedding_version(root)
            (root/'config.json').write_text('{"changed":true}')
            self.assertNotEqual(embedding_version(root),first)
