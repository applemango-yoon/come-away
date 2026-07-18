import json
import os
import re
import urllib.request
from http.server import BaseHTTPRequestHandler

def mark_highlights(text):
    return re.sub(r'\[H\](.*?)\[/H\]', r'<span class="hl">\1</span>', text)

PROMPT = '''성경 본문 "{passage}"를 분석해서 아래 JSON 형식으로만 응답해줘. 코드블록 없이 JSON만.

번역본 4개를 비교해서, 서로 다르게 표현된 단어나 구절은 [H]단어[/H] 로 감싸줘.

{{
  "translations": {{
    "개역개정": "(본문 전체, 다른 표현은 [H]...[/H] 로 감싸기)",
    "새번역": "(본문 전체, 다른 표현은 [H]...[/H] 로 감싸기)",
    "NKJV": "(full text, wrap different expressions in [H]...[/H])",
    "NASB": "(full text, wrap different expressions in [H]...[/H])"
  }},
  "words": [
    {{
      "korean": "단어 또는 표현구",
      "english": "word or phrase",
      "pos": "품사 (명사/동사/형용사/부사/동사구/명사구/형용사구 중 하나)",
      "original": "헬라어/히브리어 (단어면 기재, 표현구면 빈 문자열)",
      "meaning": "원어 뜻 또는 한국어 의미",
      "nuance": "영어 뉘앙스와 문맥 속 의미"
    }}
  ],
  "background": "이 본문 이해에 가장 중요한 배경 한 가지, 2문장"
}}

words는 4~6개. JSON만 출력.'''

class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            passage = body.get('passage', '')

            payload = json.dumps({
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": PROMPT.format(passage=passage)}],
                "max_tokens": 2048,
                "temperature": 0.3
            }).encode()

            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=payload, method='POST'
            )
            req.add_header('Authorization', 'Bearer ' + os.environ['GROQ_API_KEY'])
            req.add_header('Content-Type', 'application/json')

            with urllib.request.urlopen(req, timeout=60) as r:
                result = json.loads(r.read())

            text = result['choices'][0]['message']['content'].strip()
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
