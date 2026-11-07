"""Offline loopback web UI with no cloud service or additional UI dependencies."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .locking import ProcessLock
from .web_session import SessionError, WebSession
from .web_instance import config_key, existing_url, publish_instance, remove_instance


ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/robot.js': ('robot.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8'),
          '/favicon.svg': ('favicon.svg', 'image/svg+xml')}


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, session, port=8780):
        self.session = session
        self.token = secrets.token_urlsafe(32)
        self.instance_id = secrets.token_hex(16)
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = 'http://127.0.0.1:'+str(self.server_port)
        self.launch_url = self.origin+'/#token='+self.token

    def process_request(self, request, client_address):
        if not self.slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'SawyerLocal/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass  # Bounded in-memory application events; no routine telemetry/access logs.

    def _respond(self, code, data, content_type='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, allow_nan=False, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _check_origin(self):
        # Defend loopback against other websites and DNS rebinding.
        if self.headers.get('Host') != self.server.origin[len('http://'):]:
            raise PermissionError('Invalid local host.')
        origin = self.headers.get('Origin')
        if origin is not None and origin != self.server.origin:
            raise PermissionError('Only the local operator page can access this service.')

    def _authorize(self, body=None):
        self._check_origin()
        token = self.headers.get('X-Sawyer-Token') or (body or {}).get('token', '')
        if not isinstance(token, str) or not hmac.compare_digest(token.encode('utf-8'), self.server.token.encode('ascii')):
            raise PermissionError('Open the URL shown by Start-A to authorize this page.')
        client = self.headers.get('X-Sawyer-Client') or (body or {}).get('client', '')
        if not isinstance(client, str) or not re.fullmatch(r'[A-Za-z0-9-]{16,64}', client):
            raise PermissionError('Invalid browser session.')
        return client

    def do_GET(self):
        try:
            self._check_origin()
            url = urlsplit(self.path)
            if url.path in ASSETS:
                filename, content_type = ASSETS[url.path]
                return self._respond(200, (Path(__file__).parent/'web'/filename).read_bytes(), content_type)
            client = self._authorize()
            query = parse_qs(url.query)
            if url.path == '/api/instance':
                result = dict(service='sawyer-web', instance=self.server.instance_id,
                              config=config_key(self.server.session.path))
            elif url.path == '/api/state':
                after = max(0, int(query.get('after', ['0'])[0]))
                result = self.server.session.snapshot(client, after, query.get('view') == ['1'])
            elif url.path == '/api/camera':
                try:
                    data = self.server.session.cameras.jpeg(query.get('name', [''])[0])
                except KeyError:
                    return self._respond(404, {'error': 'Unknown camera'})
                except RuntimeError as exc:
                    return self._respond(503, {'error': str(exc)})
                return self._respond(200, data, 'image/jpeg')
            elif url.path == '/api/folders':
                path = Path(query.get('path', [self.server.session.defaults()['output']])[0]).expanduser().resolve()
                while not path.exists() and path != path.parent:
                    path = path.parent
                if not path.is_dir():
                    raise SessionError('Select a folder on the A computer.')
                folders = []
                for child in sorted(path.iterdir(), key=lambda p: p.name.casefold()):
                    if child.is_dir():
                        folders.append({'name': child.name, 'path': str(child)})
                        if len(folders) >= 500:
                            break
                drives = [str(Path(letter+':/')) for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if Path(letter+':/').is_dir()] if os.name == 'nt' else ['/']
                result = dict(path=str(path), parent=str(path.parent), folders=folders, roots=drives)
            else:
                return self._respond(404, {'error': 'Not found'})
            self._respond(200, result)
        except PermissionError as exc:
            self._respond(403, {'error': str(exc)})
        except (ValueError, OSError) as exc:
            self._respond(400, {'error': str(exc)})

    def do_POST(self):
        try:
            self._check_origin()
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise SessionError('Invalid request length.')
            if self.headers.get_content_type() not in ('application/json', 'text/plain'):
                raise SessionError('Expected JSON.')
            def reject_constant(value):
                raise SessionError('Non-finite JSON value: '+value)
            body = json.loads(self.rfile.read(size), parse_constant=reject_constant)
            if not isinstance(body, dict):
                raise SessionError('Expected a request object.')
            client = self._authorize(body if self.path == '/api/release' else None)
            if self.path == '/api/claim':
                self.server.session.claim(client)
                result = {'ok': True}
            elif self.path == '/api/release':
                self.server.session.release(client)
                result = {'ok': True}
            elif self.path == '/api/action':
                result = self.server.session.command(client, body.get('seq'), body.get('action'), body.get('options', {}))
            else:
                return self._respond(404, {'error': 'Not found'})
            self._respond(200, result)
        except PermissionError as exc:
            self._respond(403, {'error': str(exc)})
        except (ValueError, OSError, RuntimeError) as exc:
            self._respond(409, {'error': str(exc)})


def run_web(config_path, port=8780, open_browser=True):
    lock = ProcessLock('web-config:'+str(Path(config_path).resolve()))
    try:
        lock.acquire()
    except RuntimeError:
        url = existing_url(config_path)
        if url:
            print('Web A is already running. Reusing the existing service.', flush=True)
            print(url, flush=True)
            if open_browser:
                webbrowser.open(url)
            return True
        print('Web A is already running for this configuration. Use its existing browser page.\n'
              'To load updated code, first Save & End and Disconnect, then press Ctrl+C in the\n'
              'original Start-A window. After it exits, run Start-A again.\n'
              'No process or lock file was removed.', flush=True)
        return False
    session = server = None
    try:
        session = WebSession(config_path)
        server = WebServer(session, port)
        session.start()
        publish_instance(config_path, server)
        print('Sawyer Web · local A operator station', flush=True)
        print(server.launch_url, flush=True)
        print('Keep this window open; Ctrl+C stops Web A.', flush=True)
        if open_browser:
            webbrowser.open(server.launch_url)
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        if session:
            session.close()
        if server:
            remove_instance(config_path, server.instance_id)
        lock.close()
    return True
