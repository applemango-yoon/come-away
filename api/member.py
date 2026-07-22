import json
import os
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


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


def admins_list():
    return [a.strip().lower() for a in os.environ.get('ADMIN_NAMES', '').split(',') if a.strip()]


def is_admin_name(name):
    return (name or '').strip().lower() in admins_list()


def member_row(name):
    """이름으로 멤버 1건 조회. 없으면 None."""
    name = (name or '').strip()
    if not name:
        return None
    try:
        rows = sb('GET', 'members?name=eq.' + urllib.parse.quote(name, safe='') + '&select=name,avatar')
        return rows[0] if rows else None
    except Exception:
        return None


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    # ── 인증: 승인된 멤버(또는 관리자)만 ──
    def _member_ok(self):
        name = (self.headers.get('X-Member') or '').strip()
        return bool(name) and (is_admin_name(name) or member_row(name) is not None)

    def _deny(self):
        self._send_json({'error': 'not_member', 'message': '승인되지 않은 이름이에요. 관리자에게 문의하세요.'}, 401)

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        login = (q.get('login', [''])[0] or '').strip()

        # 1) 로그인 확인 (인증 불필요): 승인된 이름인지 검사하고 프로필 반환
        if login:
            row = member_row(login)
            admin = is_admin_name(login)
            # 관리자인데 멤버 목록에 없으면 자동 등록(커뮤니티에 표시되도록)
            if admin and not row:
                try:
                    sb('POST', 'members', {'name': login}, extra_headers={'Prefer': 'return=minimal'})
                    row = {'name': login, 'avatar': None}
                except Exception:
                    pass
            if row or admin:
                avatar = (row.get('avatar') if row else None) or '🐑|#c9d6bd'
                self._send_json({'ok': True, 'name': login, 'avatar': avatar, 'admin': admin})
            else:
                self._send_json({'ok': False, 'message': '승인되지 않은 이름이에요. 관리자에게 문의하세요.'})
            return

        # 2) 멤버 목록 (로그인 필요)
        if not self._member_ok():
            self._deny()
            return
        try:
            rows = sb('GET', 'members?select=name,avatar&order=created_at.asc') or []
            self._send_json([{'name': r['name'], 'avatar': r.get('avatar') or '🐑'} for r in rows])
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            body = {}
        action = (body.get('action') or '').strip()
        caller = (self.headers.get('X-Member') or '').strip()

        # ── 관리자: 멤버 추가 / 삭제 (승인 관리) ──
        if action in ('add', 'remove'):
            if not is_admin_name(caller):
                self._send_json({'error': 'forbidden', 'message': '관리자만 멤버를 관리할 수 있어요.'}, 403)
                return
            name = (body.get('name') or '').strip()[:30]
            if not name:
                self._send_json({'error': 'empty', 'message': '이름을 입력해 주세요.'}, 400)
                return
            qname = urllib.parse.quote(name, safe='')
            try:
                if action == 'add':
                    if member_row(name):
                        self._send_json({'ok': True, 'already': True})
                        return
                    avatar = (body.get('avatar') or '').strip()[:24]
                    payload = {'name': name}
                    if avatar:
                        payload['avatar'] = avatar
                    sb('POST', 'members', payload, extra_headers={'Prefer': 'return=minimal'})
                    self._send_json({'ok': True})
                else:  # remove
                    sb('DELETE', 'members?name=eq.' + qname, extra_headers={'Prefer': 'return=minimal'})
                    self._send_json({'ok': True})
            except Exception as e:
                self._send_json({'error': str(e)}, 500)
            return

        # ── 본인 프로필(캐릭터/이메일) 수정 (승인된 멤버만) ──
        if not self._member_ok():
            self._deny()
            return
        try:
            name = (body.get('name') or caller or '').strip()[:30]
            if not name:
                self._send_json({'error': 'empty', 'message': '이름을 입력해 주세요.'}, 400)
                return
            if name.lower() != caller.lower() and not is_admin_name(caller):
                self._send_json({'error': 'forbidden', 'message': '본인 프로필만 수정할 수 있어요.'}, 403)
                return
            email = (body.get('email') or '').strip()[:100]
            avatar = (body.get('avatar') or '').strip()[:24]
            payload = {}
            if email:
                payload['email'] = email
            if avatar:
                payload['avatar'] = avatar
            if not payload:
                self._send_json({'ok': True})
                return
            qname = urllib.parse.quote(name, safe='')
            if member_row(name):
                sb('PATCH', 'members?name=eq.' + qname, payload, extra_headers={'Prefer': 'return=minimal'})
            else:
                payload['name'] = name
                sb('POST', 'members', payload, extra_headers={'Prefer': 'return=minimal'})
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
