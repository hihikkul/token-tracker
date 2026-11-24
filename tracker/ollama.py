"""Opt-in loopback Ollama relay. Only response token counts are stored."""
import http.client
import json
import logging
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from uuid import uuid4

DEFAULT_PORT = 11435
DEFAULT_UPSTREAM = 'http://127.0.0.1:11434'
MAX_BODY = 64 * 1024 * 1024
MAX_EVENT = 4 * 1024 * 1024  # accounting must not buffer an unbounded stream or JSON response
NATIVE = {'/api/chat', '/api/generate'}
OPENAI = {'/v1/chat/completions', '/v1/completions'}
ANTHROPIC = '/v1/messages'
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-connection', 'proxy-authenticate',
               'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade'}


def _url(value):
    if (not isinstance(value, str) or len(value) > 200
            or re.search(r'[\s\x00-\x1f\x7f\\?#]', value)):
        raise ValueError('invalid upstream URL')
    p = urlsplit(value)
    if (p.scheme not in ('http', 'https') or not p.hostname or p.username is not None
            or p.path not in ('', '/') or p.port == 0):
        raise ValueError('invalid upstream URL')
    return p


def default_upstream():
    raw = os.environ.get('OLLAMA_HOST', '').strip()
    try:
        p = _url(raw if '://' in raw else 'http://' + raw)
        host = '127.0.0.1' if p.hostname == '0.0.0.0' else p.hostname
        if ':' in host:
            host = f'[{host}]'
        return f'{p.scheme}://{host}:{p.port or 11434}'
    except ValueError:
        return DEFAULT_UPSTREAM


def config(values=None, base=None):
    """Merge only valid fields, as with other dashboard settings."""
    result = {'enabled': False, 'port': DEFAULT_PORT, 'upstream': default_upstream()}
    for obj in (base, values):
        if not isinstance(obj, dict):
            continue
        if type(obj.get('enabled')) is bool:
            result['enabled'] = obj['enabled']
        port = obj.get('port')
        if type(port) is int and 1024 <= port <= 65535:
            result['port'] = port
        try:
            if _url(obj.get('upstream')).port is not None:
                result['upstream'] = obj['upstream']
        except ValueError:
            pass
    return result


def _headers(headers, *exclude):
    drop = HOP_HEADERS.union(exclude)
    for value in headers.get_all('Connection', []):
        drop.update(part.strip().lower() for part in value.split(','))
    return [(k, v) for k, v in headers.items() if k.lower() not in drop]


