"""Run in the application image with PYTHONPATH=/app and unittest discovery."""
import asyncio
import gzip
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import labelary
import server


class Limits(unittest.TestCase):
    @patch.object(server, 'MAX_DOCUMENT_BYTES', 128)
    def test_gzip_boundary(self):
        req = Mock()
        req.get.return_value = 'gzip'
        req.data = gzip.compress(bytes(128))
        self.assertEqual(server.read_document(req), bytes(128))
        req.data = gzip.compress(bytes(129))
        with self.assertRaises(server.IPPError):
            server.read_document(req)

    def test_stateful_zpl_not_silently_split(self):
        label = b'^XA^FDtest^FS^XZ'
        simple = label * 51
        self.assertEqual(labelary._batches(simple), [label * 50, label])
        setup = b'~DGR:TEST.GRF,1,1,FF'
        self.assertEqual(labelary._batches(label + setup + label), [label + setup + label])
        with self.assertRaises(labelary.LabelaryError):
            labelary._batches(label + setup + simple)


class Jobs(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(server, 'JOBS_DIR', Path(tmp)))
        self.p = server.Printer()
        self.client = await self.enterAsyncContext(TestClient(TestServer(server.build_app(self.p))))

    async def test_delete_during_failed_render(self):
        started, release, finished = asyncio.Event(), threading.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()

        def render(*args):
            loop.call_soon_threadsafe(started.set)
            release.wait(3)
            raise FileNotFoundError('document deleted')

        self.p.render = render
        job = self.p.create_job('test', 'test', {})
        self.p.add_document(job, b'hello', 'text/plain')
        self.p.close_job(job)
        worker = asyncio.create_task(self.p.run())
        try:
            await asyncio.wait_for(started.wait(), 2)
            self.p.delete(job)
            self.p.publish_printer = finished.set
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            self.assertFalse(job.dir.exists())
            self.assertNotIn(job.id, self.p.jobs)
        finally:
            release.set()
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

    async def test_completed_print(self):
        job = self.p.create_job('one page', 'test', {})
        self.p.add_document(job, b'Page one', 'text/plain')
        self.p.close_job(job)
        self.p.seconds_per_page = 0
        await self.p.process(job, self.p.cancel_events[job.id])
        self.assertEqual((job.state, job.pages, len(job.page_sizes)), (server.COMPLETED, 1, 1))
        self.assertTrue((job.dir / 'output.pdf').read_bytes().startswith(b'%PDF'))

    async def test_upload_limit(self):
        with patch.object(server, 'MAX_DOCUMENT_BYTES', 128):
            for size, status in [(129, 413), (128, 200)]:
                form = FormData()
                form.add_field('file', b'a' * size, filename='test.txt')
                response = await self.client.post('/api/upload', data=form)
                self.assertEqual(response.status, status)
        self.assertEqual(len(self.p.jobs), 1)

    async def test_preview_cache(self):
        job = self.p.create_job('preview', 'test', {})
        job.pages, job.page_sizes = 1, [[72, 72]]
        (job.dir / 'output.pdf').touch()
        url = f'/jobs/{job.id}/pages/1.png?w='
        with patch.object(server.convert, 'render_page_png', return_value=b'png') as render:
            results = await asyncio.gather(*(self.client.get(url + '96') for _ in range(4)))
            self.assertTrue(all(r.status == 200 for r in results))
            self.assertEqual(render.call_count, 1)
            for width in ['100', '110', '120']:
                self.assertEqual((await self.client.get(url + width)).status, 200)
        self.assertEqual(len(list(job.dir.glob('page-1-*.png'))), 3)
