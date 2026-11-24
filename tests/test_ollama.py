import http.client
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tracker import export, ollama, pricing, ranking, server, sources
from tracker.db import DB
from tracker.engine import Engine


def spawn(handler):
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': .02}, daemon=True).start()
    return httpd


def request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        conn.request(method, path, body, headers or {}, encode_chunked=True)
        with conn.getresponse() as response:
            return response.status, response.getheaders(), response.read()
    finally:
        conn.close()


def unused_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class OllamaRelay(unittest.TestCase):
    def setUp(self):
        self.db = DB(':memory:')
        self.addCleanup(self.db.conn.close)
        self.request_seen = None
        self.response = (200, 'application/json', [b'{}'])
        self.continue_stream = None
        self.recorded = threading.Event()
        upsert = self.db.upsert_records

        def record(rows):
            upsert(rows)
            self.recorded.set()

        self.db.upsert_records = record
        test = self

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def handle_request(self):
                n = int(self.headers.get('Content-Length', 0))
                test.request_seen = self.command, self.path, self.headers, self.rfile.read(n)
                status, content_type, chunks = test.response
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Connection', 'close, X-Drop-Response')
                self.send_header('X-Drop-Response', 'do not forward')
                self.send_header('X-Pass-Response', 'forward')
                streaming = len(chunks) > 1
                if streaming:
                    self.send_header('Transfer-Encoding', 'chunked')
                else:
                    self.send_header('Content-Length', str(len(chunks[0])))
                self.end_headers()
                if self.command == 'HEAD':
                    return
                for i, chunk in enumerate(chunks):
                    if i == 1 and test.continue_stream is not None:
                        test.continue_stream.wait(3)
                    self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n' if streaming else chunk)
                if streaming:
                    self.wfile.write(b'0\r\n\r\n')

            do_GET = do_POST = do_HEAD = do_PATCH = handle_request

        self.upstream = spawn(Upstream)
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.addCleanup(ollama.stop)
        self.port = unused_port()
        self.cfg = {'ollama': {'enabled': True, 'port': self.port,
                              'upstream': f'http://127.0.0.1:{self.upstream.server_port}'}}
        ollama.start(self.db, self.cfg)
        self.assertTrue(ollama.status(self.cfg)['listening'])

    def post(self, path, payload):
        return request(self.port, 'POST', path, json.dumps(payload), {'Content-Type': 'application/json'})

    def records(self):
        return self.db.query('SELECT * FROM records')

    def assert_record(self, model, input, output, cache_read=0, cache_write=0):
        self.assertTrue(self.recorded.wait(2))
        row, = self.records()
        self.assertRegex(row['uid'], r'^o:[0-9a-f-]{36}$')
        self.assertEqual(row['source'], 'ollama')
        self.assertEqual(row['model'], 'ollama/' + model)
        self.assertEqual([row[k] for k in ('input', 'output', 'cache_read', 'cache_write')],
                         [input, output, cache_read, cache_write])
        self.assertEqual([row[k] for k in ('project', 'session', 'account')], [None] * 3)

    def test_native_chat_streams_before_upstream_finishes(self):
        first = b'{"model":"qwen:7b","message":{"content":"hi"},"done":false}\n'
        last = b'{"model":"qwen:7b","done":true,"prompt_eval_count":12,"eval_count":7}\n'
        self.response = (200, 'application/x-ndjson', [first, last[:10], last[10:]])
        self.continue_stream = threading.Event()
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            conn.request('POST', '/api/chat?x=1', b'{"model":"fallback"}')
            response = conn.getresponse()
            self.assertEqual(response.read(len(first)), first)
            self.assertFalse(self.recorded.is_set())
            self.continue_stream.set()
            self.assertEqual(response.read(), last)
        finally:
            self.continue_stream.set()
            conn.close()
        self.assertEqual(self.request_seen[1], '/api/chat?x=1')
        self.assert_record('qwen:7b', 12, 7)

    def test_native_generate_json_falls_back_to_request_model(self):
        body = b'{\n "done": true, "eval_count": 4\n}'
        self.response = (200, 'application/json', [body])
        self.assertEqual(self.post('/api/generate', {'model': 'tiny', 'stream': False})[2], body)
        self.assert_record('tiny', 0, 4)

    def test_openai_stream_injects_usage_only(self):
        self.response = (200, 'text/event-stream; charset=utf-8', [
            b'data: {"model":"llama3","usage":null}\r\n\r\n',
            b'data: {"usage":{"prompt_tokens":5,"completion_tokens":9}}\n\ndata: [DONE]\n\n'])
        payload = {'model': 'request', 'stream': True, 'stream_options': {'other': 2}, 'messages': []}
        self.assertEqual(self.post('/v1/chat/completions', payload)[2], b''.join(self.response[2]))
        self.assertEqual(json.loads(self.request_seen[3]), dict(payload, stream_options={'other': 2, 'include_usage': True}))
        self.assert_record('llama3', 5, 9)

    def test_openai_explicit_opt_out_is_byte_identical(self):
        body = b'{"stream": true, "stream_options": {"include_usage": false}}'
        self.response = (200, 'text/event-stream', [b'data: [DONE]\n\n'])
        request(self.port, 'POST', '/v1/completions', body)
        self.assertEqual(self.request_seen[3], body)
        self.assertEqual(self.records(), [])

    def test_openai_json(self):
        body = b'{"model":"local","usage":{"prompt_tokens":4,"completion_tokens":5}}'
        self.response = (200, 'application/json', [body])
        self.assertEqual(self.post('/v1/completions', {'model':'fallback'})[2], body)
        self.assert_record('local', 4, 5)

    def test_anthropic_stream_keeps_start_and_delta_counts(self):
        self.response = (200, 'text/event-stream', [
            b'event: message_start\ndata: {"type":"message_start","message":{"model":"local",'
            b'"usage":{"input_tokens":10,"cache_read_input_tokens":8,"cache_creation_input_tokens":2}}}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","usage":{"input_tokens":0,"output_tokens":6}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'])
        self.assertEqual(self.post('/v1/messages', {'model':'fallback', 'stream': True})[2], b''.join(self.response[2]))
        self.assert_record('local', 10, 6, 8, 2)

    def test_anthropic_json(self):
        body = b'{"model":"local","usage":{"input_tokens":8,"output_tokens":9,"cache_read_input_tokens":10}}'
        self.response = (200, 'application/json', [body])
        self.assertEqual(self.post('/v1/messages', {'model':'fallback'})[2], body)
        self.assert_record('local', 8, 9, 10)

    def test_non_model_route_preserves_methods_headers_and_chunked_body(self):
        self.response = (200, 'application/json', [b'{"models":[]}'])
        for method, body in [('GET', None), ('PATCH', iter([b'one', b'two']))]:
            status, headers, out = request(self.port, method, '/api/tags?test=1', body,
                                           {'Connection': 'X-Drop', 'X-Drop':'secret', 'X-Pass':'ok'})
            self.assertEqual((status, out), (200, b'{"models":[]}'))
            self.assertEqual(self.request_seen[:2], (method, '/api/tags?test=1'))
            self.assertIsNone(self.request_seen[2].get('X-Drop'))
            self.assertEqual(self.request_seen[2].get('X-Pass'), 'ok')
            self.assertEqual(self.request_seen[3], b'onetwo' if body else b'')
            self.assertNotIn('X-Drop-Response', dict(headers))
            self.assertEqual(dict(headers)['X-Pass-Response'], 'forward')
        self.assertEqual(self.records(), [])

    def test_upstream_error_passes_through(self):
        self.response = (429, 'application/json', [b'{"error":"busy"}'])
        status, _, body = self.post('/api/chat', {'model':'tiny'})
        self.assertEqual((status, body), (429, b'{"error":"busy"}'))
        self.assertEqual(self.records(), [])

    def test_parse_error_does_not_break_forwarding(self):
        self.response = (200, 'application/x-ndjson', [b'\xff bad\n', b'{"done":true,"eval_count":4}\n'])
        self.assertEqual(self.post('/api/chat', {'model':'tiny'})[2], b''.join(self.response[2]))
        self.assertEqual(self.records(), [])

    def test_body_cap_is_enforced_before_upstream(self):
        status, _, _ = request(self.port, 'POST', '/api/chat', b'', {'Content-Length': str(ollama.MAX_BODY + 1)})
        self.assertEqual(status, 413)
        self.assertIsNone(self.request_seen)

    def test_busy_port_is_reported_without_replacing_the_listener(self):
        self.cfg['ollama']['port'] = self.upstream.server_port
        ollama.start(self.db, self.cfg)
        self.assertFalse(ollama.status(self.cfg)['listening'])
        self.assertEqual(request(self.upstream.server_port, 'GET', '/api/tags')[0], 200)

    def test_upstream_change_applies_on_same_port(self):
        with ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler) as other:
            threading.Thread(target=other.serve_forever, kwargs={'poll_interval': .02}, daemon=True).start()
            try:
                self.cfg['ollama']['upstream'] = f'http://127.0.0.1:{other.server_port}'
                ollama.start(self.db, self.cfg)
                self.assertTrue(ollama.status(self.cfg)['listening'])
                self.assertEqual(request(self.port, 'GET', '/api/tags')[0], 501)
                self.assertIsNone(self.request_seen)
            finally:
                other.shutdown()

    def test_settings_change_rebinds_and_disabling_releases_ports(self):
        old = self.port
        new = unused_port()
        self.cfg['ollama']['port'] = new
        ollama.start(self.db, self.cfg)
        self.assertTrue(ollama.status(self.cfg)['listening'])
        with self.assertRaises(OSError):
            request(old, 'GET', '/api/tags')
        self.cfg['ollama']['enabled'] = False
        ollama.start(self.db, self.cfg)
        self.assertFalse(ollama.status(self.cfg)['listening'])
        with self.assertRaises(OSError):
            request(new, 'GET', '/api/tags')