def _object(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('expected an object')
    return value


class Usage:
    def __init__(self, path, model, content_type):
        self.path, self.request_model = path, model
        self.mode = ('sse' if content_type == 'text/event-stream' else
                     'ndjson' if content_type == 'application/x-ndjson' else 'json')
        self.pending = bytearray()
        self.event = bytearray()
        self.model = None
        self.counts = dict.fromkeys(('input', 'output', 'cache_read', 'cache_write'), 0)
        self.complete = False

    def _counts(self, usage, **names):
        if not isinstance(usage, dict):
            raise ValueError('invalid usage')
        for dest, field in names.items():
            n = usage.get(field)
            if n is None:
                continue
            if type(n) is not int or not 0 <= n < 2**63:
                raise ValueError('invalid token count')
            self.counts[dest] = n

    def _parse(self, raw):
        obj = _object(raw)
        if 'error' in obj or obj.get('type') == 'error':
            raise ValueError('upstream error')
        if isinstance(obj.get('model'), str) and obj['model']:
            self.model = obj['model']
        if self.path in NATIVE:
            if obj.get('done') is True:
                self._counts(obj, input='prompt_eval_count', output='eval_count')
                self.complete = True
        elif self.path in OPENAI:
            if obj.get('usage') is not None:
                self._counts(obj['usage'], input='prompt_tokens', output='completion_tokens')
                self.complete = True
        else:
            kind = obj.get('type')
            if kind == 'message_start':
                msg = obj['message']
                self.model = msg.get('model') or self.model
                self._anthropic(msg.get('usage', {}))
            elif kind == 'message_delta':
                if obj.get('usage') is not None:
                    self._counts(obj['usage'], output='output_tokens')
                    self.complete = True
            elif self.mode == 'json' and obj.get('usage') is not None:
                self._anthropic(obj['usage'])
                self.complete = True

    def _anthropic(self, usage):
        self._counts(usage, input='input_tokens', output='output_tokens',
                     cache_read='cache_read_input_tokens', cache_write='cache_creation_input_tokens')

    @staticmethod
    def _append(buf, chunk):
        if len(buf) + len(chunk) > MAX_EVENT:
            raise ValueError('usage parse limit')
        buf.extend(chunk)

    def _line(self, line):
        if self.mode == 'ndjson':
            if line.strip():
                self._parse(line)
        elif line.startswith(b'data:'):
            if self.event:
                self._append(self.event, b'\n')
            data = line[5:]
            self._append(self.event, data[1:] if data.startswith(b' ') else data)
        elif not line and self.event:
            if self.event.strip() != b'[DONE]':
                self._parse(self.event)
            self.event.clear()

    def feed(self, chunk):
        if self.mode == 'json':
            self._append(self.pending, chunk)
            return
        lines = chunk.split(b'\n')
        for line in lines[:-1]:
            self._append(self.pending, line)
            self._line(self.pending.rstrip(b'\r'))
            self.pending.clear()
        self._append(self.pending, lines[-1])

    def finish(self):
        if self.mode == 'json':
            self._parse(self.pending)
        elif self.pending:
            self._line(self.pending.rstrip(b'\r'))
        if self.mode == 'sse':
            self._line(b'')
        model = self.model or self.request_model
        if self.complete and isinstance(model, str) and model:
            return dict(uid=f'o:{uuid4()}', source='ollama', ts=time.time(), model='ollama/' + model,
                        project=None, session=None, account=None, **self.counts)
        return None


_PARSE_ERRORS = (ValueError, TypeError, KeyError, AttributeError, RecursionError)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 30

    def log_message(self, *args):
        pass

    def __getattr__(self, name):
        if name.startswith('do_'):
            return self._relay  # forward methods beyond the usual GET, POST, etc.
        raise AttributeError(name)

    def _allowed_host(self):
        port = self.server.server_port
        return self.headers.get('Host', '').lower() in (f'localhost:{port}', f'127.0.0.1:{port}')

    def handle_expect_100(self):
        if not self._allowed_host():
            self.send_error(403)
            return False
        return super().handle_expect_100()

    def _read_body(self):
        lengths = self.headers.get_all('Content-Length', [])
        encodings = self.headers.get_all('Transfer-Encoding', [])
        if len(lengths) > 1 or (encodings and lengths):
            raise ValueError('ambiguous request length')
        if not encodings:
            length = lengths[0].strip() if lengths else '0'
            if not re.fullmatch(r'[0-9]+', length):
                raise ValueError('invalid length')
            n = int(length)
            if n > MAX_BODY:
                raise OverflowError
            body = self.rfile.read(n)
            if len(body) != n:
                raise ValueError('incomplete body')
            return body
        if [v.lower().strip() for v in encodings] != ['chunked']:
            raise ValueError('unsupported transfer encoding')
        body = bytearray()
        while True:
            line = self.rfile.readline(8193)
            size = line.split(b';', 1)[0].strip()
            if len(line) > 8192 or not line.endswith(b'\r\n') or not re.fullmatch(rb'[0-9a-fA-F]+', size):
                raise ValueError('invalid chunk')
            n = int(size, 16)
            if not n:
                while True:  # trailers are hop-by-hop; bound their total length
                    trailer = self.rfile.readline(8193)
                    if trailer == b'\r\n':
                        return body
                    if not trailer.endswith(b'\r\n') or len(trailer) > 8192:
                        raise ValueError('invalid trailer')
                    line += trailer
                    if len(line) > 16384:
                        raise ValueError('trailers too large')
            if n > MAX_BODY - len(body):
                raise OverflowError
            body.extend(self.rfile.read(n))
            if self.rfile.read(2) != b'\r\n':
                raise ValueError('incomplete chunk')

    def _relay(self):
        self.close_connection = True
        if not self._allowed_host():
            return self.send_error(403)
        try:
            body = self._read_body()
        except OverflowError:
            return self.send_error(413)
        except ValueError:
            return self.send_error(400)
        except OSError:
            return self.send_error(408)
        path = self.path.split('?', 1)[0]
        tracked = self.command == 'POST' and (path in NATIVE or path in OPENAI or path == ANTHROPIC)
        model = None
        if tracked:
            try:
                obj = _object(body)
                model = obj.get('model')
                if path in OPENAI and obj.get('stream') is True:
                    options = obj.get('stream_options')
                    if options is None or (isinstance(options, dict) and 'include_usage' not in options):
                        obj['stream_options'] = dict(options or {}, include_usage=True)
                        body = json.dumps(obj, separators=(',', ':')).encode()
            except _PARSE_ERRORS:
                pass

        upstream = self.server.upstream
        cls = http.client.HTTPSConnection if upstream.scheme == 'https' else http.client.HTTPConnection
        conn = cls(upstream.hostname, upstream.port, timeout=300)
        started = False
        try:
            conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            headers = _headers(self.headers, 'content-length', 'expect')
            for k, v in headers:
                conn.putheader(k, v)
            if not any(k.lower() == 'host' for k, _ in headers):
                conn.putheader('Host', upstream.netloc)
            if body or 'Content-Length' in self.headers or 'Transfer-Encoding' in self.headers:
                conn.putheader('Content-Length', str(len(body)))
            conn.endheaders(body)
            with conn.getresponse() as response:
                started = True
                self._reply(response, path if tracked else None, model)
        except (OSError, http.client.HTTPException, ValueError):
            if not started:
                self.send_error(502, 'Ollama upstream unavailable')
        finally:
            conn.close()

    def _reply(self, response, path, model):
        no_body = self.command == 'HEAD' or response.status in (204, 304) or 100 <= response.status < 200
        length = response.length
        chunked = not no_body and length is None and self.request_version == 'HTTP/1.1'
        self.send_response_only(response.status, response.reason)
        for k, v in _headers(response.headers, 'content-length'):
            self.send_header(k, v)
        if chunked:
            self.send_header('Transfer-Encoding', 'chunked')
        elif no_body and (self.command == 'HEAD' or response.status == 304):
            if response.headers.get('Content-Length') is not None:
                self.send_header('Content-Length', response.headers['Content-Length'])
        elif not no_body and length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('Connection', 'close')
        self.end_headers()
        if no_body:
            return

        usage = (Usage(path, model, response.headers.get_content_type())
                 if path and 200 <= response.status < 300
                 and response.headers.get('Content-Encoding', 'identity').lower() == 'identity' else None)
        while chunk := response.read1(65536):  # don't wait for a full read buffer before forwarding
            if chunked:
                self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
            else:
                self.wfile.write(chunk)
            if usage:
                try:
                    usage.feed(chunk)
                except _PARSE_ERRORS:
                    usage = None
        if length is not None and response.length:  # read1 can return EOF on a truncated fixed body
            return
        if usage:
            try:
                row = usage.finish()
            except _PARSE_ERRORS:
                row = None
            if row:
                try:
                    self.server.db.upsert_records([row])
                except Exception:
                    logging.getLogger(__name__).exception('Could not store Ollama usage')
        if chunked:
            self.wfile.write(b'0\r\n\r\n')


class RelayServer(ThreadingHTTPServer):
    allow_reuse_port = False
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt':
            # On Windows SO_REUSEADDR can bind over an existing listener.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


_lock = threading.Lock()
_server = None


def _stop():
    global _server
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None


def start(engine_or_db, cfg):
    """Apply relay settings. A bind failure must not stop the dashboard."""
    global _server
    cfg = config(cfg.get('ollama'))
    with _lock:
        if not cfg['enabled']:
            _stop()
            return
        upstream = _url(cfg['upstream'])
        db = getattr(engine_or_db, 'db', engine_or_db)
        if _server is not None and _server.server_port == cfg['port']:
            # Keep the port bound; the new upstream applies to subsequent requests.
            _server.upstream, _server.db = upstream, db
            return
        _stop()
        try:
            _server = RelayServer(('127.0.0.1', cfg['port']), Handler)
        except OSError:
            return
        _server.upstream, _server.db = upstream, db
        threading.Thread(target=_server.serve_forever, kwargs={'poll_interval': .05},
                         daemon=True, name='ollama-relay').start()


def stop():
    with _lock:
        _stop()


def status(cfg):
    cfg = config(cfg.get('ollama'))
    with _lock:
        listening = (_server is not None and _server.server_port == cfg['port']
                     and _server.upstream == _url(cfg['upstream']))
    return dict(cfg, url=f"http://127.0.0.1:{cfg['port']}", listening=listening)
