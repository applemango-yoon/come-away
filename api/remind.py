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
    # MOM 베이커리 테마 아침 묵상 알림 메일
    return f'''
    <div style="margin:0;padding:24px 12px;background:#f6f1e7;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif">
      <div style="max-width:460px;margin:0 auto;background:#fffdf6;border:1px solid #ddd0ba;border-radius:18px;overflow:hidden">
        <div style="background:#cd8a62;padding:18px 20px;text-align:center">
          <div style="font-size:22px;font-weight:800;color:#fffdf6;letter-spacing:1px">🥐 MOM 베이커리 🍯</div>
          <div style="font-size:12px;color:#f5e9c9;margin-top:3px">Miracles on Miracles in Christ · 아침 묵상</div>
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
          이 메일은 MOM 베이커리에 이메일을 등록한 분께 아침마다 보내드려요 🕊️
        </div>
      </div>
    </div>'''


def send_email(to, subject, html):
    payload = json.dumps({
        'from': os.environ.get('REMIND_FROM', 'MOM 베이커리 <onboarding@resend.dev>'),
        'to': [to],
        'subject': subject,
        'html': html
    }).encode()
    req = urllib.request.Request('https://api.resend.com/emails', data=payload, method='POST')
    req.add_header('Authorization', 'Bearer ' + os.environ['RESEND_API_KEY'])
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


PUSH_COL = {'act': 'p_act', 'morning': 'p_morning', 'word': 'p_word', 'plan': 'p_plan'}


def push_subs():
    """알림 구독 목록. 설정 칸이 아직 없는 경우도 견딘다."""
    try:
        return sb('GET', 'push_subscriptions?select=endpoint,name,sub,'
                         'p_act,p_morning,p_word,p_plan') or []
    except Exception:
        try:
            return sb('GET', 'push_subscriptions?select=endpoint,name,sub') or []
        except Exception:
            return []


def push_wants(s, kind):
    col = PUSH_COL.get(kind)
    if not col:
        return True
    v = s.get(col)
    return True if v is None else bool(v)


def send_push_to(names, title, body, site, kind='morning', url=None):
    """웹 푸시 발송 — VAPID 키가 설정돼 있을 때만 동작. 실패해도 조용히 넘어감."""
    pub = os.environ.get('VAPID_PUBLIC_KEY', '')
    priv = os.environ.get('VAPID_PRIVATE_KEY', '')
    if not pub or not priv or not names:
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except Exception:
        return 0
    names = set(names)
    targets = [s for s in push_subs() if s.get('name') in names and push_wants(s, kind)]
    sent = 0
    claims = {'sub': 'mailto:' + os.environ.get('VAPID_SUBJECT', 'admin@mombakery.app')}
    payload = json.dumps({'title': title, 'body': body,
                          'url': (site.rstrip('/') + url) if url else site},
                         ensure_ascii=False)
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

            # 어떤 알림을 보낼 차례인지 정한다.
            #   ?kind=morning|plan|word 로 직접 지정할 수 있고,
            #   지정이 없으면 지금 한국 시간을 보고 고른다.
            #   (버셀 무료 요금제는 예약 시각이 최대 한 시간까지 밀릴 수 있어서
            #    경계를 넉넉히 잡았다: 아침 9시 / 오후 4시 반 / 밤 10시)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            kind = (q.get('kind') or [''])[0]
            if kind not in ('morning', 'plan', 'word'):
                h = datetime.now(KST).hour
                kind = 'morning' if h < 12 else ('plan' if h < 20 else 'word')
            if kind in ('plan', 'word'):
                self._send_json(self._later(today, site, kind))
                return

            members = sb('GET', 'members?select=name,email') or []
            done_rows = sb('GET', f'entries?select=author&date=eq.{urllib.parse.quote(today)}') or []
            done = {r.get('author') for r in done_rows}

            missing = [m for m in members if m['name'] not in done]
            missing_names = [m['name'] for m in missing]
            sent, skipped = [], []

            # 1) 웹 푸시 — 아직 묵상 안 한 멤버 중 알림 켠 사람에게 (이메일 없어도 감)
            pushed = send_push_to(
                missing_names,
                '🥐 오늘의 빵이 따끈해요',
                '잠시 말씀 앞에 머무는 아침 묵상 시간을 가져볼까요?',
                site, kind='morning')

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

    # ── 오후·밤: 액션 플랜 되새김 / 영단어 복습 ────────────────────
    def _later(self, today, site, kind):
        d = datetime.strptime(today, '%Y-%m-%d')
        d3 = (d - timedelta(days=3)).strftime('%Y-%m-%d')
        d7 = (d - timedelta(days=7)).strftime('%Y-%m-%d')

        def rows_on(day):
            return sb('GET', 'entries?date=eq.' + urllib.parse.quote(day)
                      + '&select=author,date,action,words') or []

        sent = 0
        if kind == 'word':
            # 영단어 복습 — 3일 전에 담아 둔 단어, 그날이 비었으면 일주일 전 것
            words_by = {}
            for day, label in ((d3, '3일 전'), (d7, '일주일 전')):
                for r in rows_on(day):
                    who = (r.get('author') or '').strip()
                    ws = [str(w).strip() for w in (r.get('words') or []) if str(w).strip()]
                    if who and ws and who not in words_by:
                        words_by[who] = (label, ws[:5])
            for who, (label, ws) in words_by.items():
                sent += send_push_to(
                    [who], '🔑 단어 복습 시간이에요',
                    '%s 담아 둔 단어예요 — %s' % (label, ', '.join(ws)),
                    site, kind='word', url='/')
        else:
            # 액션 플랜 — 오늘 적어 둔 다짐을 오후에 다시 보여 준다
            for r in rows_on(today):
                who = (r.get('author') or '').strip()
                act = (r.get('action') or '').strip()
                if not who or not act:
                    continue
                if len(act) > 60:
                    act = act[:59] + '…'
                sent += send_push_to(
                    [who], '✍️ 오늘의 액션 플랜, 기억하고 계세요?',
                    '“%s”' % act, site, kind='plan', url='/')

        return {'ok': True, 'kind': kind, 'date': today, 'pushed': sent}

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
