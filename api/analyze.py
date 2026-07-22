import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler


def member_ok(h):
    # 승인된 멤버(또는 관리자)만 허용. 입장 코드 없이 이름으로 인증.
    name = (h.headers.get('X-Member') or '').strip()
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
    # 구글 제미나이 (무료·카드 불필요, Vercel에서 안정적). OpenAI 호환 엔드포인트 사용.
    # 모델명은 시기에 따라 바뀔 수 있어 후보 여러 개를 순서대로 시도 → 하나만 되면 통과.
    if os.environ.get('GEMINI_API_KEY'):
        gurl = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
        gkey = os.environ['GEMINI_API_KEY']
        gmodels = [m] if m else ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-flash-latest', 'gemini-1.5-flash']
        for gm in gmodels:
            out.append(('openai', gurl, gkey, gm))
    if os.environ.get('ANTHROPIC_API_KEY'):
        out.append(('anthropic', 'https://api.anthropic.com/v1/messages',
                    os.environ['ANTHROPIC_API_KEY'],
                    m or 'claude-haiku-4-5'))
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

번역본 4개를 비교해서, 번역본들끼리 서로 "다른 단어/표현을 써서 뉘앙스가 달라지는" 곳만 [H]...[/H] 로 감싸줘.
하이라이트 규칙 (중요):
- 실질적 어휘·표현 차이만 감싼다. 예: NKJV "still waters" vs NASB "quiet waters" → 둘 다 감싸기. 한국어 예: "쉴 만한 물가" vs "잔잔한 물가" → 서로 다른 표현이므로 감싸기.
- 뜻이 같은데 문법만 다른 것은 절대 감싸지 마. 특히 한국어 조사·어미(예: '~은/는', '~이/가', '~을/를', '~에게/한테'), 주어 받침에 따른 조사 차이, 띄어쓰기, 문장부호, 어순 차이는 감싸지 않는다.
- 모든 번역본이 같은 단어를 쓴 곳도 감싸지 마. 예: 둘 다 "shepherd"면 감싸지 않기.
- 한국어 번역본끼리(개역개정 vs 새번역), 영어 번역본끼리(NKJV vs NASB) 각각 비교해서 판단해.
- 애매하면 감싸지 마라. 확실히 "다른 단어를 골라 써서 의미·뉘앙스가 달라지는" 경우에만 감싼다.

{{
  "translations": {{
    "개역개정": "(범위 전체 본문, 다른 표현은 [H]...[/H] 로 감싸기)",
    "새번역": "(범위 전체 본문, 다른 표현은 [H]...[/H] 로 감싸기)",
    "NKJV": "(full text of the whole range, wrap different expressions in [H]...[/H])",
    "NASB": "(full text of the whole range, wrap different expressions in [H]...[/H])"
  }},
  "words": [
    {{
      "korean": "단어 또는 표현구",
      "english": "word or phrase",
      "pos": "품사 (명사/동사/형용사/부사/동사구/명사구/형용사구 중 하나)",
      "meaning": "이 단어의 핵심 한국어 뜻을 중요도 순으로 2~3개, 쉼표로 구분 (예: '사랑, 자비, 긍휼'). 절대 한 개만 쓰지 말 것.",
      "nuance": "영어 뉘앙스와 문맥 속 의미"
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

words는 4~6개이고 원어(헬라어/히브리어)는 절대 넣지 마.
각 단어의 meaning에는 문맥에서 중요한 한국어 뜻을 반드시 2~3개(중요도 순) 담아라. 영어 단어만 적고 뜻을 비우지 마.
originals는 이 본문에서 가장 중요한 원어 딱 3개만.
Strong's 번호는 반드시 정확해야 한다 (블루레터바이블·바이블허브에서 검증 가능해야 하므로).
추측하지 말고 확실한 것만. JSON만 출력.'''


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
            if cached and _valid(cached[0].get('data')):
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

            for k in list(data.get('translations', {}).keys()):
                v = data['translations'][k]
                if isinstance(v, str):
                    data['translations'][k] = mark_highlights(v)

            # 3. 캐시 교체 저장 (예전 깨진 캐시가 있으면 지우고 새로 저장). 실패해도 응답엔 지장 없음.
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
