import json
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

ADMINS = [a.strip() for a in os.environ.get('ADMIN_NAMES', '').split(',') if a.strip()]


def member_ok(h):
    # 승인된 멤버(또는 관리자)만 허용. 입장 코드 없이 이름으로 인증.
    name = urllib.parse.unquote(h.headers.get('X-Member') or '').strip()
    if not name:
        return False
    admins = [a.strip().lower() for a in os.environ.get('ADMIN_NAMES', '').split(',') if a.strip()]
    if name.lower() in admins:
        return True
    try:
        rows = sb('GET', 'members?name=eq.' + urllib.parse.quote(name, safe='') + '&select=name')
        return bool(rows)
    except Exception:
        return False


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
        if not member_ok(self):
            self._send_json({'error': 'bad_code'}, 401)
            return
        try:
            rows = sb('GET', 'announcements?select=id,title,body,author,active&order=created_at.desc') or []
            self._send_json([r for r in rows if r.get('active')])
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def do_POST(self):
        if not member_ok(self):
            self._send_json({'error': 'bad_code', 'message': '승인되지 않은 이름이에요. 관리자에게 문의하세요.'}, 401)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            author = (body.get('author') or '').strip()
            if author not in ADMINS:
                self._send_json({'error': 'forbidden', 'message': '관리자만 칠판을 쓸 수 있어요.'}, 403)
                return
            action = body.get('action', 'add')

            if action == 'add':
                title = (body.get('title') or '').strip()[:80]
                text = (body.get('body') or '').strip()[:500]
                if not title:
                    self._send_json({'error': 'bad_request', 'message': '제목을 입력해 주세요.'}, 400)
                    return
                sb('POST', 'announcements', {'title': title, 'body': text, 'author': author, 'active': True})
                self._send_json({'ok': True})

            elif action == 'delete':
                aid = body.get('id')
                sb('DELETE', 'announcements?id=eq.' + urllib.parse.quote(str(aid)))
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
