import json
import os
import re
import difflib
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
    text = re.sub(r'\[H\](.*?)\[/H\]', r'<span class="hl">\1</span>', text)
    return re.sub(r'\[V(\d+)\]\s*', r'<sup class="vn">\1</sup>', text)


# ── 하이라이트 검증: '똑같은 표현'은 코드로 강제 제거 ────────────────────
# AI가 규칙을 어기고 동일한 표현(NKJV/NASB 둘 다 "only begotten" 등)을 감싸는 일이 있어
# 프롬프트에만 의존하지 않고, 서버에서 짝을 직접 대조해 확실히 지운다.
def _plain(s):
    return re.sub(r'\[/?H\]', '', s or '')


def _norm_en(s):
    s = (s or '').lower()
    s = re.sub(r"[^a-z0-9' ]+", ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _norm_ko(s):
    s = re.sub(r'[^가-힣ㄱ-ㆎ0-9a-zA-Z ]+', ' ', s or '')
    return re.sub(r'\s+', '', s)


def _prune_one(text, other_plain, lang):
    """상대 번역본이 '글자 그대로 똑같은 표현'을 쓰고 있으면 그 하이라이트를 벗긴다."""
    if lang == 'en':
        hay = _norm_en(other_plain)

        def same_in_other(sp):
            n = _norm_en(sp)
            if not n:
                return True
            return re.search(r'(?<![a-z0-9])' + re.escape(n) + r'(?![a-z0-9])', hay) is not None
    else:
        hay = _norm_ko(other_plain)

        def same_in_other(sp):
            n = _norm_ko(sp)
            return (not n) or (n in hay)

    def repl(m):
        inner = m.group(1)
        return inner if same_in_other(inner) else '[H]' + inner + '[/H]'

    return re.sub(r'\[H\](.*?)\[/H\]', repl, text)


PAIR_KEYS = (('ko', '개역개정', '새번역'), ('en', 'NKJV', 'NASB'))


def _find_span(text, span, lang):
    """본문에서 표현의 위치를 찾는다. 영어는 대소문자 무시 + 단어 경계 우선."""
    if lang == 'en':
        m = re.search(r'(?<![A-Za-z0-9])' + re.escape(span) + r'(?![A-Za-z0-9])', text, re.I)
        if m:
            return m.start(), m.end()
        i = text.lower().find(span.lower())
        return (i, i + len(span)) if i >= 0 else (-1, -1)
    i = text.find(span)
    return (i, i + len(span)) if i >= 0 else (-1, -1)


def _wrap_ranges(text, ranges):
    """지정한 위치들을 [H]...[/H]로 감싼다. (겹치는 것은 앞의 것만)"""
    out, last = [], 0
    for st, en in sorted(ranges):
        if st < last or st < 0 or en > len(text) or en <= st:
            continue
        out.append(text[last:st])
        out.append('[H]' + text[st:en] + '[/H]')
        last = en
    out.append(text[last:])
    return ''.join(out)


# ── 두 번역본을 '기계적으로' 대조해서 다른 자리를 찾아낸다 ──────────────
# AI가 다른 자리를 놓치는 일이 잦아서(여호수아 1:10-13처럼 차이가 많은데 하나도 못 잡는 경우),
# 코드가 직접 낱말 단위로 비교해 '다른 자리'를 빠짐없이 짝으로 찾아낸다.
_TOK_EN = re.compile(r"[A-Za-z][A-Za-z'’]*")
_TOK_KO = re.compile(r'\S+')


def _tokens(text, lang):
    pat = _TOK_EN if lang == 'en' else _TOK_KO
    return [(m.group(0), m.start(), m.end()) for m in pat.finditer(text or '')]


def _tok_norm(t, lang):
    return _norm_en(t) if lang == 'en' else _norm_ko(t)


def auto_pairs(pa, pb, lang, limit=16):
    """두 본문에서 서로 다른 자리를 (A위치, B위치) 짝으로 돌려준다."""
    ta, tb = _tokens(pa, lang), _tokens(pb, lang)
    if not ta or not tb:
        return []
    na = [_tok_norm(t[0], lang) for t in ta]
    nb = [_tok_norm(t[0], lang) for t in tb]
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, na, nb, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        # 한쪽이 비어 있으면(한 번역본에만 있는 말) 앞뒤 낱말을 함께 묶어 '짝'을 만든다
        if i1 == i2 or j1 == j2:
            if i1 > 0 and j1 > 0:
                i1 -= 1
                j1 -= 1
            elif i2 < len(ta) and j2 < len(tb):
                i2 += 1
                j2 += 1
            else:
                continue
        if i1 >= i2 or j1 >= j2:
            continue
        # 한국어는 덩어리째 칠하면 지저분하므로, 낱말 수가 같으면 낱말끼리 짝지어 쪼갠다
        blocks = [(i1, i2, j1, j2)]
        if lang == 'ko' and 1 < (i2 - i1) == (j2 - j1) <= 3:
            blocks = [(i1 + k, i1 + k + 1, j1 + k, j1 + k + 1) for k in range(i2 - i1)]
        for x1, x2, y1, y2 in blocks:
            if lang == 'ko' and (x2 - x1 > 6 or y2 - y1 > 6):
                continue
            if ' '.join(na[x1:x2]).strip() == ' '.join(nb[y1:y2]).strip():
                continue
            out.append(((ta[x1][1], ta[x2 - 1][2]), (tb[y1][1], tb[y2 - 1][2])))
        if len(out) >= limit:
            break
    return out


def _ko_heads(s):
    """한국어 어절의 첫 음절 모음 — '문체만 바꾼 자리'를 걸러내는 데 쓴다."""
    return set(w[0] for w in re.findall(r'[가-힣]+', s or ''))


def _ko_style_only(sa, sb):
    """'내게 부족함이 없으리로다' ↔ '나는 부족한 것이 없습니다'처럼
    같은 낱말을 어미·문체만 바꿔 쓴 자리인지 판단한다."""
    A, B = _ko_heads(sa), _ko_heads(sb)
    if not A or not B:
        return True
    return len(A & B) / float(min(len(A), len(B))) >= 0.6


def build_highlights(trans, diffs):
    """하이라이트를 서버가 직접 만든다.
    - 영어(NKJV↔NASB): 코드 대조로 '다른 자리'를 전부 찾고, AI가 준 짝을 더한다.
    - 한국어(개역개정↔새번역): AI가 고른 짝을 우선 쓰고, 없으면 코드 대조 결과에서
      '문체만 다른 자리'를 걸러내고 쓴다.
    어느 경우든 결과는 반드시 양쪽 번역본에 '짝'으로 표시된다."""
    for lang, a, b in PAIR_KEYS:
        ta, tb = trans.get(a), trans.get(b)
        if not isinstance(ta, str) or not isinstance(tb, str):
            continue
        pa, pb = _plain(ta), _plain(tb)
        norm = _norm_en if lang == 'en' else _norm_ko

        ai_pairs = []
        for it in (((diffs or {}).get(lang) or []) if isinstance(diffs, dict) else []):
            if not isinstance(it, dict):
                continue
            sa = str(it.get(a) or '').strip()
            sb = str(it.get(b) or '').strip()
            if not sa or not sb or norm(sa) == norm(sb):
                continue                      # 글자 그대로 같은 표현 → 버림
            ra = _find_span(pa, sa, lang)
            rb = _find_span(pb, sb, lang)
            if ra[0] < 0 or rb[0] < 0:
                continue                      # 한쪽이라도 본문에 없으면 짝이 깨지므로 버림
            ai_pairs.append((ra, rb))

        auto = auto_pairs(pa, pb, lang)
        if lang == 'en':
            pairs = ai_pairs + auto
        else:
            pairs = ai_pairs or [
                p for p in auto
                if not _ko_style_only(pa[p[0][0]:p[0][1]], pb[p[1][0]:p[1][1]])
            ]

        # 양쪽 동시에 겹침 제거 → 한쪽만 남는 하이라이트가 생기지 않는다
        pairs.sort(key=lambda p: (p[0][0], p[1][0]))
        keep, la, lb = [], -1, -1
        for ra, rb in pairs:
            if ra[0] < la or rb[0] < lb:
                continue
            keep.append((ra, rb))
            la, lb = ra[1], rb[1]
            if len(keep) >= 16:
                break

        trans[a] = _wrap_ranges(pa, [k[0] for k in keep])
        trans[b] = _wrap_ranges(pb, [k[1] for k in keep])
    return trans


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


# ── 📖 실제 성경 본문 앵커 ───────────────────────────────────────────────
# AI 모델은 장절을 헷갈려 '다른 본문'을 그럴듯하게 지어낼 수 있다(할루시네이션).
# 성경 공부 도구에서 이건 치명적이므로, 공개 도메인 KJV 본문을 실제로 가져와
# 프롬프트에 '정답 본문'으로 박아 넣고, 결과가 그 본문과 맞는지 검증한다.

KO_BOOKS = {
    '창세기': 'genesis', '창': 'genesis',
    '출애굽기': 'exodus', '출': 'exodus',
    '레위기': 'leviticus', '레': 'leviticus',
    '민수기': 'numbers', '민': 'numbers',
    '신명기': 'deuteronomy', '신': 'deuteronomy',
    '여호수아': 'joshua', '수': 'joshua',
    '사사기': 'judges', '삿': 'judges',
    '룻기': 'ruth', '룻': 'ruth',
    '사무엘상': '1 samuel', '삼상': '1 samuel',
    '사무엘하': '2 samuel', '삼하': '2 samuel',
    '열왕기상': '1 kings', '왕상': '1 kings',
    '열왕기하': '2 kings', '왕하': '2 kings',
    '역대상': '1 chronicles', '대상': '1 chronicles',
    '역대하': '2 chronicles', '대하': '2 chronicles',
    '에스라': 'ezra', '스': 'ezra',
    '느헤미야': 'nehemiah', '느': 'nehemiah',
    '에스더': 'esther', '에': 'esther',
    '욥기': 'job', '욥': 'job',
    '시편': 'psalms', '시': 'psalms',
    '잠언': 'proverbs', '잠': 'proverbs',
    '전도서': 'ecclesiastes', '전': 'ecclesiastes',
    '아가': 'song of solomon', '아': 'song of solomon',
    '이사야': 'isaiah', '사': 'isaiah',
    '예레미야애가': 'lamentations', '애가': 'lamentations', '애': 'lamentations',
    '예레미야': 'jeremiah', '렘': 'jeremiah',
    '에스겔': 'ezekiel', '겔': 'ezekiel',
    '다니엘': 'daniel', '단': 'daniel',
    '호세아': 'hosea', '호': 'hosea',
    '요엘': 'joel', '욜': 'joel',
    '아모스': 'amos', '암': 'amos',
    '오바댜': 'obadiah', '옵': 'obadiah',
    '요나': 'jonah', '욘': 'jonah',
    '미가': 'micah', '미': 'micah',
    '나훔': 'nahum', '나': 'nahum',
    '하박국': 'habakkuk', '합': 'habakkuk',
    '스바냐': 'zephaniah', '습': 'zephaniah',
    '학개': 'haggai', '학': 'haggai',
    '스가랴': 'zechariah', '슥': 'zechariah',
    '말라기': 'malachi', '말': 'malachi',
    '마태복음': 'matthew', '마태': 'matthew', '마': 'matthew',
    '마가복음': 'mark', '마가': 'mark', '막': 'mark',
    '누가복음': 'luke', '누가': 'luke', '눅': 'luke',
    '요한복음': 'john', '요한': 'john', '요': 'john',
    '사도행전': 'acts', '행': 'acts',
    '로마서': 'romans', '롬': 'romans',
    '고린도전서': '1 corinthians', '고전': '1 corinthians',
    '고린도후서': '2 corinthians', '고후': '2 corinthians',
    '갈라디아서': 'galatians', '갈': 'galatians',
    '에베소서': 'ephesians', '엡': 'ephesians',
    '빌립보서': 'philippians', '빌': 'philippians',
    '골로새서': 'colossians', '골': 'colossians',
    '데살로니가전서': '1 thessalonians', '살전': '1 thessalonians',
    '데살로니가후서': '2 thessalonians', '살후': '2 thessalonians',
    '디모데전서': '1 timothy', '딤전': '1 timothy',
    '디모데후서': '2 timothy', '딤후': '2 timothy',
    '디도서': 'titus', '딛': 'titus',
    '빌레몬서': 'philemon', '몬': 'philemon',
    '히브리서': 'hebrews', '히': 'hebrews',
    '야고보서': 'james', '약': 'james',
    '베드로전서': '1 peter', '벧전': '1 peter',
    '베드로후서': '2 peter', '벧후': '2 peter',
    '요한일서': '1 john', '요일': '1 john',
    '요한이서': '2 john', '요이': '2 john',
    '요한삼서': '3 john', '요삼': '3 john',
    '유다서': 'jude', '유': 'jude',
    '요한계시록': 'revelation', '계시록': 'revelation', '계': 'revelation',
}
# 긴 이름부터 매칭해야 '요한복음'이 '요'로 잘리지 않는다.
_BOOK_KEYS = sorted(KO_BOOKS.keys(), key=len, reverse=True)


def parse_ref(passage):
    """'여호수아 4:6-7', '수 4:6~7', '요한복음 3장 16절' → ('joshua', 4, 6, 7).
    해석 못 하면 None."""
    s = re.sub(r'\s+', '', str(passage or '')).replace('～', '-').replace('~', '-').replace('–', '-')
    book = None
    for k in _BOOK_KEYS:
        if s.startswith(k):
            book = KO_BOOKS[k]
            s = s[len(k):]
            break
    if not book:
        m = re.match(r'^([1-3]?[a-zA-Z\s]+)', str(passage or '').strip())
        if not m:
            return None
        book = m.group(1).strip().lower()
        s = re.sub(r'\s+', '', str(passage or '').strip()[m.end():])
    s = s.replace('장', ':').replace('절', '').lstrip(':')
    m = re.match(r'^(\d+)[:.](\d+)(?:-(\d+))?', s)
    if m:
        c, v1 = int(m.group(1)), int(m.group(2))
        return (book, c, v1, int(m.group(3)) if m.group(3) else v1)
    m = re.match(r'^(\d+)$', s)
    if m:
        return (book, int(m.group(1)), None, None)   # 장 전체
    return None


def fetch_anchor(passage):
    """공개 도메인 KJV 본문을 실제로 가져온다. 실패하면 None (분석은 계속 진행)."""
    ref = parse_ref(passage)
    if not ref:
        return None
    book, ch, v1, v2 = ref
    q = '%s %d' % (book, ch)
    if v1:
        q += ':%d' % v1
        if v2 and v2 != v1:
            q += '-%d' % v2
    url = 'https://bible-api.com/' + urllib.parse.quote(q) + '?translation=kjv'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'come-away/1.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
    except Exception:
        return None
    verses = d.get('verses') or []
    if not verses:
        return None
    lines = []
    for v in verses[:60]:
        t = re.sub(r'\s+', ' ', str(v.get('text') or '')).strip()
        if t:
            lines.append('%d. %s' % (v.get('verse') or 0, t))
    if not lines:
        return None
    return {'ref': d.get('reference') or q,
            'lines': lines,
            'text': ' '.join(lines),
            'exact': v1 is not None,
            'byv': dict((v.get('verse'), re.sub(r'\s+', ' ', str(v.get('text') or '')).strip())
                        for v in verses if v.get('verse')),
            'nums': [v.get('verse') for v in verses if v.get('verse')]}


_STOP_EN = set('''the a an and or of to in is are was were be been that this these those
for with from by on at as it its he she they them his her their you your ye thou thy thee
shall will not but so which who whom what when then there here up out over into unto
'''.split())


def _content_words(s):
    w = re.findall(r'[a-z]+', str(s or '').lower())
    return set(x for x in w if len(x) > 2 and x not in _STOP_EN)


TR_KEYS = ('개역개정', '새번역', 'NKJV', 'NASB')


def _vnum(x):
    s = re.sub(r'\D', '', str(x if x is not None else ''))
    return int(s) if s else None


def _esc(s):
    """화면에 그대로 넣을 본문이므로 HTML 특수문자를 막아 둔다."""
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def assemble(data):
    """AI가 준 verses(절별 배열) → 화면용 translations.
    절 번호를 윗첨자로 붙여 그대로 이어 붙인다 (하이라이트 없음, 본문 그대로)."""
    vs = (data or {}).get('verses')
    if not isinstance(vs, list) or not vs:
        return None
    rows = []
    for v in vs:
        if not isinstance(v, dict):
            continue
        n = _vnum(v.get('n'))
        if n is None:
            continue
        row = {'n': n}
        for k in TR_KEYS:
            row[k] = re.sub(r'\s+', ' ', str(v.get(k) or '')).strip()
        if any(row[k] for k in TR_KEYS):
            rows.append(row)
    if not rows:
        return None
    rows.sort(key=lambda r: r['n'])
    tr = {}
    for k in TR_KEYS:
        parts = ['<sup class="vn">%d</sup>%s' % (r['n'], _esc(r[k])) for r in rows if r[k]]
        if parts:
            tr[k] = ' '.join(parts)
    return {'rows': rows, 'translations': tr} if tr else None


def verses_ok(rows, anchor):
    """★핵심 검증★ 절 번호가 요청 범위와 정확히 맞는지, 그리고 '절마다' 내용이
    그 절의 실제 본문과 맞는지 대조한다. 한두 절씩 밀려 쓰는 오류를 여기서 잡는다."""
    if not anchor:
        return True
    rows = rows or []
    if not rows:
        return False
    want = [n for n in (anchor.get('nums') or []) if n]
    got = [r['n'] for r in rows]
    if want:
        if anchor.get('exact'):
            if got != want:                      # 범위를 지정했으면 절 번호가 정확히 같아야 한다
                return False
        elif not set(got) <= set(want):
            return False
    byv = anchor.get('byv') or {}
    checked = 0
    for r in rows:
        base = _content_words(byv.get(r['n']))
        if len(base) < 4:
            continue                             # 너무 짧은 절은 판정 불가
        got_w = _content_words(r.get('NKJV')) | _content_words(r.get('NASB'))
        if not got_w:
            return False
        if len(base & got_w) / float(len(base)) < 0.4:
            return False                         # 그 절 자리에 다른 절 내용이 들어옴
        checked += 1
    return checked > 0 or not byv


_ARCHAIC = re.compile(r'\b(ye|thou|thy|thee|hath|doth|saith|hither|unto|'
                      r'\w+eth)\b', re.I)


def archaic_bad(data):
    """NKJV/NASB 자리에 KJV 옛 문체가 들어왔는지. (현대 번역본인데 KJV를 베낀 경우)"""
    tr = (data or {}).get('translations') or {}
    for k in ('NKJV', 'NASB'):
        s = _plain(str(tr.get(k) or ''))
        if len(_ARCHAIC.findall(s)) >= 3:
            return True
    return False


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
{anchor}
■ 본문 정확성 — 다른 무엇보다 먼저 지켜야 할 규칙:
- ★verses는 절 하나에 항목 하나씩, "n"에 절 번호를 반드시 숫자로 적어라. 요청 범위의 절 번호와 **정확히 일치**해야 한다.
  (예: "여호수아 4:6-8"을 요청받았으면 n은 6, 7, 8 세 개. 4나 5를 넣거나 6만 넣으면 오답이다.)
- ★각 항목의 내용은 그 절 번호의 내용이어야 한다. 한 절씩 밀려 쓰는 실수가 가장 흔하니, 적기 전에 절 번호와 내용을 한 번 대조하라.
- 요청받은 장·절의 내용을 정확히 다뤄야 한다. 기억이 흐릿하면 지어내지 말고, 위에 주어진 실제 본문을 그대로 근거로 삼아라.
- 인접한 장(예: 4장을 요청했는데 3장 내용)을 쓰는 것은 절대 금지다. 이 도구는 성경 공부용이라 본문이 틀리면 아무 의미가 없다.
- NKJV와 NASB는 **현대 영어 번역본**이다. KJV의 옛 문체(passeth, cometh, ye, thou, thy, thee, unto, hath, doth, saith, hither)를 쓰면 안 된다.
  NKJV는 "Then Joshua said to the children of Israel, 'Come here...'"처럼, NASB는 "Then Joshua said to the sons of Israel, 'Come here...'"처럼 현대 영어로 적어라.
- ★위에 주어진 KJV 본문은 '절 번호와 내용을 맞추기 위한 대조표'일 뿐이다. 문장을 그대로 옮겨 적지 마라.★
  (예: KJV "What mean ye by these stones?" → NKJV는 "What do these stones mean to you?", NASB는 "What do these stones mean to you?"로 적어야 한다.
   "What mean you by these stones"처럼 KJV 어순을 살짝 고쳐 쓴 것은 NKJV가 아니다.)
- 개역개정과 새번역도 각각 실제 그 번역본의 문장이어야 한다. 한쪽을 베껴 쓰지 마라.

{{
  "verses": [
    {{
      "n": 6,
      "개역개정": "그 절의 개역개정 본문 (그 절만, 표시 없는 순수 본문)",
      "새번역": "그 절의 새번역 본문 (그 절만)",
      "NKJV": "that verse only, NKJV (modern English)",
      "NASB": "that verse only, NASB (modern English)"
    }}
  ],
  "words": [
    {{
      "english": "본문에 나오는 '영어 단어 하나' 또는 '굳어진 표현/숙어'. 문장·해석 금지 (예: eternal, everlasting, perish, begotten, so, lay down).",
      "korean": "그 단어의 짧은 한국어 대응어 (예: 영원한)",
      "pos": "품사 (명사/동사/형용사/부사/전치사/접속사/감탄사/동사구/명사구/숙어 중 하나)",
      "meaning": "표준 영어사전(옥스퍼드·메리엄웹스터 급)의 '사전적 정의'를 한국어로 2~3개, 쉼표 구분. 의역·직역·성경식 풀이 금지. (예 so: '매우, 많이, 이 정도로, 이렇게')",
      "nuance": "성경 뜻이 아니라 '영어에서 실제 쓰이는 어감·용법' + 간단한 예문 하나. (예 so: '정도를 강조하는 강조부사. too가 부정적 과함을 뜻하는 것과 달리 so는 주로 긍정적 강조에 쓴다. 예: It''s so beautiful! 정말 아름다워)"
    }}
  ],
  "originals": [
    {{
      "strong": "Strong's 번호 (히브리어면 H로 시작 예:H7462, 헬라어면 G로 시작 예:G26)",
      "original": "헬라어/히브리어 원어",
      "reading": "음역 (예: 로이)",
      "korean": "해당하는 한국어 단어",
      "where": "★이 원어가 '이 본문 안에서' 나오는 자리. 반드시 절 번호와 그 자리의 한국어 표현을 함께 적어라. 예: 3절 '거룩하게 하시고' / 12절 '높이시며'. 여러 절에 나오면 쉼표로 두 곳까지.",
      "meaning": "Strong's 사전에 실린 기본 뜻만 아주 짧게 (한 줄, 사전 뜻 나열)",
      "nuance": "★가장 중요한 칸★ 이 단어가 성경 전체에서 '실제로 어떻게 쓰이는지'를 2~3문장으로. 사전 뜻을 다시 말하지 말고, 그 뜻의 '결'을 설명하라 — 어느 정도의 강도인지, 무엇과 무엇을 갈라놓는 말인지, 주로 누가 누구에게 하는 동작인지, 비슷한 다른 원어와 어떻게 다른지.",
      "refs": [
        {{
          "ref": "같은 Strong's 번호가 쓰인 '다른 본문'의 장절 (예: 출 3:5)",
          "phrase": "그 구절에서 이 단어가 들어간 짧은 어구 (예: 네 발에서 신을 벗으라 네가 선 곳은 거룩한 땅이니라)",
          "note": "그 쓰임이 이 단어에 대해 알려 주는 것 한 문장 (예: 땅 자체가 달라진 게 아니라 하나님이 계셔서 구별된 것 — 카다쉬는 '하나님께 속하게 됨'이다)"
        }}
      ],
      "point": "그래서 이 본문에서 이 단어를 이렇게 읽으면 무엇이 달라지는가, 한 문장"
    }}
  ],
  "background": "이 본문의 배경. 표준 주석에 근거해 3~4문장으로, 반드시 다음 세 가지를 모두 담아라 — (1) 이 본문이 쓰인 시대와 당시 상황, (2) 이 책을 쓴 저자가 누구인지, (3) 저자가 이 본문을 누구에게 왜 썼는지(집필 의도)."
}}

words 규칙 (★"영어 사전"이라고 생각하고 뽑아라. 성경 해석이 아니다★):
- 표제(english)는 이 본문의 영어 본문(NKJV/NASB)에 실제로 나오는 "영어 단어 하나" 또는 "굳어진 표현/숙어"여야 한다. 문장 조각이나 해석(예: "believes in Him", "should not perish")은 절대 넣지 마라 — 그건 단어가 아니라 문장이다. 사전 표제어가 될 만한 것만: 낱말(everlasting, eternal, perish, begotten, so) 또는 숙어/구동사(lay down, abide in).
- everlasting과 eternal처럼 서로 다른 단어는 하나로 묶지 말고 각각 따로 항목으로 넣어라. ("everlasting life / eternal life"처럼 합치지 마라.)
- meaning = 표준 영어사전의 사전적 정의(한국어) 2~3개. 문맥 의역·성경식 풀이 금지. 그 단어를 사전에서 찾으면 나오는 뜻을 적어라.
- nuance = 그 단어가 "영어에서 일상적으로 어떤 어감·용법으로 쓰이는지" + 예문 하나. 성경적 의미로 설명하지 마라. (예: so는 too와 달리 긍정적 강조에 쓴다는 식.)
- 쉽고 뻔한 단어(the, and, God, is)는 빼고, 배울 가치가 있는 단어·표현 위주로.
- ★개수: 반드시 8개 이상 12개까지 뽑아라.★ 5개만 주는 것은 부족하다. 본문이 여러 절이면 절마다 골고루 뽑아라. 중급 이상 학습자에게 유용한 단어(commanded, provisions, possess, rest, servant, remember, prepare, officers, tribe, inheritance 같은 수준)면 충분히 넣을 만하다. 정말로 본문이 짧아 단어가 모자랄 때만 8개 미만이어도 된다.
- 원어(헬라어/히브리어)는 words에 넣지 마 (originals에서).
originals 규칙 (★영어단어 외우듯 '단어=뜻'으로 끝내지 마라. 이 칸의 목적은 뉘앙스다★):
- 이 본문에서 가장 중요한 원어 딱 3개만.
- Strong's 번호는 반드시 정확해야 한다 (블루레터바이블·바이블허브에서 검증 가능해야 하므로).
- ★where는 반드시 채워라.★ 읽는 사람이 "몇 절 어느 단어가 이 원어인지"를 알아야 본문에서 찾을 수 있다. 절 번호 + 그 자리의 한국어 표현(개역개정 기준)을 꼭 같이 적어라. 절이 하나뿐인 본문이면 그 절 번호를 그대로 적어라.
- meaning은 사전 뜻 한 줄로 짧게 끝내라. 설명은 nuance에서 한다.
- nuance는 "그래서 이게 뭐가 다른데?"에 답해야 한다. 예를 들어 '거룩하게 하다'로 끝내지 말고, 카다쉬는 도덕적으로 깨끗해지는 말이 아니라 '보통의 자리에서 떼어 내어 하나님께 속하게 하는' 말이며 사람이 스스로 하는 게 아니라 하나님이 하시는 동작이 대부분이라는 식으로, 그 단어의 결을 드러내라.
- ★refs는 반드시 2~3개 넣어라.★ 반드시 "이 본문이 아닌 다른 성경 구절"에서, "같은 Strong's 번호"가 쓰인 자리를 골라라. 유명하고 그림이 그려지는 장면을 우선하라 (예: H6942 카다쉬 → 창 2:3 일곱째 날, 출 3:5 거룩한 땅, 출 19:23 시내산 경계).
- refs의 phrase는 그 구절에 실제로 있는 어구를 적어라. note는 그 장면이 이 단어의 어떤 면(강도·방식·구별의 성격)을 보여 주는지 한 문장으로 적어라. 구절 요약이 아니라 '단어 해설'이어야 한다.
- 세 원어의 refs가 서로 겹치지 않게 하라. 확실하지 않은 장절은 넣지 말고 확실한 것만 넣어라 (틀린 장절은 최악이다).
- point는 이 본문으로 돌아와서, 그 뉘앙스를 알고 읽으면 문장이 어떻게 달라지는지 한 문장.
verses 규칙:
- 절 본문에는 아무 표시(별표·괄호·하이라이트)도 붙이지 마라. 그 번역본의 문장을 있는 그대로만 적어라.
- 절 번호는 "n"에만 숫자로 적고, 본문 문자열 안에는 절 번호를 쓰지 마라.
추측하지 말고 확실한 것만. JSON만 출력.'''


DEFINE_PROMPT = '''아래 영어 단어들의 "영어사전 뜻"을 알려줘. 성경 해석이 아니라 표준 영어사전(옥스퍼드·메리엄웹스터 급) 기준이다.
코드블록 없이 JSON만 출력해.

단어 목록: {words}

{{
  "defs": [
    {{
      "english": "표제어 (입력받은 그대로)",
      "korean": "가장 대표적인 한국어 대응어 하나 (예: 영원한)",
      "pos": "품사 (명사/동사/형용사/부사/전치사/접속사/감탄사/동사구/명사구/숙어 중 하나)",
      "meaning": "표준 영어사전의 사전적 정의를 한국어로 2~3개, 쉼표로 구분. 의역·성경식 풀이 금지.",
      "nuance": "영어에서 실제 쓰이는 어감·용법 한 문장 + 짧은 예문 하나 (한국어 해석 포함)"
    }}
  ]
}}

규칙:
- 입력된 단어는 하나도 빠뜨리지 말고 모두 defs에 넣어라. english는 입력 그대로 적어라.
- meaning은 반드시 사전에 실린 정의여야 한다. 문맥에 맞춘 의역이나 신학적 설명을 넣지 마라.
- everlasting과 eternal처럼 비슷한 단어도 각각 자기 사전 뜻을 정확히 구분해서 적어라.
- 한국어 단어가 섞여 있으면 그 단어는 뜻풀이만 간단히 적어라.
JSON만 출력.'''


SCHEMA_VER = 15  # 분석 결과 형식 버전. 올리면 이전 캐시를 자동으로 무시하고 다시 분석함.


def _valid(d):
    """분석 결과가 화면에 그릴 만큼 온전한지 (assemble()이 만든 translations가 있는지)."""
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


def anchor_block(anchor):
    """프롬프트에 끼워 넣을 '정답 본문' 블록."""
    if not anchor:
        return ''
    return ('\n■ 이 본문의 실제 내용 — 절 번호와 내용의 정답표다 (참고용 공개 도메인 원문):\n'
            '[' + anchor['ref'] + ']\n' + '\n'.join(anchor['lines']) +
            '\n★verses의 "n"과 내용은 위 표와 절 단위로 정확히 맞아야 한다. '
            '위에 없는 절을 넣거나, 위에 있는 절을 빠뜨리거나, 한 절씩 밀려 쓰면 전부 오답이다.\n'
            '★단, 위 문장을 그대로 베끼지 마라. 위는 옛 문체(KJV)이고 화면에 나가는 것은 '
            'NKJV·NASB·개역개정·새번역이다. 내용만 맞추고 문장은 각 번역본의 것으로 적어라.\n')


def call_ai(passage, strict=False, anchor=None):
    return call_ai_raw(PROMPT.format(passage=passage, anchor=anchor_block(anchor)), strict)


def call_ai_raw(content, strict=False):
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

            # ── 단어 뜻만 찾기 (플래시카드 정답용). 단어별로 캐시해서 두 번째부터는 즉시. ──
            if body.get('mode') == 'define':
                self._define(body)
                return

            passage = body.get('passage', '').strip()
            if not passage:
                self._send_json({'error': 'empty', 'message': '본문을 입력해 주세요.'}, 400)
                return

            key = normalize_key(passage)
            qkey = urllib.parse.quote(key)

            # 1. 캐시 확인.
            #    SCHEMA_VER 14부터는 '절 단위 검증을 통과한 결과'만 저장되므로, 저장된 것은
            #    이미 절 번호·내용이 실제 본문과 대조된 것이다. 검증 안 된 옛 캐시는 v가 달라 자동으로 무시된다.
            #    body에 fresh=true가 오면(‘다시 분석’ 버튼) 캐시를 건너뛰고 새로 분석한다.
            cached = None if body.get('fresh') else \
                sb('GET', 'analyses?passage_key=eq.' + qkey + '&select=data', silent=True)
            if cached and _valid(cached[0].get('data')) and cached[0]['data'].get('v') == SCHEMA_VER:
                data = cached[0]['data']
                data['cached'] = True
                self._send_json(data)
                return

            # 1-b. 실제 성경 본문(KJV, 공개 도메인)을 먼저 가져와 '정답표'로 삼는다.
            #      AI가 장절을 헷갈려 엉뚱한 본문을 지어내는 것을 막기 위함.
            anchor = fetch_anchor(passage)

            def attempt(strict):
                """AI 호출 → 절별 배열(verses)을 화면용 translations로 조립까지."""
                d = call_ai(passage, strict=strict, anchor=anchor)
                a = assemble(d)
                if a:
                    d['verses_rows'] = a['rows']
                    d['translations'] = a['translations']
                return d

            # 2. AI 호출 (JSON 파싱/형식 실패 시 strict 모드로 1회 재시도)
            try:
                data = attempt(False)
                if not _valid(data):
                    data = attempt(True)
            except (ValueError, json.JSONDecodeError):
                data = attempt(True)

            # 결과가 여전히 온전치 않으면 깨진 데이터를 저장/반환하지 않고 명확히 알린다.
            if not _valid(data):
                self._send_json({'error': 'bad_ai',
                                 'message': '분석 결과를 온전히 받지 못했어요. 잠시 후 다시 시도해 주세요.'}, 502)
                return

            # 2-b. ★절 단위 검증★ — 절 번호가 요청 범위와 정확히 같은지, 절마다 내용이 그 절의
            #      실제 본문과 맞는지 대조한다. 한 절씩 밀려 쓴 결과나 NKJV/NASB 자리의 KJV
            #      옛 문체가 걸리면 1회 재시도.
            if (anchor and not verses_ok(data.get('verses_rows'), anchor)) or archaic_bad(data):
                try:
                    retry = attempt(True)
                    if _valid(retry) and (not anchor or verses_ok(retry.get('verses_rows'), anchor)):
                        data = retry
                except (ValueError, json.JSONDecodeError):
                    pass
            # 재시도 후에도 '다른 본문'이면 틀린 말씀을 보여주느니 솔직히 알린다.
            if anchor and not verses_ok(data.get('verses_rows'), anchor):
                self._send_json({'error': 'wrong_passage',
                                 'message': '요청하신 본문과 다른 내용이 와서 표시하지 않았어요. '
                                            '한 번만 다시 눌러 주세요. (' + anchor['ref'] + ')'}, 502)
                return

            # 단어는 최대 10개까지만 (AI가 더 줘도 잘라냄; 부족하면 있는 만큼)
            if isinstance(data.get('words'), list):
                data['words'] = data['words'][:12]

            # 하이라이트는 하지 않는다. 번역본 비교 칸은 '본문 그대로'만 정확히 보여 준다.
            data.pop('diffs', None)
            data.pop('verses', None)        # 조립이 끝났으면 원본 절 배열은 보낼 필요 없다
            data.pop('verses_rows', None)
            data['v'] = SCHEMA_VER   # 형식 버전 기록 (옛 캐시 자동 무효화용)

            # 3. 캐시 교체 저장 (예전/깨진 캐시가 있으면 지우고 새로 저장). 실패해도 응답엔 지장 없음.
            sb('DELETE', 'analyses?passage_key=eq.' + qkey, silent=True)
            sb('POST', 'analyses', {'passage_key': key, 'passage': passage, 'data': data}, silent=True)

            data['cached'] = False
            self._send_json(data)
        except Exception as e:
            self._send_json({'error': str(e)}, 500)

    def _define(self, body):
        """영어 단어들의 사전적 뜻을 돌려준다. analyses 테이블에 'def:단어'로 캐시."""
        raw = body.get('words')
        want = []
        if isinstance(raw, list):
            for w in raw:
                w = str(w or '').strip()
                if w and w not in want:
                    want.append(w)
        want = want[:30]   # 한 번에 최대 30개
        if not want:
            self._send_json({'ok': True, 'defs': {}})
            return

        defs, missing = {}, []
        for w in want:
            ck = 'def:' + normalize_key(w)
            row = sb('GET', 'analyses?passage_key=eq.' + urllib.parse.quote(ck) + '&select=data', silent=True)
            d = row[0].get('data') if row else None
            if isinstance(d, dict) and d.get('meaning'):
                defs[w] = d
            else:
                missing.append(w)

        if missing:
            try:
                out = call_ai_raw(DEFINE_PROMPT.format(words=', '.join(missing)))
            except (ValueError, json.JSONDecodeError):
                out = call_ai_raw(DEFINE_PROMPT.format(words=', '.join(missing)), strict=True)
            got = out.get('defs') if isinstance(out, dict) else None
            by_low = {w.lower(): w for w in missing}
            for item in (got or []):
                if not isinstance(item, dict):
                    continue
                en = str(item.get('english') or '').strip()
                orig = by_low.get(en.lower(), en)
                if not orig:
                    continue
                d = {
                    'ko': str(item.get('korean') or '')[:80],
                    'meaning': str(item.get('meaning') or '')[:400],
                    'nuance': str(item.get('nuance') or '')[:400],
                    'pos': str(item.get('pos') or '')[:20],
                }
                if not d['meaning']:
                    continue
                defs[orig] = d
                ck = 'def:' + normalize_key(orig)
                sb('DELETE', 'analyses?passage_key=eq.' + urllib.parse.quote(ck), silent=True)
                sb('POST', 'analyses', {'passage_key': ck, 'passage': orig, 'data': d}, silent=True)

        self._send_json({'ok': True, 'defs': defs})

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
