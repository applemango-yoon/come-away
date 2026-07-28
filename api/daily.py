import json
import os
import datetime
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler


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


def is_admin_name(name):
    admins = [a.strip().lower() for a in os.environ.get('ADMIN_NAMES', '').split(',') if a.strip()]
    return (name or '').strip().lower() in admins


def today_kst():
    # 한국 시간 기준 오늘 날짜 (서버는 UTC)
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
    """관리자가 정한 본문 순서 + 시작일로부터 '하루 한 장'씩 진행. 끝나면 처음부터 반복."""
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


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        # 공개: 오늘의 말씀 + 커뮤니티 초대 정보 (비회원 초대에도 쓰일 수 있게 인증 불필요)
        plan = get_plan()
        passage, idx, total = compute_today(plan)
        self._send_json({
            'ok': True,
            'passage': passage,
            'index': idx,
            'total': total,
            'link': plan.get('link') or '',
            'text': plan.get('text') or '',
            'passages': plan.get('passages') or [],
            'start': plan.get('start') or '',
        })

    def do_POST(self):
        # 관리자만: 오늘의 말씀 계획 설정
        caller = urllib.parse.unquote(self.headers.get('X-Member') or '').strip()
        if not is_admin_name(caller):
            self._send_json({'error': 'forbidden', 'message': '관리자만 설정할 수 있어요.'}, 403)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            raw = body.get('passages')
            if isinstance(raw, str):
                passages = [p.strip() for p in raw.replace('\r', '').split('\n') if p.strip()]
            elif isinstance(raw, list):
                passages = [str(p).strip() for p in raw if str(p).strip()]
            else:
                passages = []
            start = (body.get('start') or '').strip() or today_kst().isoformat()
            value = {
                'passages': passages,
                'start': start,
                'link': (body.get('link') or '').strip()[:500],
                'text': (body.get('text') or '').strip()[:1000],
            }
            existing = sb('GET', 'settings?key=eq.daily&select=key')
            if existing:
                sb('PATCH', 'settings?key=eq.daily', {'value': value},
                   extra_headers={'Prefer': 'return=minimal'})
            else:
                sb('POST', 'settings', {'key': 'daily', 'value': value},
                   extra_headers={'Prefer': 'return=minimal'})
            passage, idx, total = compute_today(value)
            self._send_json({'ok': True, 'passage': passage, 'total': total})
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
