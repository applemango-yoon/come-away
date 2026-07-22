import json
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler


def member_ok(h):
    # 승인된 멤버(또는 관리자)만 허용. 입장 코드 없이 이름으로 인증.
    name = (h.headers.get('X-Member') or '').strip()
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

    def do_POST(self):
        if not member_ok(self):
            self._send_json({'error': 'bad_code', 'message': '승인되지 않은 이름이에요. 관리자에게 문의하세요.'}, 401)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            entry_id = body.get('id')
            author = (body.get('author') or '').strip()

            # 작성자 검증: 본인 기록만 수정 가능
            rows = sb('GET', f'entries?id=eq.{urllib.parse.quote(str(entry_id))}&select=id,author')
            if not rows:
                self._send_json({'error': 'not_found', 'message': '기록을 찾을 수 없어요.'}, 404)
                return
            if rows[0].get('author') and rows[0]['author'] != author:
                self._send_json({'error': 'forbidden',
                                 'message': f"'{rows[0]['author']}'님의 기록이에요. 본인 기록만 수정할 수 있어요."}, 403)
                return

            patch = {
                'summary': body.get('summary', ''),
                'action': body.get('action', ''),
                'words': body.get('words', [])
            }
            if 'public' in body:
                patch['public'] = bool(body.get('public'))
            sb('PATCH', f'entries?id=eq.{urllib.parse.quote(str(entry_id))}', patch)
            self._send_json({'ok': True})
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
