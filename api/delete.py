import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler

class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            entry_id = body.get('id')
            url = os.environ['SUPABASE_URL'] + f'/rest/v1/entries?id=eq.{entry_id}'
            req = urllib.request.Request(url, method='DELETE')
            req.add_header('apikey', os.environ['SUPABASE_ANON_KEY'])
            req.add_header('Authorization', 'Bearer ' + os.environ['SUPABASE_ANON_KEY'])
            req.add_header('Prefer', 'return=minimal')
            urllib.request.urlopen(req)
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
