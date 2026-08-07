import json
import os
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


# ── 웹 푸시 알림 ────────────────────────────────────────────────
# VAPID 키가 환경변수에 있을 때만 동작한다. 실패해도 본래 작업은 그대로 성공시킨다.
def push_to(names, title, message, url='/'):
    pub = os.environ.get('VAPID_PUBLIC_KEY', '')
    priv = os.environ.get('VAPID_PRIVATE_KEY', '')
    if not pub or not priv or not names:
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except Exception:
        return 0
    site = os.environ.get('SITE_URL', 'https://come-away-xi.vercel.app')
    try:
        subs = sb('GET', 'push_subscriptions?select=endpoint,name,sub') or []
    except Exception:
        return 0
    want = set(names)
    payload = json.dumps({'title': title, 'body': message,
                          'url': site.rstrip('/') + url}, ensure_ascii=False)
    claims = {'sub': 'mailto:' + os.environ.get('VAPID_SUBJECT', 'admin@mombakery.app')}
    sent = 0
    for s in subs:
        if s.get('name') not in want:
            continue
        try:
            webpush(subscription_info=s['sub'], data=payload,
                    vapid_private_key=priv, vapid_claims=dict(claims))
            sent += 1
        except WebPushException as e:
            # 만료된 구독(404/410)은 지운다
            r = getattr(e, 'response', None)
            if r is not None and getattr(r, 'status_code', 0) in (404, 410):
                try:
                    sb('DELETE', 'push_subscriptions?endpoint=eq.'
                       + urllib.parse.quote(s['endpoint'], safe=''))
                except Exception:
                    pass
        except Exception:
            pass
    return sent


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if not member_ok(self):
            self._send_json({'error': 'bad_code', 'message': '승인되지 않은 이름이에요. 관리자에게 문의하세요.'}, 401)
            return
        try:
            rows = sb('GET', 'comments?select=id,entry_id,author,text&order=created_at.asc') or []
            self._send_json(rows)
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def do_POST(self):
        if not member_ok(self):
            self._send_json({'error': 'bad_code', 'message': '승인되지 않은 이름이에요. 관리자에게 문의하세요.'}, 401)
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
                try:                                  # 묵상 주인에게 알림 (내 글에 내가 달면 보내지 않음)
                    owner = sb('GET', 'entries?id=eq.'
                               + urllib.parse.quote(str(entry_id), safe='') + '&select=author') or []
                    who = (owner[0].get('author') or '').strip() if owner else ''
                    if who and who != author:
                        push_to([who], '\U0001f4ac 새 댓글이 달렸어요',
                                '%s님이 당신의 묵상에 댓글을 남겼어요.' % author)
                except Exception:
                    pass
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
