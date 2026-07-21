import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

KST = timezone(timedelta(hours=9))


def code_ok(h):
    code = os.environ.get('ENTRY_CODE', '').strip()
    return (not code) or h.headers.get('X-Entry-Code', '').strip() == code


def sb(path):
    url = os.environ['SUPABASE_URL'] + '/rest/v1/' + path
    req = urllib.request.Request(url)
    req.add_header('apikey', os.environ['SUPABASE_ANON_KEY'])
    req.add_header('Authorization', 'Bearer ' + os.environ['SUPABASE_ANON_KEY'])
    with urllib.request.urlopen(req) as r:
        body = r.read()
        return json.loads(body) if body else []


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        # 전체 데이터 백업 (JSON 다운로드) — 입장 코드 필요
        if not code_ok(self):
            self._send_json({'error': 'bad_code', 'message': '입장 코드가 올바르지 않아요.'}, 401)
            return
        try:
            dump = {
                'exported_at': datetime.now(KST).isoformat(),
                'entries': sb('entries?select=*&order=created_at.asc'),
                'members': sb('members?select=name,avatar,created_at&order=created_at.asc'),
                'reactions': sb('reactions?select=*'),
                'shells': sb('shells?select=*&order=created_at.asc'),
                'shell_uses': sb('shell_uses?select=*&order=created_at.asc'),
                'comments': sb('comments?select=*&order=created_at.asc'),
            }
            body = json.dumps(dump, ensure_ascii=False, indent=2).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            fname = 'come-away-backup-' + datetime.now(KST).strftime('%Y%m%d') + '.json'
            self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
