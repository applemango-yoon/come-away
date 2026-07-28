import json
import os
import datetime
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ADMINS = [a.strip() for a in os.environ.get('ADMIN_NAMES', '').split(',') if a.strip()]


def is_admin_name(name):
    admins = [a.strip().lower() for a in os.environ.get('ADMIN_NAMES', '').split(',') if a.strip()]
    return (name or '').strip().lower() in admins


def member_ok(h):
    name = urllib.parse.unquote(h.headers.get('X-Member') or '').strip()
    if not name:
        return False
    if is_admin_name(name):
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


# ── 오늘의 말씀 (daily) : 함수 수 제한(12개) 때문에 이 파일에 함께 둠 ──
def today_kst():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).date()


def get_plan():
    try:
        rows = sb('GET', 'settings?key=eq.daily&select=value')
        if rows:
            return rows[0].get('value') or {}
    except Exception:
        pass
    return {}


def compute_today(plan):
    passages = plan.get('passages') or []
    if not passages:
        return None, 0, 0
    idx = 0
    start = plan.get('start')
    if start:
        try:
            sd = datetime.date.fromisoformat(start)
            days = (today_kst() - sd).days
            if days < 0:
                days = 0
            idx = days % len(passages)
        except Exception:
            idx = 0
    return passages[idx], idx, len(passages)


# ── 내 단어장 뜻 사전 (vocab) : 함수 수 제한(12개) 때문에 이 파일에 함께 둠 ──
def vocab_key(name):
    return 'vocab:' + (name or '').strip()[:60]


def get_vocab(name):
    try:
        rows = sb('GET', 'settings?key=eq.' + urllib.parse.quote(vocab_key(name), safe='') + '&select=value')
        if rows:
            v = rows[0].get('value')
            return v if isinstance(v, dict) else {}
    except Exception:
        pass
    return {}


def save_vocab(name, merged):
    key = vocab_key(name)
    existing = sb('GET', 'settings?key=eq.' + urllib.parse.quote(key, safe='') + '&select=key')
    if existing:
        sb('PATCH', 'settings?key=eq.' + urllib.parse.quote(key, safe=''), {'value': merged})
    else:
        sb('POST', 'settings', {'key': key, 'value': merged})


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        # 내 단어장 뜻 사전 조회 (본인만)
        if q.get('vocab'):
            caller = urllib.parse.unquote(self.headers.get('X-Member') or '').strip()
            if not member_ok(self):
                self._send_json({'ok': True, 'vocab': {}})
                return
            self._send_json({'ok': True, 'vocab': get_vocab(caller)})
            return
        # 오늘의 말씀 조회 (공개 — 인증 불필요)
        if q.get('daily'):
            plan = get_plan()
            passage, idx, total = compute_today(plan)
            self._send_json({
                'ok': True, 'passage': passage, 'index': idx, 'total': total,
                'link': plan.get('link') or '', 'text': plan.get('text') or '',
                'passages': plan.get('passages') or [], 'start': plan.get('start') or '',
            })
            return
        # 공지 목록 (로그인 필요)
        if not member_ok(self):
            self._send_json({'error': 'bad_code'}, 401)
            return
        try:
            rows = sb('GET', 'announcements?select=id,title,body,author,active&order=created_at.desc') or []
            self._send_json([r for r in rows if r.get('active')])
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            body = {}
        action = body.get('action', 'add')

        # 내 단어장 뜻 저장 (본인만) — 플래시카드 뒷면에 '원래 뜻'을 보여주기 위함
        if action == 'vocab_add':
            caller = urllib.parse.unquote(self.headers.get('X-Member') or '').strip()
            if not member_ok(self):
                self._send_json({'error': 'bad_code'}, 401)
                return
            try:
                incoming = body.get('words')
                if not isinstance(incoming, dict):
                    incoming = {}
                merged = get_vocab(caller)
                for k, v in list(incoming.items())[:60]:
                    if not isinstance(k, str) or not k.strip():
                        continue
                    if not isinstance(v, dict):
                        continue
                    merged[k.strip()[:60]] = {
                        'ko': str(v.get('ko') or '')[:80],
                        'meaning': str(v.get('meaning') or '')[:400],
                        'nuance': str(v.get('nuance') or '')[:400],
                        'pos': str(v.get('pos') or '')[:20],
                        'passage': str(v.get('passage') or '')[:80],
                    }
                # 너무 커지지 않도록 최근 600개만 유지
                if len(merged) > 600:
                    merged = dict(list(merged.items())[-600:])
                save_vocab(caller, merged)
                self._send_json({'ok': True, 'count': len(merged)})
            except Exception as e:
                self._send_json({'error': str(e)}, 500)
            return

        # 오늘의 말씀 설정 (관리자 전용)
        if action == 'daily_set':
            caller = urllib.parse.unquote(self.headers.get('X-Member') or '').strip()
            if not is_admin_name(caller):
                self._send_json({'error': 'forbidden', 'message': '관리자만 설정할 수 있어요.'}, 403)
                return
            try:
                raw = body.get('passages')
                if isinstance(raw, str):
                    passages = [p.strip() for p in raw.replace('\r', '').split('\n') if p.strip()]
                elif isinstance(raw, list):
                    passages = [str(p).strip() for p in raw if str(p).strip()]
                else:
                    passages = []
                start = (body.get('start') or '').strip() or today_kst().isoformat()
                value = {
                    'passages': passages, 'start': start,
                    'link': (body.get('link') or '').strip()[:500],
                    'text': (body.get('text') or '').strip()[:1000],
                }
                existing = sb('GET', 'settings?key=eq.daily&select=key')
                if existing:
                    sb('PATCH', 'settings?key=eq.daily', {'value': value})
                else:
                    sb('POST', 'settings', {'key': 'daily', 'value': value})
                passage, idx, total = compute_today(value)
                self._send_json({'ok': True, 'passage': passage, 'total': total})
            except Exception as e:
                self._send_json({'error': str(e)}, 500)
            return

        # 공지(오늘의 스페셜) 관리 — 관리자만
        if not member_ok(self):
            self._send_json({'error': 'bad_code', 'message': '승인되지 않은 이름이에요. 관리자에게 문의하세요.'}, 401)
            return
        try:
            author = (body.get('author') or '').strip()
            if author not in ADMINS:
                self._send_json({'error': 'forbidden', 'message': '관리자만 칠판을 쓸 수 있어요.'}, 403)
                return
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
