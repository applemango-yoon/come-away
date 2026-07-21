import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

KST = timezone(timedelta(hours=9))


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


def build_email(name, done_names, site):
    # 오병이어 상점 테마 아침 묵상 알림 메일
    return f'''
    <div style="margin:0;padding:24px 12px;background:#f6f1e7;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif">
      <div style="max-width:460px;margin:0 auto;background:#fffdf6;border:1px solid #ddd0ba;border-radius:18px;overflow:hidden">
        <div style="background:#cd8a62;padding:18px 20px;text-align:center">
          <div style="font-size:22px;font-weight:800;color:#fffdf6;letter-spacing:1px">🐟 Come Away 🍞</div>
          <div style="font-size:12px;color:#f5e9c9;margin-top:3px">작은 오병이어 상점 · 아침 묵상</div>
        </div>
        <div style="padding:26px 24px">
          <div style="font-size:17px;font-weight:800;color:#4c4237;margin-bottom:10px">🌅 {name}님, 좋은 아침이에요!</div>
          <p style="font-size:14px;color:#75675a;line-height:1.75;margin:0 0 16px">
            따끈한 말씀 빵과 커피 한 잔, 오늘도 주님은 참 좋으신 분이에요 ☕<br>
            잠깐 마주 앉아볼까요? 오늘 함께한 멤버: <b style="color:#a96b47">{done_names}</b>
          </p>
          <div style="background:#f5e9c9;border-radius:12px;padding:12px 16px;font-size:13px;color:#66795a;line-height:1.6;margin-bottom:20px">
            "How good and pleasant it is when God's people dwell together in unity!"<br>— Psalm 133:1</div>
          <a href="{site}" style="display:block;background:#8aa07a;color:#fffdf6;padding:14px;text-decoration:none;font-weight:800;font-size:15px;text-align:center;border-radius:12px">☀️ 주님과 마주 앉으러 가기</a>
        </div>
        <div style="background:#f6f1e7;padding:12px;text-align:center;font-size:11px;color:#b6a88f">
          이 메일은 Come Away에 이메일을 등록한 분께 아침마다 보내드려요 🕊️
        </div>
      </div>
    </div>'''


def send_email(to, subject, html):
    payload = json.dumps({
        'from': os.environ.get('REMIND_FROM', 'Come Away <onboarding@resend.dev>'),
        'to': [to],
        'subject': subject,
        'html': html
    }).encode()
    req = urllib.request.Request('https://api.resend.com/emails', data=payload, method='POST')
    req.add_header('Authorization', 'Bearer ' + os.environ['RESEND_API_KEY'])
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def send_push_to(names, title, body, site):
    """웹 푸시 발송 — VAPID 키가 설정돼 있을 때만 동작. 실패해도 조용히 넘어감."""
    pub = os.environ.get('VAPID_PUBLIC_KEY', '')
    priv = os.environ.get('VAPID_PRIVATE_KEY', '')
    if not pub or not priv:
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except Exception:
        return 0
    subs = sb('GET', 'push_subscriptions?select=endpoint,name,sub') or []
    targets = [s for s in subs if s['name'] in names]
    sent = 0
    claims = {'sub': 'mailto:' + os.environ.get('VAPID_SUBJECT', 'admin@come-away.app')}
    payload = json.dumps({'title': title, 'body': body, 'url': site})
    for s in targets:
        try:
            webpush(subscription_info=s['sub'], data=payload,
                    vapid_private_key=priv, vapid_claims=dict(claims))
            sent += 1
        except WebPushException as e:
            # 만료된 구독(410/404)은 정리
            if getattr(e, 'response', None) is not None and e.response.status_code in (404, 410):
                try:
                    sb('DELETE', 'push_subscriptions?endpoint=eq.' + urllib.parse.quote(s['endpoint']))
                except Exception:
                    pass
        except Exception:
            pass
    return sent


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        try:
            # Vercel Cron 인증: CRON_SECRET 환경변수를 설정하면
            # Vercel이 Authorization: Bearer <CRON_SECRET> 헤더를 자동으로 붙여서 호출함
            secret = os.environ.get('CRON_SECRET', '')
            if secret and self.headers.get('Authorization', '') != f'Bearer {secret}':
                self._send_json({'error': 'unauthorized'}, 401)
                return

            today = datetime.now(KST).strftime('%Y-%m-%d')
            site = os.environ.get('SITE_URL', 'https://come-away-xi.vercel.app')

            members = sb('GET', 'members?select=name,email') or []
            done_rows = sb('GET', f'entries?select=author&date=eq.{urllib.parse.quote(today)}') or []
            done = {r.get('author') for r in done_rows}

            missing = [m for m in members if m['name'] not in done]
            missing_names = [m['name'] for m in missing]
            sent, skipped = [], []

            # 1) 웹 푸시 — 아직 묵상 안 한 멤버 중 알림 켠 사람에게 (이메일 없어도 감)
            pushed = send_push_to(
                missing_names,
                '🍞 오늘의 빵이 따끈해요',
                '잠시 말씀 앞에 머무는 아침 묵상 시간을 가져볼까요?',
                site)

            # 2) 이메일 — 이메일 등록한 멤버에게
            if os.environ.get('RESEND_API_KEY'):
                for m in missing:
                    if not m.get('email'):
                        skipped.append(m['name'])
                        continue
                    done_names = ' · '.join(sorted(done)) if done else '아직 없어요'
                    html = build_email(m['name'], done_names, site)
                    try:
                        send_email(m['email'], f'🍞 {m["name"]}님, 오늘의 빵이 따끈해요 — 아침 묵상 시간이에요', html)
                        sent.append(m['name'])
                    except Exception:
                        skipped.append(m['name'])

            self._send_json({'ok': True, 'date': today, 'emailed': sent, 'pushed': pushed,
                             'skipped_email': skipped, 'missing': missing_names,
                             'already_done': sorted(done)})
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
