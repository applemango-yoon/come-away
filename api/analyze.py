import json
import os
import re
import urllib.request
from http.server import BaseHTTPRequestHandler

def mark_highlights(text):
    return re.sub(r'\[H\](.*?)\[/H\]', r'<span class="hl">\1</span>', text)

PROMPT = '''성경 본문 "{passage}"를 분석해서 아래 JSON 형식으로만 응답해줘. 코드블록 없이 JSON만.

번역본 4개를 비교해서, 서로 다르게 표현된 단어나 구절은 [H]단어[/H] 로 감싸줘.
같은 뜻이지만 단어 선택이나 표현이 다른 부분이 핵심이야. 4개 중 하나라도 다르면 표시.

{{
  "translations": {{
    "개역개정": "(본문 전체, 다른 표현은 [H]...[/H] 로 감싸기)",
    "새번역": "(본문 전체, 다른 표현은 [H]...[/H] 로 감싸기)",
    "NKJV": "(full text, different expressions wrapped in [H]...[/H])",
    "NASB": "(full text, different expressions wrapped in [H]...[/H])"
  }},
  "words": [
    {{
      "korean": "단어 또는 숙어/표현구",
      "english": "word or phrase",
      "pos": "품사 (명사/동사/형용사/부사/동사구/명사구/형용사구 중 하나, 한국어로)",
      "original": "헬라어/히브리어 (단어인 경우만, 표현구면 빈 문자열)",
      "meaning": "원어 뜻 또는 표현의 한국어 의미",
      "nuance": "영어 뉘앙스와 문맥 속 의미. 수능/중등 수준으로"
    }}
  ],
  "background": "저자·시대·문맥 중 이 본문을 이해하는 데 가장 중요한 것 한 가지, 2문장"
}}

words는 중요한 단어 + 유용한 숙어·표현구 합쳐서 4~6개.
JSON만 출력.'''

class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            passage = body.get('passage', '')

            api_key = os.environ['GEMINI_API_KEY']
            prompt = PROMPT.format(passage=passage)

            payload = json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3}
            }).encode()

            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
            req = urllib.request.Request(url, data=payload, method='POST')
            req.add_header('Content-Type', 'application/json')

            with urllib.request.urlopen(req, timeout=60) as r:
                result = json.loads(r.read())

            text = result['candidates'][0]['content']['parts'][0]['text'].strip()
            if '```' in text:
                text = text.split('```')[1]
                if text.startswith('json'):
                    text = text[4:]
            data = json.loads(text.strip())
            for key in data.get('translations', {}):
                data['translations'][key] = mark_highlights(data['translations'][key])

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
