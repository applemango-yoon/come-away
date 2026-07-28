import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
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


def mark_highlights(text):
    return re.sub(r'\[H\](.*?)\[/H\]', r'<span class="hl">\1</span>', text)


def sb(method, path, data=None, silent=False):
    try:
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
    except Exception:
        if silent:
            return None
        raise


def normalize_key(passage):
    """'요한복음 3:16', '요한복음3:16', ' 요한복음  3:16 ' 등을 모두 같은 캐시 키로 정규화.
    공백을 전부 제거해 띄어쓰기 유무와 관계없이 같은 본문으로 취급한다."""
    return re.sub(r'\s+', '', passage.strip().lower())


def providers():
    """시도할 AI 공급자 목록(우선순위 순). 하나가 실패하면 다음 것을 시도한다.
    Anthropic(클로드)을 먼저 두는 이유: Groq은 클라우드/지역 차단으로 403이 날 수 있고,
    Anthropic은 Vercel에서 안정적으로 동작하기 때문.
    각 항목: (형식, URL, 키, 모델). 형식은 'anthropic' 또는 'openai'(OpenAI 호환).
    """
    out = []
    m = os.environ.get('AI_MODEL')  # 지정 시 모든 공급자에 우선 적용
    if os.environ.get('AI_API_URL'):
        out.append(('openai', os.environ['AI_API_URL'],
                    os.environ.get('AI_API_KEY', ''),
                    m or 'meta-llama/llama-3.3-70b-instruct'))
    # 클로드(Anthropic) — 결제된 계정이라 속도 제한이 넉넉해 동시 접속에 강함. 최우선.
    # 기본 모델은 빠른 haiku. 더 깊은 품질을 원하면 AI_MODEL에 claude-sonnet-4-6 등을 지정.
    if os.environ.get('ANTHROPIC_API_KEY'):
        out.append(('anthropic', 'https://api.anthropic.com/v1/messages',
                    os.environ['ANTHROPIC_API_KEY'],
                    m or 'claude-haiku-4-5'))
    # 구글 제미나이 — 백업(무료). OpenAI 호환 엔드포인트. 모델명이 바뀔 수 있어 후보 여러 개 시도.
    if os.environ.get('GEMINI_API_KEY'):
        gurl = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
        gkey = os.environ['GEMINI_API_KEY']
        gmodels = [m] if m else ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-flash-latest', 'gemini-1.5-flash']
        for gm in gmodels:
            out.append(('openai', gurl, gkey, gm))
    if os.environ.get('OPENROUTER_API_KEY'):
        out.append(('openai', 'https://openrouter.ai/api/v1/chat/completions',
                    os.environ['OPENROUTER_API_KEY'],
                    m or 'meta-llama/llama-3.3-70b-instruct:free'))
    if os.environ.get('AI_GATEWAY_API_KEY'):
        out.append(('openai', 'https://ai-gateway.vercel.sh/v1/chat/completions',
                    os.environ['AI_GATEWAY_API_KEY'],
                    m or 'openai/gpt-4o-mini'))
    if os.environ.get('GROQ_API_KEY'):
        out.append(('openai', 'https://api.groq.com/openai/v1/chat/completions',
                    os.environ['GROQ_API_KEY'],
                    m or 'llama-3.3-70b-versatile'))
    if not out:
        raise RuntimeError('AI API 키가 설정되지 않았어요. ANTHROPIC_API_KEY 또는 GROQ_API_KEY를 Vercel 환경변수에 추가해 주세요.')
    return out


