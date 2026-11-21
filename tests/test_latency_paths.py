import asyncio
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import tests  # Establish synthetic-only storage before application imports.
import main
from services.file_agent import runtime
from services.knowledge import embedder


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_ranges_revalidation_updates_and_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'sample.pdf'
            source.write_bytes(b'%PDF-test-original')
            record = dict(id='perf-file', name='sample.pdf', original_name='sample.pdf',
                          storage_path=str(source), audience='personal', owner='0000')
            session = dict(account='0000', created_at=datetime.now().isoformat())
            store = SimpleNamespace(_deleted_files={}, get_session=AsyncMock(return_value=session),
                                    find_file_by_id=AsyncMock(return_value=record))
            with patch.object(main, 'data_store', store), patch.object(main, 'resolve_storage_path', return_value=str(source)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
                    headers = {'X-Session-ID': 'fixture'}
                    for url in ['/api/files/perf-file/preview', '/api/documents/perf-file/file']:
                        first = await client.get(url, headers=headers)
                        self.assertEqual(first.status_code, 200)
                        self.assertEqual(first.content, source.read_bytes())
                        self.assertEqual(first.headers['cache-control'], 'private, no-cache')
                        self.assertEqual(first.headers['vary'], 'X-Session-ID')
                        ranged = await client.get(url, headers={**headers, 'Range': 'bytes=0-3'})
                        self.assertEqual(ranged.status_code, 206)
                        self.assertEqual(ranged.content, b'%PDF')
                        self.assertNotIn('content-encoding', ranged.headers)
                        cached = await client.get(url, headers={**headers, 'If-None-Match': first.headers['etag']})
                        self.assertEqual(cached.status_code, 304)
                        self.assertEqual(cached.content, b'')
                        source.write_bytes(source.read_bytes() + b'-changed')
                        updated = await client.get(url, headers={**headers, 'If-None-Match': first.headers['etag']})
                        self.assertEqual(updated.status_code, 200)
                        self.assertNotEqual(updated.headers['etag'], first.headers['etag'])
                        record['owner'] = 'someone-else'
                        denied = await client.get(url, headers={**headers, 'If-None-Match': updated.headers['etag']})
                        self.assertEqual(denied.status_code, 403)
                        record['owner'] = '0000'
                        self.assertEqual((await client.get(url)).status_code, 401)

    async def test_ui_compression_and_static_revalidation(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            page = await client.get('/')
            self.assertEqual(page.headers['content-encoding'], 'gzip')
            self.assertIn('warmCaptcha()', page.text)
            asset = await client.get('/static/pdf.worker.min.js')
            self.assertEqual(asset.headers['content-encoding'], 'gzip')
            cached = await client.get('/static/pdf.worker.min.js', headers={'If-None-Match': asset.headers['etag']})
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.headers['cache-control'], 'public, no-cache')
            self.assertEqual((await client.get('/static/main.py')).status_code, 404)

    async def test_office_conversions_queue_once_and_cache_hits_do_not_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.docx'
            source.write_bytes(b'fixture')
            active = 0
            peak = 0
            count = 0
            def render(_source, directory):
                nonlocal active, peak, count
                active += 1
                peak = max(peak, active)
                count += 1
                time.sleep(.03)
                directory.mkdir(exist_ok=True)
                target = directory / 'preview.pdf'
                target.write_bytes(b'%PDF-fixture')
                active -= 1
                return target
            with patch.object(runtime, '_render_office_preview', side_effect=render):
                await asyncio.gather(*(asyncio.to_thread(runtime.office_preview, source, root / name)
                                       for name in ['one', 'one', 'two', 'two']))
                self.assertEqual(peak, 1)
                self.assertEqual(count, 2)
                with runtime._preview_converter_slots:
                    await asyncio.wait_for(asyncio.to_thread(runtime.office_preview, source, root / 'one'), .5)


class EmbedderTests(unittest.TestCase):
    def test_concurrent_fulltext_and_vector_initialization_share_local_model(self):
        from concurrent.futures import ThreadPoolExecutor
        with patch.object(embedder, '_LOCAL_EMBEDDER', None), patch.object(embedder, 'LocalEmbedder') as constructor:
            with ThreadPoolExecutor(max_workers=4) as pool:
                models = list(pool.map(lambda _: embedder.get_local_embedder(), range(8)))
            self.assertTrue(all(model is models[0] for model in models))
            self.assertIs(embedder._build('local'), models[0])
            constructor.assert_called_once()
