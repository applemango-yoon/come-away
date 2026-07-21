import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

KST = timezone(timedelta(hours=9))

# 물고기(🐟)는 관리자만 나눌 수 있어요. Vercel 환경변수 ADMIN_NAMES="망고" (쉼표로 여러 명)
ADMINS = [a.strip() for a in os.environ.get('ADMIN_NAMES', '').split(',') if a.strip()]

FISH_VALUE = 2  # 2병 = 1어


def code_ok(h):
    # 입장 코드: Vercel 환경변수 ENTRY_CODE 설정 시에만 검사
    code = os.environ.get('ENTRY_CODE', '').strip()
    return (not code) or h.headers.get('X-Entry-Code', '').strip() == code


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


def today_kst():
    return datetime.now(KST).strftime('%Y-%m-%d')


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if not code_ok(self):
            self._send_json({'error': 'bad_code', 'message': '입장 코드가 올바르지 않아요.'}, 401)
            return
        # 바구니 잔액(병 단위)·오늘 나눈 사람·최근 내역
        try:
            shells = sb('GET', 'shells?select=date,from_name,to_name,item,note&order=created_at.desc') or []
            uses = sb('GET', 'shell_uses?select=name,amount,memo&order=created_at.desc') or []
            balances = {}
            for s in shells:
                b = balances.setdefault(s['to_name'], {'fish': 0, 'bread': 0, 'used': 0})
                if s.get('item') == '🐟':
                    b['fish'] += 1
                else:
                    b['bread'] += 1
            for u in uses:
                b = balances.setdefault(u['name'], {'fish': 0, 'bread': 0, 'used': 0})
                b['used'] += u['amount']
            today = today_kst()
            out = {
                'balances': {k: {'fish': v['fish'], 'bread': v['bread'], 'used': v['used'],
                                 'balance': v['bread'] + v['fish'] * FISH_VALUE - v['used']}
                             for k, v in balances.items()},
                'sent_today': [s['from_name'] for s in shells if s['date'] == today and s.get('item') != '🐟'],
                'received_today': [s['to_name'] for s in shells if s['date'] == today],
                'admins': ADMINS,
                'fish_value': FISH_VALUE,
                'recent': shells[:8],
                'recent_uses': uses[:5],
            }
            self._send_json(out)
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def do_POST(self):
        if not code_ok(self):
            self._send_json({'error': 'bad_code', 'message': '입장 코드가 올바르지 않아요.'}, 401)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            action = body.get('action', 'send')

            if action == 'send':
                frm = (body.get('from') or '').strip()[:30]
                to = (body.get('to') or '').strip()[:30]
                item = body.get('item') if body.get('item') in ('🐟', '🍞') else '🍞'
                note = (body.get('note') or '').strip()[:100]
                if not frm or not to:
                    self._send_json({'error': 'bad_request', 'message': '보내는 사람/받는 사람이 필요해요.'}, 400)
                    return
                if frm == to:
                    self._send_json({'error': 'self', 'message': '자기 자신에게는 나눌 수 없어요 🙂'}, 400)
                    return
                is_admin = frm in ADMINS
                if item == '🐟' and not is_admin:
                    self._send_json({'error': 'fish_admin_only',
                                     'message': '물고기는 관리자만 나눌 수 있어요 🐟'}, 403)
                    return
                # 오늘의 병은 하루 1개 — 나누지 않으면 사라져요 (관리자의 물고기는 제한 없음)
                if item == '🍞':
                    bread_q = urllib.parse.quote('🍞')
                    today_sent = sb('GET', f"shells?from_name=eq.{urllib.parse.quote(frm)}"
                                           f"&date=eq.{urllib.parse.quote(today_kst())}&item=eq.{bread_q}&select=id") or []
                    if today_sent:
                        self._send_json({'error': 'daily_limit',
                                         'message': '오늘의 빵은 이미 나눴어요. 내일 새 빵이 나와요 🍞'}, 409)
                        return
                sb('POST', 'shells', {'date': today_kst(), 'from_name': frm, 'to_name': to,
                                      'item': item, 'note': note})
                self._send_json({'ok': True})

            elif action == 'event_grant':
                # 관리자가 이벤트/챌린지로 물고기를 여러 명에게 한 번에 지급
                frm = (body.get('from') or '').strip()[:30]
                if frm not in ADMINS:
                    self._send_json({'error': 'fish_admin_only', 'message': '물고기는 관리자만 줄 수 있어요 🐟'}, 403)
                    return
                tos = body.get('to') or []
                amount = int(body.get('amount') or 1)
                note = (body.get('note') or '이벤트 선물')[:100]
                if not isinstance(tos, list) or not tos or amount < 1:
                    self._send_json({'error': 'bad_request', 'message': '받을 멤버와 개수를 확인해 주세요.'}, 400)
                    return
                granted = 0
                for name in tos:
                    name = str(name).strip()[:30]
                    if not name:
                        continue
                    for _ in range(amount):
                        sb('POST', 'shells', {'date': today_kst(), 'from_name': frm,
                                              'to_name': name, 'item': '🐟', 'note': note})
                        granted += 1
                self._send_json({'ok': True, 'granted': granted})

            elif action == 'use':
                name = (body.get('name') or '').strip()[:30]
                amount = int(body.get('amount') or 0)  # 병 단위
                memo = (body.get('memo') or '').strip()[:100]
                if not name or amount <= 0:
                    self._send_json({'error': 'bad_request', 'message': '사용할 개수(빵 기준)를 입력해 주세요.'}, 400)
                    return
                rows = sb('GET', f'shells?to_name=eq.{urllib.parse.quote(name)}&select=item') or []
                received = sum(FISH_VALUE if r.get('item') == '🐟' else 1 for r in rows)
                used = sum(u['amount'] for u in (sb('GET', f'shell_uses?name=eq.{urllib.parse.quote(name)}&select=amount') or []))
                if received - used < amount:
                    self._send_json({'error': 'insufficient',
                                     'message': f'바구니가 부족해요. 현재 빵 {received - used}개 어치 🧺'}, 400)
                    return
                sb('POST', 'shell_uses', {'name': name, 'amount': amount, 'memo': memo})
                self._send_json({'ok': True, 'balance': received - used - amount})

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
