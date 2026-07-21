import json
import os
import re
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler


def code_ok(h):
    # 입장 코드: Vercel 환경변수 ENTRY_CODE 설정 시에만 검사
    code = os.environ.get('ENTRY_CODE', '').strip()
    return (not code) or h.headers.get('X-Entry-Code', '').strip() == code


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
    """'요한복음 3:16', ' 요한복음  3:16 ' 등을 같은 캐시 키로 정규화"""
    return re.sub(r'\s+', ' ', passage.strip().lower())


def resolve_provider():
    """환경변수로 AI 공급자 결정.
    우선순위: AI_API_URL 직접 지정 > OPENROUTER_API_KEY > AI_GATEWAY_API_KEY(Vercel) > GROQ_API_KEY
    """
    if os.environ.get('AI_API_URL'):
        return (os.environ['AI_API_URL'],
                os.environ.get('AI_API_KEY', ''),
                os.environ.get('AI_MODEL', 'meta-llama/llama-3.3-70b-instruct'))
    if os.environ.get('OPENROUTER_API_KEY'):
        return ('https://openrouter.ai/api/v1/chat/completions',
                os.environ['OPENROUTER_API_KEY'],
                os.environ.get('AI_MODEL', 'meta-llama/llama-3.3-70b-instruct:free'))
    if os.environ.get('AI_GATEWAY_API_KEY'):
        return ('https://ai-gateway.vercel.sh/v1/chat/completions',
                os.environ['AI_GATEWAY_API_KEY'],
                os.environ.get('AI_MODEL', 'openai/gpt-4o-mini'))
    if os.environ.get('GROQ_API_KEY'):
        return ('https://api.groq.com/openai/v1/chat/completions',
                os.environ['GROQ_API_KEY'],
                os.environ.get('AI_MODEL', 'llama-3.3-70b-versatile'))
    raise RuntimeError('AI API 키가 설정되지 않았어요. OPENROUTER_API_KEY 또는 AI_GATEWAY_API_KEY를 Vercel 환경변수에 추가해 주세요.')


PROMPT = '''성경 본문 "{passage}"를 분석해서 아래 JSON 형식으로만 응답해줘. 코드블록 없이 JSON만.

본문이 여러 절 범위(예: 시편 23:1-3)라면 그 범위의 모든 절을 포함해서 분석해줘.

번역본 4개를 비교해서, 번역본들끼리 서로 "다르게" 표현한 단어나 구절만 [H]...[/H] 로 감싸줘.
하이라이트 규칙 (중요):
- 번역본마다 표현이 갈리는 곳만 감싸. 예: NKJV가 "still waters", NASB가 "quiet waters"라면 둘 다 감싸기.
- 모든 번역본이 똑같이 쓴 단어는 절대 감싸지 마. 예: NKJV와 NASB 둘 다 "shepherd"라면 감싸지 않기.
- 한국어 번역본끼리(개역개정 vs 새번역), 영어 번역본끼리(NKJV vs NASB) 각각 비교해서 판단해.

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
      "meaning": "한국어 의미",
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
originals는 이 본문에서 가장 중요한 원어 딱 3개만.
Strong's 번호는 반드시 정확해야 한다 (블루레터바이블·바이블허브에서 검증 가능해야 하므로).
추측하지 말고 확실한 것만. JSON만 출력.'''


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


def call_ai(passage, strict=False):
    url, key, model = resolve_provider()
    content = PROMPT.format(passage=passage)
    if strict:
        content += '\n\n반드시 유효한 JSON 객체 하나만, 다른 텍스트 없이 출력해.'
    payload = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': content}],
        'max_tokens': 2048,
        'temperature': 0.3
    }).encode()
    req = urllib.request.Request(url, data=payload, method='POST')
    req.add_header('Authorization', 'Bearer ' + key)
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=55) as r:
        result = json.loads(r.read())
    return extract_json(result['choices'][0]['message']['content'])


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        if not code_ok(self):
            self._send_json({'error': 'bad_code', 'message': '입장 코드가 올바르지 않아요.'}, 401)
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            passage = body.get('passage', '').strip()
            if not passage:
                self._send_json({'error': 'empty', 'message': '본문을 입력해 주세요.'}, 400)
                return

            key = normalize_key(passage)

            # 1. 캐시 확인 — 같은 본문은 AI를 다시 호출하지 않음
            cached = sb('GET', 'analyses?passage_key=eq.' + urllib.parse.quote(key) + '&select=data', silent=True)
            if cached:
                data = cached[0]['data']
                data['cached'] = True
                self._send_json(data)
                return

            # 2. AI 호출 (JSON 파싱 실패 시 1회 재시도)
            try:
                data = call_ai(passage)
            except (ValueError, json.JSONDecodeError):
                data = call_ai(passage, strict=True)

            for k in data.get('translations', {}):
                data['translations'][k] = mark_highlights(data['translations'][k])

            # 3. 캐시에 저장 (실패해도 응답에는 지장 없음)
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
