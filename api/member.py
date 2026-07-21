import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler


def code_ok(h):
    # 입장 코드: Vercel 환경변수 ENTRY_CODE 설정 시에만 검사
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
        if not code_ok(self):
            self._send_json({'error': 'bad_code', 'message': '입장 코드가 올바르지 않아요.'}, 401)
            return
        # 멤버 목록 (이메일은 노출하지 않음)
        try:
            rows = sb('GET', 'members?select=name,avatar&order=created_at.asc') or []
            self._send_json([{'name': r['name'], 'avatar': r.get('avatar') or '🐑'} for r in rows])
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def do_POST(self):
        if not code_ok(self):
            self._send_json({'error': 'bad_code', 'message': '입장 코드가 올바르지 않아요.'}, 401)
            return
        # 닉네임 등록/이메일 업데이트 (upsert)
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            name = (body.get('name') or '').strip()[:30]
            email = (body.get('email') or '').strip()[:100]
            avatar = (body.get('avatar') or '').strip()[:24]  # "🕊️|#cddbe1" 형태 (이모지+색) 수용
            if not name:
                self._send_json({'error': 'empty', 'message': '이름을 입력해 주세요.'}, 400)
                return
            payload = {'name': name}
            if email:
                payload['email'] = email
            if avatar:
                payload['avatar'] = avatar
            sb('POST', 'members?on_conflict=name', payload,
               extra_headers={'Prefer': 'resolution=merge-duplicates,return=minimal'})
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
