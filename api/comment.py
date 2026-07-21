import json
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler


def code_ok(h):
    # 입장 코드: Vercel 환경변수 ENTRY_CODE 설정 시에만 검사
    code = os.environ.get('ENTRY_CODE', '')
    return (not code) or h.headers.get('X-Entry-Code', '') == code


def sb(method, path, data=None):
    url = os.environ['SUPABASE_URL'] + '/rest/v1/' + path
    req = urllib.request.Request(url, method=method)
    req.add_header('apikey', os.environ['SUPABASE_ANON_KEY'])
    req.add_header('Authorization', 'Bearer ' + os.environ['SUPABASE_ANON_KEY'])
    req.add_header('Content-Type', 'application/json')
    if data is not None:
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req) as r:
        body = r.read()
        return json.loads(body) if body else None


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if not code_ok(self):
            self._send_json({'error': 'bad_code', 'message': '입장 코드가 올바르지 않아요.'}, 401)
            return
        try:
            rows = sb('GET', 'comments?select=id,entry_id,author,text&order=created_at.asc') or []
            self._send_json(rows)
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def do_POST(self):
        if not code_ok(self):
            self._send_json({'error': 'bad_code', 'message': '입장 코드가 올바르지 않아요.'}, 401)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            action = body.get('action', 'add')
            author = (body.get('author') or '').strip()[:30]

            if action == 'add':
                entry_id = body.get('entry_id')
                text = (body.get('text') or '').strip()[:300]
                if not entry_id or not author or not text:
                    self._send_json({'error': 'bad_request', 'message': '내용을 입력해 주세요.'}, 400)
                    return
                sb('POST', 'comments', {'entry_id': entry_id, 'author': author, 'text': text})
                self._send_json({'ok': True})

            elif action == 'delete':
                cid = body.get('id')
                rows = sb('GET', f'comments?id=eq.{urllib.parse.quote(str(cid))}&select=id,author')
                if not rows:
                    self._send_json({'error': 'not_found'}, 404)
                    return
                if rows[0]['author'] != author:
                    self._send_json({'error': 'forbidden', 'message': '본인 댓글만 지울 수 있어요.'}, 403)
                    return
                sb('DELETE', f'comments?id=eq.{urllib.parse.quote(str(cid))}')
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
