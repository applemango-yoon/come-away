import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler


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


def sb(method, path, data=None, extra_headers=None):
    url = os.environ['SUPABASE_URL'] + '/rest/v1/' + path
    req = urllib.request.Request(url, method=method)
    req.add_header('apikey', os.environ['SUPABASE_ANON_KEY'])
    req.add_header('Authorization', 'Bearer ' + os.environ['SUPABASE_ANON_KEY'])
    req.add_header('Content-Type', 'application/json')
    req.add_header('Prefer', 'return=representation')
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
        if not member_ok(self):
            self._send_json({'error': 'bad_code', 'message': '승인되지 않은 이름이에요. 관리자에게 문의하세요.'}, 401)
            return
        try:
            def u8(s):
                # 일부 클라이언트가 비ASCII를 raw로 보낼 때 latin-1로 잘못 읽히는 것 보정
                try:
                    return s.encode('latin-1').decode('utf-8')
                except (UnicodeDecodeError, UnicodeEncodeError):
                    return s
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q = u8((qs.get('q', [''])[0] or '').strip())
            author = u8((qs.get('author', [''])[0] or '').strip())
            me = u8((qs.get('me', [''])[0] or '').strip())

            path = 'entries?select=*&order=date.asc,created_at.asc'
            if author:
                path += '&author=eq.' + urllib.parse.quote(author)
            elif me:
                # 광장/내기록 공용: 공개 글 + 내 글(비공개 포함)만
                path += '&or=(public.is.true,author.eq.' + urllib.parse.quote(me) + ')'
            else:
                path += '&public=is.true'
            if q:
                # 서버 검색: 구절·묵상·액션플랜에서 부분 일치 (단어칩은 클라이언트에서 추가 필터)
                pat = '*' + q.replace(',', ' ').replace('(', ' ').replace(')', ' ') + '*'
                pat = urllib.parse.quote(pat)
                path += f'&or=(passage.ilike.{pat},summary.ilike.{pat},action.ilike.{pat})'
            entries = sb('GET', path)
            self._send_json(entries)
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def do_POST(self):
        if not member_ok(self):
            self._send_json({'error': 'bad_code', 'message': '승인되지 않은 이름이에요. 관리자에게 문의하세요.'}, 401)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            author = (body.get('author') or '익명').strip()[:30]
            is_public = body.get('public')
            is_public = True if is_public is None else bool(is_public)
            try:
                sb('POST', 'entries', {
                    'date': body.get('date', ''),
                    'passage': body.get('passage', ''),
                    'summary': body.get('summary', ''),
                    'action': body.get('action', ''),
                    'words': body.get('words', []),
                    'author': author,
                    'public': is_public
                })
            except urllib.error.HTTPError as he:
                if he.code == 409:
                    # (date, author) 유니크 제약: 같은 날 두 번째 저장
                    self._send_json({'error': 'duplicate',
                                     'message': '오늘은 이미 기록했어요. 대시보드에서 기존 기록을 편집해 주세요.'}, 409)
                    return
                raise
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