PROMPT = '''성경 본문 "{passage}"를 분석해서 아래 JSON 형식으로만 응답해줘. 코드블록 없이 JSON만.

본문이 여러 절 범위(예: 시편 23:1-3)라면 그 범위의 모든 절을 포함해서 분석해줘.

번역본 4개를 비교해서, 번역본끼리 "같은 뜻인데 서로 다른 단어·표현으로 옮긴 곳"만 [H]...[/H] 로 감싸라.
하이라이트 핵심 원리 (반드시 지켜라):
- 목적: 번역본마다 갈리는 "표현의 차이"를 눈에 띄게 하는 것이다. 그러니 여러 번역본이 "같은 의미를 서로 다르게 표현한" 부분만 감싼다.
- (감싼다) 같은 뜻을 다른 단어로: NKJV "still waters" vs NASB "quiet waters", "everlasting" vs "eternal", 한국어 "쉴 만한 물가" vs "잔잔한 물가" → 양쪽 다 감싸기.
- (감싼다) 같은 뜻을 다른 동사구·표현·구문으로: 예 "lay down his life" vs "give up his life", 한국어 "멸망하지 않고" vs "죽지 않고" → 양쪽 다 감싸기. 단어뿐 아니라 표현·동사구도 반드시 대상이다.
- (절대 감싸지 마) 모든 번역본이 "똑같은 단어"를 쓴 곳. 예: 넷 다 "shepherd/목자"면 감싸지 않는다. ★같은 단어를 감싸는 것은 틀린 것이다.★
- (절대 감싸지 마) 문법 차이만 다른 것: 조사·어미('~은/는', '~이/가', '~을/를'), 어순, 띄어쓰기, 문장부호.
- 비교는 언어쌍 안에서: 한국어(개역개정 vs 새번역)끼리, 영어(NKJV vs NASB)끼리 비교해 서로 다른 표현을 찾아 감싼다.
- 한 표현을 감쌀 때는 짝이 되는 다른 번역본의 대응 표현도 함께 감싼다(양쪽 다 감싸야 "무엇이 어떻게 다른지" 비교가 보인다).

{{
  "translations": {{
    "개역개정": "(범위 전체 본문, 다른 표현은 [H]...[/H] 로 감싸기)",
    "새번역": "(범위 전체 본문, 다른 표현은 [H]...[/H] 로 감싸기)",
    "NKJV": "(full text of the whole range, wrap different expressions in [H]...[/H])",
    "NASB": "(full text of the whole range, wrap different expressions in [H]...[/H])"
  }},
  "words": [
    {{
      "english": "영어 단어 또는 짧은 구 (이 항목의 표제 — 예: eternal, everlasting, perish, believe, lay down)",
      "korean": "대응하는 한국어 낱말 (짧게, 예: 영원한)",
      "pos": "품사 (명사/동사/형용사/부사/동사구/명사구/형용사구 중 하나)",
      "meaning": "이 '영어 단어'의 핵심 한국어 뜻 2~3개를 중요도 순으로, 쉼표로 구분 (예: '영원한, 끝없는, 시간을 초월한'). 한 개만 쓰지 말 것.",
      "nuance": "이 영어 단어가 문맥에서 갖는 뉘앙스와 함의를 한국어로"
    }}
  ],
  "originals": [
    {{
      "strong": "Strong's 번호 (히브리어면 H로 시작 예:H7462, 헬라어면 G로 시작 예:G26)",
      "original": "헬라어/히브리어 원어",
      "reading": "음역 (예: 로이)",
      "korean": "해당하는 한국어 단어",
      "meaning": "Strong's 사전·표준 어휘에 근거한 뜻과 이 본문에서 중요한 이유, 1~2문장"
    }}
  ],
  "background": "이 본문의 역사적·문학적 배경 한 가지, 표준 주석에 근거해 2문장"
}}

words 규칙 (반드시 지켜라):
- ★표제(english)는 이 본문의 영어 본문(NKJV 또는 NASB)에 "실제로 등장하는" 영어 단어/짧은 구여야 한다.★ 본문에 없는 단어를 지어내면 절대 안 된다. (한국어 문장 조각 '믿는 자'·'멸망하지 않고' 같은 것도 표제로 쓰지 마라 → 그 속의 실제 영어 단어 believe, perish 를 써라.)
- 이 본문에서 "가장 중요하고 어려운(뜻·뉘앙스가 깊은)" 단어/동사구를 골라라. 쉽고 뻔한 단어(the, and, God, is 등)는 넣지 마라.
- 개수: 최대 10개. 중요한 단어가 부족하면 억지로 10개를 채우지 말고 있는 만큼만(예: 4개·6개·8개). 질이 개수보다 우선이다.
- 번역본마다 서로 다른 영어 단어를 쓴 곳은 각각 따로 넣어도 된다(everlasting, eternal 등). 각 단어의 미묘한 차이를 뉘앙스에 설명하라.
- korean 필드에는 그 영어 단어에 대응하는 짧은 한국어 낱말을 넣어라(예: eternal→영원한). meaning에는 한국어 뜻 2~3개(중요도 순)를 담아라.
- 원어(헬라어/히브리어)는 words에 절대 넣지 마 (그건 originals에서).
originals는 이 본문에서 가장 중요한 원어 딱 3개만.
Strong's 번호는 반드시 정확해야 한다 (블루레터바이블·바이블허브에서 검증 가능해야 하므로).
추측하지 말고 확실한 것만. JSON만 출력.'''


SCHEMA_VER = 5   # 분석 결과 형식 버전. 올리면 이전 캐시를 자동으로 무시하고 다시 분석함.


def _valid(d):
    """분석 결과가 화면에 그릴 만큼 온전한지 (translations 딕셔너리가 있는지)."""
    return isinstance(d, dict) and isinstance(d.get('translations'), dict) and len(d.get('translations')) > 0