class OllamaIntegration(unittest.TestCase):
    def setUp(self):
        self.db = DB(':memory:')
        self.addCleanup(self.db.conn.close)

    def test_free_local_models_skip_catalogs_and_ranking(self):
        now = time.time()
        self.db.upsert_records([dict(uid='local', ts=now, source='ollama',
                                     model='ollama/not-in-catalog', input=123)])
        self.assertIsNotNone(pricing.price_for('ollama/claude-sonnet'))
        self.assertEqual(pricing.cost('ollama/claude-sonnet', input=1000000), 0)
        with patch.object(pricing, '_catalog_prices') as catalog, patch.object(pricing, '_read_object', return_value={}), \
                patch.object(pricing, '_table', return_value=({}, [])), patch.object(pricing, 'data_dir', return_value=Path('.')):
            pricing._refresh_auto(self.db, 3)
        catalog.assert_not_called()
        self.assertEqual(sources.vendor_source('ollama/deepseek'), 'ollama')
        self.assertEqual(sources.meta()['ollama']['kind'], 'local')
        self.assertFalse(sources.is_subscription('ollama'))
        self.assertEqual(sources.refresh(self.db), [])
        self.assertEqual(export.dashboard_rows(self.db, now)['daily_detail'][0]['cost_usd'], 0)
        with patch.object(ranking, 'cost', return_value=99):
            spend = ranking._spend(self.db, 0, cfg={})
        self.assertNotIn('ollama', spend)
        self.assertFalse(any(ranking._period_tokens(self.db, {}).values()))

    def test_environment_upstream_default(self):
        for host, expected in [('0.0.0.0', 'http://127.0.0.1:11434'),
                               ('0.0.0.0:9999', 'http://127.0.0.1:9999'),
                               ('https://localhost', 'https://localhost:11434'),
                               ('[::1]', 'http://[::1]:11434')]:
            with self.subTest(host=host), patch.dict(ollama.os.environ, OLLAMA_HOST=host):
                self.assertEqual(ollama.config()['upstream'], expected)

    def test_settings_validates_saves_and_updates_listener(self):
        cfg = {'ollama': {'enabled': False, 'port': ollama.DEFAULT_PORT, 'upstream': ollama.DEFAULT_UPSTREAM}}
        with patch.object(server.paths, 'load_config', return_value=cfg), patch.object(server.paths, 'save_config', return_value=True) as save:
            engine = Engine(self.db)
            self.addCleanup(engine.stop)
            dashboard = spawn(server.make_handler(engine))
            self.addCleanup(dashboard.server_close)
            self.addCleanup(dashboard.shutdown)
            port = dashboard.server_port

            def get():
                return json.loads(request(port, 'GET', '/api/settings')[2])['ollama']

            def post(**values):
                return request(port, 'POST', '/api/settings', json.dumps({'ollama': values}))[0]

            self.assertFalse(get()['listening'])
            valid_port = unused_port()
            self.assertEqual(post(enabled=True, port=valid_port, upstream='http://127.0.0.1:11434'), 200)
            expected = dict(cfg['ollama'], url=f'http://127.0.0.1:{valid_port}', listening=True)
            self.assertEqual(get(), expected)
            for bad in (dict(enabled=1, port=True, upstream='file://localhost:11434'),
                        dict(port=1023, upstream='http://localhost'),
                        dict(port=65536, upstream='http://localhost:65536'),
                        dict(port=1.5, upstream='http://u:p@localhost:11434'),
                        dict(port='1234', upstream='http://' + 'x' * 200 + ':11434')):
                self.assertEqual(post(**bad), 200)
                self.assertEqual(get(), expected)
            save.return_value = False
            # A failed write must not turn off a running relay.
            self.assertEqual(post(enabled=False), 500)
            self.assertTrue(ollama.status({'ollama': expected})['listening'])
            save.return_value = True
            self.assertEqual(post(enabled=False), 200)
            self.assertFalse(get()['listening'])


if __name__ == '__main__':
    unittest.main()
