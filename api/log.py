import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler

def sb(method, path, data=None, extra_headers=None):
    url = os.environ['SUPABASE_URL'] + '/rest/v1/' + path
    req = urllib.request.Request(url, method=method)
    req.add_header('apikey', os.environ['SUPABASE_ANON_KEY'])
    req.add_header('Authorization', 'Bearer ' + os.environ['SUPABASE_ANON_KEY'])
    req.add_header('Content-Type', 'application/json')
    req.add_header('Prefer', 'return=representation')
    if extra_headers:
        for k, v in extra_headers.items():
            req.add_header(k, v)
    if data is not None:
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        try:
            entries = sb('GET', 'entries?select=*&order=date.asc,created_at.asc')
            self._send_json(entries)
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            sb('POST', 'entries', {
                'date': body.get('date', ''),
                'passage': body.get('passage', ''),
                'summary': body.get('summary', ''),
                'action': body.get('action', ''),
                'words': body.get('words', [])
            })
            self._send_json({'ok': True})
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
