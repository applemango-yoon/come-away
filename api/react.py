import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

ALLOWED = ['🙏', '❤️', '🔥']


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
            self._send_json({'error': 'bad_code', 'message': '승인되지 않은 이름이에요. 관리자에게 문의하세요.'}, 401)
            return
        try:
            rows = sb('GET', 'reactions?select=entry_id,author,emoji') or []
            self._send_json(rows)
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def do_POST(self):
        if not member_ok(self):
            self._send_json({'error': 'bad_code', 'message': '승인되지 않은 이름이에요. 관리자에게 문의하세요.'}, 401)
            return
        # 토글: 이미 눌렀으면 취소, 아니면 추가
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            entry_id = body.get('entry_id')
            author = (body.get('author') or '').strip()[:30]
            emoji = body.get('emoji', '')
            if not author or emoji not in ALLOWED or not entry_id:
                self._send_json({'error': 'bad_request'}, 400)
                return
            q = (f"reactions?entry_id=eq.{urllib.parse.quote(str(entry_id))}"
                 f"&author=eq.{urllib.parse.quote(author)}&emoji=eq.{urllib.parse.quote(emoji)}")
            existing = sb('GET', q + '&select=id')
            if existing:
                sb('DELETE', q)
                self._send_json({'ok': True, 'toggled': 'off'})
            else:
                try:
                    sb('POST', 'reactions', {'entry_id': entry_id, 'author': author, 'emoji': emoji})
                except urllib.error.HTTPError as he:
                    if he.code != 409:
                        raise
                self._send_json({'ok': True, 'toggled': 'on'})
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