def extract_json(text):
    """모델 응답에서 JSON 객체를 최대한 안전하게 추출"""
    text = text.strip()
    if '```' in text:
        m = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
        if m:
            text = m.group(1).strip()
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1:
        raise ValueError('응답에서 JSON을 찾을 수 없음')
    return json.loads(text[start:end + 1])


def _post_json(url, headers, payload):
    """POST 후 JSON 반환. HTTP 오류면 서버가 준 실제 본문을 메시지에 담아 올림."""
    req = urllib.request.Request(url, data=payload, method='POST')
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=55) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'ignore')[:300]
        except Exception:
            detail = e.reason or ''
        raise RuntimeError('HTTP %s %s' % (e.code, detail))


def call_ai(passage, strict=False):
    content = PROMPT.format(passage=passage)
    if strict:
        content += '\n\n반드시 유효한 JSON 객체 하나만, 다른 텍스트 없이 출력해.'
    errors = []
    for kind, url, key, model in providers():
        try:
            if kind == 'anthropic':
                payload = json.dumps({
                    'model': model,
                    'max_tokens': 8000,   # 여러 절 범위도 안 잘리게 넉넉히 (실제 쓴 만큼만 과금)
                    'temperature': 0.3,
                    'messages': [{'role': 'user', 'content': content}]
                }).encode()
                headers = {'x-api-key': key,
                           'anthropic-version': '2023-06-01',
                           'Content-Type': 'application/json'}
                result = _post_json(url, headers, payload)
                text = result['content'][0]['text']
            else:  # openai 호환 (Groq / OpenRouter / Vercel Gateway 등)
                payload = json.dumps({
                    'model': model,
                    'messages': [{'role': 'user', 'content': content}],
                    'max_tokens': 8000,
                    'temperature': 0.3
                }).encode()
                headers = {'Authorization': 'Bearer ' + key,
                           'Content-Type': 'application/json'}
                result = _post_json(url, headers, payload)
                text = result['choices'][0]['message']['content']
            return extract_json(text)
        except (ValueError, json.JSONDecodeError):
            raise  # 응답은 왔는데 JSON 형식 문제 → 상위에서 strict로 재시도
        except Exception as e:
            errors.append('%s %s' % (kind, e))
            continue  # 이 공급자 실패 → 다음 공급자 시도
    raise RuntimeError(' / '.join(errors) if errors else 'AI 호출에 실패했어요.')


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        if not member_ok(self):
            self._send_json({'error': 'bad_code', 'message': '승인되지 않은 이름이에요. 관리자에게 문의하세요.'}, 401)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            passage = body.get('passage', '').strip()
            if not passage:
                self._send_json({'error': 'empty', 'message': '본문을 입력해 주세요.'}, 400)
                return

            key = normalize_key(passage)
            qkey = urllib.parse.quote(key)

            # 1. 캐시 확인 — 단, 온전한(translations 있는) 결과만 사용. 예전에 저장된 깨진 캐시는 무시.
            cached = sb('GET', 'analyses?passage_key=eq.' + qkey + '&select=data', silent=True)
            if cached and _valid(cached[0].get('data')) and cached[0]['data'].get('v') == SCHEMA_VER:
                data = cached[0]['data']
                data['cached'] = True
                self._send_json(data)
                return

            # 2. AI 호출 (JSON 파싱/형식 실패 시 strict 모드로 1회 재시도)
            try:
                data = call_ai(passage)
                if not _valid(data):
                    data = call_ai(passage, strict=True)
            except (ValueError, json.JSONDecodeError):
                data = call_ai(passage, strict=True)

            # 결과가 여전히 온전치 않으면 깨진 데이터를 저장/반환하지 않고 명확히 알린다.
            if not _valid(data):
                self._send_json({'error': 'bad_ai',
                                 'message': '분석 결과를 온전히 받지 못했어요. 잠시 후 다시 시도해 주세요.'}, 502)
                return

            # 단어는 최대 10개까지만 (AI가 더 줘도 잘라냄; 부족하면 있는 만큼)
            if isinstance(data.get('words'), list):
                data['words'] = data['words'][:10]

            for k in list(data.get('translations', {}).keys()):
                v = data['translations'][k]
                if isinstance(v, str):
                    data['translations'][k] = mark_highlights(v)

            data['v'] = SCHEMA_VER   # 형식 버전 기록 (옛 캐시 자동 무효화용)

            # 3. 캐시 교체 저장 (예전/깨진 캐시가 있으면 지우고 새로 저장). 실패해도 응답엔 지장 없음.
            sb('DELETE', 'analyses?passage_key=eq.' + qkey, silent=True)
            sb('POST', 'analyses', {'passage_key': key, 'passage': passage, 'data': data}, silent=True)

            data['cached'] = False
            self._send_json(data)
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
