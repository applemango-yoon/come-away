import json
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler


def code_ok(h):
    code = os.environ.get('ENTRY_CODE', '')
    return (not code) or h.headers.get('X-Entry-Code', '') == code


def sb(method, path, data=None, extra_headers=None):
    url = os.environ['SUPABASE_URL'] + '/rest/v1/' + path
    req = urllib.request.Request(url, method=method)
    req.add_header('apikey', os.environ['SUPABASE_ANON_KEY'])
    req.add_header('Authorization', 'Bearer ' + os.environ['SUPABASE_ANON_KEY'])
    req.add_header('Content-Type', 'application/json')
    if extra_headers:
        for k, v in extra_headers.items():
            req.add_header(k, v)
    if data is not None:
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req) as r:
        body = r.read()
        return json.loads(body) if body else None


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        # 프론트가 구독할 때 필요한 VAPID 공개키를 내려줌 (없으면 빈 문자열 → 알림 기능 비활성)
        if not code_ok(self):
            self._send_json({'error': 'bad_code'}, 401)
            return
        self._send_json({'publicKey': os.environ.get('VAPID_PUBLIC_KEY', '')})

    def do_POST(self):
        if not code_ok(self):
            self._send_json({'error': 'bad_code', 'message': '입장 코드가 올바르지 않아요.'}, 401)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            action = body.get('action', 'subscribe')

            if action == 'subscribe':
                sub = body.get('subscription') or {}
                name = (body.get('name') or '').strip()[:30]
                endpoint = sub.get('endpoint')
                if not endpoint or not name:
                    self._send_json({'error': 'bad_request'}, 400)
                    return
                sb('POST', 'push_subscriptions?on_conflict=endpoint',
                   {'endpoint': endpoint, 'name': name, 'sub': sub},
                   extra_headers={'Prefer': 'resolution=merge-duplicates,return=minimal'})
                self._send_json({'ok': True})

            elif action == 'unsubscribe':
                endpoint = body.get('endpoint')
                if endpoint:
                    sb('DELETE', 'push_subscriptions?endpoint=eq.' + urllib.parse.quote(endpoint))
                self._send_json({'ok': True})

            else:
                self._send_json({'error': 'bad_request'}, 400)
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
