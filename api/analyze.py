import json
import os
import re
import difflib
import urllib.parse
import urllib.request
import urllib.error
import html as _htmlmod
from concurrent.futures import ThreadPoolExecutor
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


def _lab(s):
    """하이라이트 말풍선에 넣을 상대 번역본 표현 (따옴표·대괄호는 빼서 안전하게)."""
    s = str(s or '')
    for ch in '[]|"':
        s = s.replace(ch, ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:60]


def mark_highlights(text):
    """[H|상대표현]본문[/H] → <span class="hl" title="…">본문</span>"""
    def _h(m):
        label = (m.group(1) or '').strip()
        inner = m.group(2)
        if label:
            return '<span class="hl" title="다른 번역본: %s">%s</span>' % (label, inner)
        return '<span class="hl">%s</span>' % inner

    text = re.sub(r'\[H(?:\|([^\]]*))?\](.*?)\[/H\]', _h, text or '')
    return re.sub(r'\[V(\d+)\]\s*', r'<sup class="vn">\1</sup>', text)


def _plain(s):
    """표시용 태그·표시자를 걷어낸 맨 글자 (본문 검사용)."""
    s = re.sub(r'\[/?H(?:\|[^\]]*)?\]', '', s or '')
    return re.sub(r'<[^>]*>', ' ', s)


def _norm_en(s):
    s = (s or '').lower()
    s = re.sub(r"[^a-z0-9' ]+", ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _wrap_ranges(text, ranges):
    """지정한 위치들을 [H|상대표현]…[/H]로 감싼다. (겹치는 것은 앞의 것만)"""
    out, last = [], 0
    for st, en, label in sorted(ranges):
        if st < last or st < 0 or en > len(text) or en <= st:
            continue
        out.append(text[last:st])
        out.append('[H|' + _lab(label) + ']' + text[st:en] + '[/H]')
        last = en
    out.append(text[last:])
    return ''.join(out)


# ── 두 영어 번역본을 '기계적으로' 대조해서 다른 자리를 찾아낸다 ──────────
# NKJV·NASB는 성경 본문 제공처에서 글자 그대로 받아오므로, AI에게 묻지 않고
# 코드가 직접 낱말 단위로 비교한다. (빠르고, 지어낼 여지가 없다)
_TOK_EN = re.compile(r"[A-Za-z][A-Za-z'\u2019]*")


def _tokens(text):
    return [(m.group(0), m.start(), m.end()) for m in _TOK_EN.finditer(text or '')]


def auto_pairs(pa, pb, limit=16):
    """두 영어 본문에서 서로 다른 자리를 찾아 돌려준다.
    각 항목은 (A위치, B위치, A묶음글, B묶음글).
    '묶음글'은 낱말 하나만 사이에 두고 이어진 자리들을 통째로 이은 것으로,
    'walks not' ↔ 'does not walk'처럼 어순만 바뀐 자리를 통째로 보고 걸러내는 데 쓴다."""
    ta, tb = _tokens(pa), _tokens(pb)
    if not ta or not tb:
        return []
    na = [_norm_en(t[0]) for t in ta]
    nb = [_norm_en(t[0]) for t in tb]

    groups = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, na, nb, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        if groups and (i1 - groups[-1][-1][1]) <= 1 and (j1 - groups[-1][-1][3]) <= 1:
            groups[-1].append((i1, i2, j1, j2))
        else:
            groups.append([(i1, i2, j1, j2)])

    def _txt(toks, src, x1, x2):
        return src[toks[x1][1]:toks[x2 - 1][2]] if x2 > x1 else ''

    out = []
    for g in groups:
        ga = _txt(ta, pa, g[0][0], g[-1][1])
        gb = _txt(tb, pb, g[0][2], g[-1][3])
        for i1, i2, j1, j2 in g:
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
            if (i2 - i1) > 6 or (j2 - j1) > 6:
                continue                  # 너무 긴 덩어리는 칠하면 지저분하다
            if ' '.join(na[i1:i2]).strip() == ' '.join(nb[j1:j2]).strip():
                continue
            out.append(((ta[i1][1], ta[i2 - 1][2]), (tb[j1][1], tb[j2 - 1][2]), ga, gb))
        if len(out) >= limit:
            break
    return out[:limit]


# ── '뜻이 정말 다른 자리'만 남기는 거르개 ────────────────────────────────
# 규칙: 뉘앙스·낱말 선택이 분명히 다른 곳만 칠한다.
#       어미(-eth/-s/-ed)나 조동사·관사·전치사만 다른 자리는 칠하지 않는다.
_ARCH_MAP = {
    'thou': 'you', 'thee': 'you', 'thy': 'your', 'thine': 'your', 'ye': 'you',
    'hath': 'have', 'hast': 'have', 'has': 'have', 'had': 'have',
    'doth': 'do', 'dost': 'do', 'does': 'do', 'did': 'do',
    'saith': 'say', 'sayeth': 'say', 'says': 'say', 'said': 'say',
    'shalt': 'shall', 'wilt': 'will',
    'art': 'be', 'is': 'be', 'are': 'be', 'was': 'be', 'were': 'be', 'am': 'be',
    'been': 'be', 'being': 'be',
    'unto': 'to', 'upon': 'on',
    'brethren': 'brother', 'brothers': 'brother',
    'children': 'child', 'men': 'man', 'women': 'woman',
}

_FUNC_EN = set('''
a an the this that these those there here
and or but nor for yet so as if then than when while because since though although
of in on at to unto into onto from by with within without upon over under
through throughout among between about against before after above below toward towards
i me my mine we us our ours you your yours thou thee thy thine ye
he him his she her hers it its they them their theirs one
who whom whose which what where how why
is are was were am be been being art
have has had hath hast having
do does did doth dost done
shall will would should shalt wilt may might can could must let
not no never none neither nothing
all any some every each both very own same such other another
o oh lo behold indeed also too only just even still now
yea nay verily thus alas whether
whoever whosoever whomever whatever whenever wherever
everyone everybody anyone anybody someone somebody everything anything something
'''.split())


def _stem_en(w):
    """아주 가벼운 어미 떼기. 어미만 다른 낱말을 같은 것으로 보기 위한 것."""
    w = _ARCH_MAP.get(w, w)
    if len(w) > 4 and w.endswith('eth'):
        w = w[:-3]
    elif len(w) > 4 and w.endswith('est'):
        w = w[:-3]
    elif len(w) > 4 and w.endswith('ing'):
        w = w[:-3]
    elif len(w) > 4 and w.endswith('ies'):
        w = w[:-3] + 'y'
    elif len(w) > 3 and w.endswith('ed'):
        w = w[:-2]
    elif len(w) > 4 and w.endswith('es'):
        w = w[:-2]
    elif len(w) > 3 and w.endswith('s') and not w.endswith('ss'):
        w = w[:-1]
    if len(w) > 3 and w[-1] == w[-2] and w[-1] in 'bdgklmnprt':
        w = w[:-1]                      # running → runn → run
    if len(w) > 2 and w.endswith('i'):
        w = w[:-1] + 'y'                # cried → cri → cry
    return _ARCH_MAP.get(w, w)


def _content_set(s):
    """기능어(관사·조동사·전치사·대명사)를 뺀 '알맹이 낱말' 모음."""
    out = set()
    for w in _norm_en(s).split():
        w = w.replace("'", '')
        w = _ARCH_MAP.get(w, w)
        if not w or w in _FUNC_EN:
            continue
        w = _stem_en(w)
        if w and w not in _FUNC_EN:
            out.add(w)
    return out


def _meaningful_en(sa, sb):
    """두 표현이 '뜻·낱말 선택'까지 분명히 다른지.
    어미·조동사·관사만 다르면 False (칠하지 않는다)."""
    ca, cb = _content_set(sa), _content_set(sb)
    if not ca and not cb:
        return False                    # 양쪽 다 기능어뿐 → 조동사·관사 차이
    if ca == cb:
        return False                    # 알맹이 낱말이 같음 → 어미·문체만 다름
    return True


def mark_rows_en(rows, a_key='NKJV', b_key='NASB'):
    """절마다 영어 두 칸을 대조해 '뜻이 분명히 다른 자리'만 표시자로 감싼다.
    (한국어 번역본은 칠하지 않는다 — 어미 차이가 너무 많아 어지럽기 때문)"""
    if not a_key or not b_key or a_key == b_key:
        return rows                      # 영어를 한 칸만 골랐으면 대조할 짝이 없다
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        a = str(r.get(a_key) or '').strip()
        b = str(r.get(b_key) or '').strip()
        if not a or not b:
            continue
        # 묶음 전체로도, 그 안의 한 자리로도 '뜻이 다르다'고 판정될 때만 칠한다
        pairs = [(ra, rb) for ra, rb, ga, gb in auto_pairs(a, b)
                 if _meaningful_en(ga, gb) and _meaningful_en(a[ra[0]:ra[1]], b[rb[0]:rb[1]])]
        if not pairs:
            continue
        # 양쪽 동시에 겹침 제거 → 한쪽만 남는 하이라이트가 생기지 않는다
        pairs.sort(key=lambda p: (p[0][0], p[1][0]))
        keep, la, lb = [], -1, -1
        for ra, rb in pairs:
            if ra[0] < la or rb[0] < lb:
                continue
            keep.append((ra, rb))
            la, lb = ra[1], rb[1]
            if len(keep) >= 8:
                break
        if not keep:
            continue
        r[a_key] = _wrap_ranges(a, [(ra[0], ra[1], b[rb[0]:rb[1]]) for ra, rb in keep])
        r[b_key] = _wrap_ranges(b, [(rb[0], rb[1], a[ra[0]:ra[1]]) for ra, rb in keep])
    return rows


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
# 자주 쓰는 다른 표기도 함께 알아듣게 (사람마다 적는 방식이 달라서).
KO_BOOKS.update({
    '창세': 'genesis', '출애굽': 'exodus', '출애': 'exodus', '레위': 'leviticus',
    '민수': 'numbers', '신명': 'deuteronomy', '여호수아서': 'joshua', '사사': 'judges',
    '룻': 'ruth', '느헤미아': 'nehemiah', '에스더서': 'esther', '욥': 'job',
    '시편': 'psalms', '잠언서': 'proverbs', '전도': 'ecclesiastes',
    '아가서': 'song of solomon', '이사야서': 'isaiah', '예레미야서': 'jeremiah',
    '애가서': 'lamentations', '에스겔서': 'ezekiel', '다니엘서': 'daniel',
    '마태': 'matthew', '마가': 'mark', '누가': 'luke', '요한': 'john',
    '사도': 'acts', '로마': 'romans', '갈라디아': 'galatians', '에베소': 'ephesians',
    '빌립보': 'philippians', '골로새': 'colossians', '디도': 'titus',
    '빌레몬': 'philemon', '히브리': 'hebrews', '야고보': 'james',
    '유다': 'jude', '계시': 'revelation', '요한계시': 'revelation',
    '고린도전': '1 corinthians', '고린도후': '2 corinthians',
    '데살로니가전': '1 thessalonians', '데살로니가후': '2 thessalonians',
    '디모데전': '1 timothy', '디모데후': '2 timothy',
    '베드로전': '1 peter', '베드로후': '2 peter',
    '요한일': '1 john', '요한이': '2 john', '요한삼': '3 john',
    '사무엘상서': '1 samuel', '사무엘하서': '2 samuel',
})
# 긴 이름부터 매칭해야 '요한복음'이 '요'로 잘리지 않는다.
_BOOK_KEYS = sorted(KO_BOOKS.keys(), key=len, reverse=True)

# 영어로 적는 사람도 있으니 흔한 약어를 알아듣게 (matt 4:3, jn 3:16, 1 cor 13 …)
EN_BOOKS = {
    'gen': 'genesis', 'ge': 'genesis', 'gn': 'genesis',
    'exo': 'exodus', 'exod': 'exodus', 'ex': 'exodus',
    'lev': 'leviticus', 'lv': 'leviticus', 'le': 'leviticus',
    'num': 'numbers', 'nu': 'numbers', 'nm': 'numbers', 'nb': 'numbers',
    'deut': 'deuteronomy', 'deu': 'deuteronomy', 'dt': 'deuteronomy',
    'josh': 'joshua', 'jos': 'joshua', 'jsh': 'joshua',
    'judg': 'judges', 'jdg': 'judges', 'jg': 'judges',
    'rut': 'ruth', 'rth': 'ruth', 'ru': 'ruth',
    '1sam': '1 samuel', '1sa': '1 samuel', '1sm': '1 samuel',
    '2sam': '2 samuel', '2sa': '2 samuel', '2sm': '2 samuel',
    '1kings': '1 kings', '1kgs': '1 kings', '1ki': '1 kings', '1kin': '1 kings',
    '2kings': '2 kings', '2kgs': '2 kings', '2ki': '2 kings', '2kin': '2 kings',
    '1chron': '1 chronicles', '1chr': '1 chronicles', '1ch': '1 chronicles',
    '2chron': '2 chronicles', '2chr': '2 chronicles', '2ch': '2 chronicles',
    'ezr': 'ezra', 'neh': 'nehemiah', 'ne': 'nehemiah',
    'est': 'esther', 'esth': 'esther',
    'jb': 'job',
    'ps': 'psalms', 'psa': 'psalms', 'psalm': 'psalms', 'psm': 'psalms', 'pss': 'psalms',
    'prov': 'proverbs', 'pro': 'proverbs', 'prv': 'proverbs', 'pr': 'proverbs',
    'eccl': 'ecclesiastes', 'ecc': 'ecclesiastes', 'ec': 'ecclesiastes', 'qoh': 'ecclesiastes',
    'song': 'song of solomon', 'songofsongs': 'song of solomon', 'sos': 'song of solomon',
    'cant': 'song of solomon', 'ss': 'song of solomon',
    'isa': 'isaiah', 'is': 'isaiah',
    'jer': 'jeremiah', 'je': 'jeremiah',
    'lam': 'lamentations', 'la': 'lamentations',
    'ezek': 'ezekiel', 'eze': 'ezekiel', 'ezk': 'ezekiel',
    'dan': 'daniel', 'dn': 'daniel', 'da': 'daniel',
    'hos': 'hosea', 'ho': 'hosea',
    'joe': 'joel', 'jl': 'joel',
    'amo': 'amos', 'am': 'amos',
    'obad': 'obadiah', 'ob': 'obadiah',
    'jon': 'jonah', 'jnh': 'jonah',
    'mic': 'micah', 'mi': 'micah',
    'nah': 'nahum', 'na': 'nahum',
    'hab': 'habakkuk', 'hb': 'habakkuk',
    'zeph': 'zephaniah', 'zep': 'zephaniah', 'zph': 'zephaniah',
    'hag': 'haggai', 'hg': 'haggai',
    'zech': 'zechariah', 'zec': 'zechariah', 'zch': 'zechariah',
    'mal': 'malachi', 'ml': 'malachi',
    'matt': 'matthew', 'mat': 'matthew', 'mt': 'matthew',
    'mrk': 'mark', 'mk': 'mark', 'mr': 'mark',
    'luk': 'luke', 'lk': 'luke',
    'joh': 'john', 'jhn': 'john', 'jn': 'john',
    'act': 'acts', 'ac': 'acts',
    'rom': 'romans', 'ro': 'romans', 'rm': 'romans',
    '1cor': '1 corinthians', '1co': '1 corinthians',
    '2cor': '2 corinthians', '2co': '2 corinthians',
    'gal': 'galatians', 'ga': 'galatians',
    'eph': 'ephesians', 'ephes': 'ephesians',
    'phil': 'philippians', 'php': 'philippians', 'pp': 'philippians',
    'col': 'colossians', 'cl': 'colossians',
    '1thess': '1 thessalonians', '1thes': '1 thessalonians', '1th': '1 thessalonians',
    '2thess': '2 thessalonians', '2thes': '2 thessalonians', '2th': '2 thessalonians',
    '1tim': '1 timothy', '1ti': '1 timothy', '1tm': '1 timothy',
    '2tim': '2 timothy', '2ti': '2 timothy', '2tm': '2 timothy',
    'tit': 'titus', 'ti': 'titus',
    'philem': 'philemon', 'phm': 'philemon', 'pm': 'philemon',
    'heb': 'hebrews', 'hbr': 'hebrews',
    'jas': 'james', 'jam': 'james', 'jm': 'james',
    '1pet': '1 peter', '1pe': '1 peter', '1pt': '1 peter', '1p': '1 peter',
    '2pet': '2 peter', '2pe': '2 peter', '2pt': '2 peter', '2p': '2 peter',
    '1joh': '1 john', '1jn': '1 john', '1jo': '1 john', '1j': '1 john',
    '2joh': '2 john', '2jn': '2 john', '2jo': '2 john', '2j': '2 john',
    '3joh': '3 john', '3jn': '3 john', '3jo': '3 john', '3j': '3 john',
    'jud': 'jude', 'jd': 'jude',
    'rev': 'revelation', 'rv': 'revelation', 'apoc': 'revelation',
}

# 1·2·3권을 적는 여러 방식을 숫자로 통일 (First John / I John / 1st John …)
_ORD = [
    (r'^(?:1st|first|i)\s+', '1 '), (r'^(?:2nd|second|ii)\s+', '2 '),
    (r'^(?:3rd|third|iii)\s+', '3 '),
]


def _en_book(raw):
    """영어로 적은 책 이름 → bolls가 아는 이름. 못 알아들으면 None."""
    t = re.sub(r'[.\s]+', ' ', str(raw or '')).strip().lower()
    for pat, rep in _ORD:
        t = re.sub(pat, rep, t)
    if t in BOLLS_BOOK:
        return t
    flat = t.replace(' ', '')
    if flat in EN_BOOKS:
        return EN_BOOKS[flat]
    # 'matthew4' 처럼 붙여 쓴 경우까지는 위에서 걸러지므로 여기선 부분 일치만 본다
    for full in BOLLS_BOOK:
        if full.replace(' ', '') == flat:
            return full
    return None


# 장·절을 적는 온갖 방식을 하나로 다듬는다
_DASHES = '～~–—−∼﹘－ー'
_SEPS = '.,;·､、/'


def _clean_nums(s):
    s = re.sub(r'\s+', '', str(s or ''))
    for d in _DASHES:
        s = s.replace(d, '-')
    s = s.replace('장', ':').replace('편', ':').replace('절', '').replace('항', '')
    s = s.replace('제', '').replace('篇', ':').replace('章', ':')
    for c in _SEPS:
        s = s.replace(c, ':')
    s = re.sub(r':{2,}', ':', s)
    s = re.sub(r'-{2,}', '-', s)
    return s.strip(':-')


_LIST_SEPS = ',，、､'


def parse_parts(passage):
    """'요한복음 3:7,16-17' → ('john', 3, [7, 16, 17]).
    쉼표로 끊긴, 이어지지 않는 절들을 그대로 뽑아 준다.
    쉼표가 없거나 한 글자라도 못 알아들으면 None → 지금까지 하던 방식(parse_ref)으로 돌아간다."""
    raw = str(passage or '').strip()
    if not raw:
        return None
    s = re.sub(r'\s+', '', raw)
    book = None
    for k in _BOOK_KEYS:
        if s.startswith(k):
            book = KO_BOOKS[k]
            s = s[len(k):]
            break
    if not book:
        m = re.match(r'^([1-3]?\s*[a-zA-Z][a-zA-Z.\s]*)', raw)
        if not m:
            return None
        book = _en_book(m.group(1))
        if not book:
            return None
        s = re.sub(r'\s+', '', raw[m.end():])
    if not any(c in s for c in _LIST_SEPS):
        return None                      # 쉼표가 없으면 예전 방식 그대로
    for c in _LIST_SEPS[1:]:
        s = s.replace(c, ',')
    for d in _DASHES:
        s = s.replace(d, '-')
    s = s.replace('장', ':').replace('편', ':').replace('篇', ':').replace('章', ':')
    s = s.replace('절', '').replace('항', '').replace('제', '')
    s = re.sub(r'[.;·/]', ':', s)
    s = re.sub(r':{2,}', ':', s)
    s = re.sub(r'-{2,}', '-', s)
    s = s.strip(':-,')
    m = re.match(r'^(\d+):(.+)$', s)
    if not m:
        return None
    ch = int(m.group(1))
    nums = []
    for part in m.group(2).split(','):
        part = part.strip(':-')
        if not part:
            continue
        mm = re.match(r'^(\d+)-(\d+)$', part)
        if mm:
            a, b = int(mm.group(1)), int(mm.group(2))
            if b < a:
                a, b = b, a
            nums.extend(range(a, min(b, a + 80) + 1))
            continue
        mm = re.match(r'^(\d+)$', part)
        if mm:
            nums.append(int(mm.group(1)))
            continue
        return None                      # 못 알아들으면 통째로 포기 (엉뚱한 절을 주는 것보다 낫다)
    nums = sorted(set(n for n in nums if 1 <= n <= 200))
    if len(nums) < 2:
        return None
    return (book, ch, nums[:80])


def parse_ref(passage):
    """'여호수아 4:6-7', '수 4:6~7', '요한복음 3장 16절', '마태복음 4장',
    '시편 23편 1-3절', 'Matt 4:3-11' → ('joshua', 4, 6, 7). 해석 못 하면 None.
    절이 없으면 (책, 장, None, None) = 그 장 전체."""
    raw = str(passage or '').strip()
    if not raw:
        return None
    s = re.sub(r'\s+', '', raw)
    book = None
    for k in _BOOK_KEYS:
        if s.startswith(k):
            book = KO_BOOKS[k]
            s = s[len(k):]
            break
    if not book:
        m = re.match(r'^([1-3]?\s*[a-zA-Z][a-zA-Z.\s]*)', raw)
        if not m:
            return None
        book = _en_book(m.group(1))
        if not book:
            return None
        s = raw[m.end():]
    s = _clean_nums(s)

    # 장:절-장:절 (장을 넘어가는 범위) → 첫 장의 그 절부터 장 끝까지
    m = re.match(r'^(\d+):(\d+)-(\d+):(\d+)$', s)
    if m:
        return (book, int(m.group(1)), int(m.group(2)), 200)
    # 장:절-절
    m = re.match(r'^(\d+):(\d+)-(\d+)$', s)
    if m:
        c, a, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return (book, c, a, b if b >= a else a)
    # 장:절
    m = re.match(r'^(\d+):(\d+)$', s)
    if m:
        v = int(m.group(2))
        return (book, int(m.group(1)), v, v)
    # 장만 (마태복음 4 / 마태복음 4장 / 시편 23편)
    m = re.match(r'^(\d+)$', s)
    if m:
        return (book, int(m.group(1)), None, None)
    # 장-장 (마태복음 4-5) → 첫 장 전체
    m = re.match(r'^(\d+)-(\d+)$', s)
    if m:
        return (book, int(m.group(1)), None, None)
    # 앞에서 못 맞췄어도 맨 앞 숫자 두 개만 살려서 최대한 알아듣는다
    nums = re.findall(r'\d+', s)
    if len(nums) >= 2:
        c, a = int(nums[0]), int(nums[1])
        b = int(nums[2]) if len(nums) >= 3 and int(nums[2]) >= a else a
        return (book, c, a, b)
    if len(nums) == 1:
        return (book, int(nums[0]), None, None)
    return None


# ── 📖 실제 번역본 본문 가져오기 (bolls.life) ─────────────────────────────
# ★이 앱의 핵심★ 번역본 본문은 AI 기억에서 꺼내지 않는다. 실제 성경 본문 제공처에서
# 그대로 받아온다. AI는 단어·원어·배경 설명에만 쓴다.
#   GET https://bolls.life/get-text/{번역본}/{책번호}/{장}/  → [{"verse":1,"text":"..."}, ...]
# 책번호는 창세기=1 … 요한계시록=66. 키(API key) 필요 없음.
# 서버가 작으니 전체 성경을 긁지 말라는 안내가 있어, 요청한 장만 받고 결과는 캐시한다.

_BOOK_ORDER = [
    'genesis', 'exodus', 'leviticus', 'numbers', 'deuteronomy', 'joshua', 'judges', 'ruth',
    '1 samuel', '2 samuel', '1 kings', '2 kings', '1 chronicles', '2 chronicles', 'ezra',
    'nehemiah', 'esther', 'job', 'psalms', 'proverbs', 'ecclesiastes', 'song of solomon',
    'isaiah', 'jeremiah', 'lamentations', 'ezekiel', 'daniel', 'hosea', 'joel', 'amos',
    'obadiah', 'jonah', 'micah', 'nahum', 'habakkuk', 'zephaniah', 'haggai', 'zechariah',
    'malachi', 'matthew', 'mark', 'luke', 'john', 'acts', 'romans', '1 corinthians',
    '2 corinthians', 'galatians', 'ephesians', 'philippians', 'colossians',
    '1 thessalonians', '2 thessalonians', '1 timothy', '2 timothy', 'titus', 'philemon',
    'hebrews', 'james', '1 peter', '2 peter', '1 john', '2 john', '3 john', 'jude',
    'revelation',
]
BOLLS_BOOK = dict((n, i + 1) for i, n in enumerate(_BOOK_ORDER))

# 고를 수 있는 번역본
# ※ bolls의 'RNKSV'는 이름만 새번역이고 내용은 개역한글과 같아서 쓰지 않는다(직접 대조해 확인).
# 한국어는 개역한글만 '받아온 그대로'이고, 개역개정·새번역은 공개 API가 없어
# AI가 쓰되 받아온 개역한글과 절 단위로 대조해 검증한다.
KO_CHOICES = ('개역개정', '새번역', '개역한글')
KO_VERBATIM = ('개역한글',)                      # 받아온 그대로인 한국어 칸
EN_CHOICES = ('NIV', 'ESV', 'AMP', 'KJV', 'NKJV', 'NASB')
EN_BOLLS = {'NIV': 'NIV', 'ESV': 'ESV', 'AMP': 'AMP',
            'KJV': 'KJV', 'NKJV': 'NKJV', 'NASB': 'NASB'}
DEF_KO = ('개역개정', '새번역')
DEF_EN = ('NKJV', 'ESV')


def pick_two(want, allowed, default):
    """고른 번역본을 다듬는다. 모르는 이름·중복은 버리고, 비면 기본값. 최대 2개."""
    out = []
    for x in (want if isinstance(want, list) else []):
        x = str(x or '').strip()
        if x in allowed and x not in out:
            out.append(x)
        if len(out) >= 2:
            break
    return out or list(default)


def norm_sel(body):
    """요청에서 '고른 번역본'을 꺼낸다 → (한국어 목록, 영어 목록)."""
    t = body.get('tr') if isinstance(body, dict) else None
    t = t if isinstance(t, dict) else {}
    return (pick_two(t.get('ko'), KO_CHOICES, DEF_KO),
            pick_two(t.get('en'), EN_CHOICES, DEF_EN))


def bolls_tr(en_list):
    """실제로 받아올 것들. 개역한글은 검증의 '자'라서 늘 받아온다."""
    return [('개역한글', 'KRV')] + [(e, EN_BOLLS[e]) for e in en_list if e in EN_BOLLS]

_TAG = re.compile(r'<[^>]+>')
_STRONG = re.compile(r'<S>\d+</S>', re.I)


def _html_text(s):
    """bolls가 주는 본문은 HTML이라 태그·원어번호를 걷어내고 순수 문장만 남긴다."""
    s = str(s or '')
    s = _STRONG.sub(' ', s)
    s = re.sub(r'<br\s*/?>', ' ', s, flags=re.I)
    s = _TAG.sub('', s)
    s = _htmlmod.unescape(s)
    s = s.replace(' ', ' ')
    return re.sub(r'\s+', ' ', s).strip()


def _bolls_chapter(tr_code, book_id, chapter):
    """한 번역본의 한 장을 통째로 받아 {절번호: 본문} 으로 돌려준다. 실패하면 {}."""
    url = 'https://bolls.life/get-text/%s/%d/%d/' % (tr_code, book_id, chapter)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'mom-bakery/1.0',
                                                   'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=12) as r:
            rows = json.loads(r.read().decode('utf-8', 'ignore'))
    except Exception:
        return {}
    out = {}
    if isinstance(rows, list):
        for it in rows:
            if not isinstance(it, dict):
                continue
            n = _vnum(it.get('verse'))
            t = _html_text(it.get('text'))
            if n and t:
                out[n] = t
    return out


def fetch_scripture(passage, en_list=None):
    """요청한 본문을 고른 번역본 '실제 문장 그대로' 받아온다.
    돌려주는 값: {'rows': [{'n':6, '개역한글':..., '새번역':..., 'NKJV':..., 'NASB':...}, ...],
                  'ref': '여호수아 4:6-8', 'got': ['NKJV', ...]}
    하나도 못 받으면 None."""
    parts = parse_parts(passage)          # '3:7,16-17'처럼 이어지지 않는 절
    if parts:
        book, ch, pick = parts
        v1 = v2 = None
    else:
        pick = None
        ref = parse_ref(passage)
        if not ref:
            return None
        book, ch, v1, v2 = ref
    book_id = BOLLS_BOOK.get(book)
    if not book_id:
        return None
    want = bolls_tr(list(en_list) if en_list else list(DEF_EN))

    with ThreadPoolExecutor(max_workers=4) as ex:      # 여러 번역본 동시에 → 빠르게
        futs = [(label, ex.submit(_bolls_chapter, code, book_id, ch)) for label, code in want]
        chapters = []
        for label, f in futs:
            try:
                chapters.append((label, f.result(timeout=15)))
            except Exception:
                chapters.append((label, {}))

    got = [label for label, d in chapters if d]
    if not got:
        return None

    # 요청한 절 범위 정하기 (장 전체면 받아온 절 전부)
    if pick:
        nums = list(pick)
    elif v1:
        nums = list(range(v1, (v2 or v1) + 1))
    else:
        allv = set()
        for _, d in chapters:
            allv |= set(d.keys())
        nums = sorted(allv)
    nums = nums[:80]      # 너무 긴 장은 잘라 화면·비용을 지킨다

    rows = []
    for n in nums:
        row = {'n': n}
        for label, d in chapters:
            row[label] = d.get(n, '')
        if any(row[label] for label, _ in want):
            rows.append(row)
    if not rows:
        return None

    r = passage.strip()
    en1 = (list(en_list) if en_list else list(DEF_EN))[0]
    return {'rows': rows, 'ref': r, 'got': got, 'en': [e for e, _ in want[1:]], 'en1': en1,
            'nums': [x['n'] for x in rows],
            'byv': dict((x['n'], x.get(en1) or '') for x in rows)}


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
        req = urllib.request.Request(url, headers={'User-Agent': 'mom-bakery/1.0'})
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


_IRREG = {
    'gave': 'give', 'given': 'give', 'went': 'go', 'gone': 'go', 'came': 'come',
    'said': 'say', 'saw': 'see', 'seen': 'see', 'made': 'make', 'took': 'take',
    'taken': 'take', 'told': 'tell', 'held': 'hold', 'kept': 'keep', 'left': 'leave',
    'brought': 'bring', 'built': 'build', 'sent': 'send', 'stood': 'stand',
    'spoke': 'speak', 'spoken': 'speak', 'knew': 'know', 'known': 'know',
    'fell': 'fall', 'fallen': 'fall', 'laid': 'lay', 'led': 'lead',
    'men': 'man', 'women': 'woman', 'children': 'child', 'feet': 'foot',
}


def _stem(w):
    """'stretched' → 'stretch', 'gives' → 'giv'. 같은 낱말의 다른 꼴을 한 덩이로 묶기 위한 어림 어간."""
    w = re.sub(r'[^a-z]', '', str(w or '').lower())
    if not w:
        return ''
    if w in _IRREG:
        w = _IRREG[w]
    else:
        for suf, rep in (('ies', 'y'), ('ied', 'y'), ('ing', ''), ('ed', ''), ('s', '')):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[:len(w) - len(suf)] + rep
                break
        if len(w) >= 4 and w[-1] == w[-2] and w[-1] not in 'aeiou':
            w = w[:-1]
    if len(w) >= 4 and w.endswith('e'):
        w = w[:-1]
    return w


def _word_key(s):
    """'Stretch out'과 'stretched out'을 같은 이름표로 만든다."""
    toks = [t for t in re.split(r'[^A-Za-z]+', str(s or '')) if t]
    return ' '.join(_stem(t) for t in toks if _stem(t))


def dedupe_words(words):
    """같은 영어 낱말이 꼴만 바꿔 두 번 나오는 것을 지운다 (먼저 나온 것을 남긴다)."""
    if not isinstance(words, list):
        return words
    seen, out = set(), []
    for w in words:
        if not isinstance(w, dict):
            continue
        k = _word_key(w.get('english'))
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(w)
    return out


def dedupe_originals(items):
    """같은 원어(Strong's 번호 또는 원어 철자)가 두 번 나오는 것을 지운다."""
    if not isinstance(items, list):
        return items
    seen, out = set(), []
    for it in items:
        if not isinstance(it, dict):
            continue
        k = re.sub(r'\s+', '', str(it.get('strong') or '')).upper() \
            or re.sub(r'\s+', '', str(it.get('original') or ''))
        if k and k in seen:
            continue
        if k:
            seen.add(k)
        out.append(it)
    return out


def _content_words(s):
    w = re.findall(r'[a-z]+', str(s or '').lower())
    return set(x for x in w if len(x) > 2 and x not in _STOP_EN)


# 예비 경로(본문 제공처가 응답하지 않을 때)에서 쓰는 기본 칸 순서.
# 평소에는 사용자가 고른 번역본 목록(ko_list + en_list)이 이 자리를 대신한다.
TR_KEYS = DEF_KO + DEF_EN


def _vnum(x):
    s = re.sub(r'\D', '', str(x if x is not None else ''))
    return int(s) if s else None


def _esc(s):
    """화면에 그대로 넣을 본문이므로 HTML 특수문자를 막아 둔다."""
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


# ── 한국어 절 정렬 검증 ────────────────────────────────────────────────
# 개역개정·새번역은 저작권 때문에 공개 API가 없어 AI가 쓴다. 대신 '실제로 받아온 개역한글'을
# 자로 삼아, AI가 6절 자리에 5절 내용을 써 넣는 식의 밀림을 잡아낸다.
# 판정 방법: 그 문장이 이웃 절(±1, ±2)보다 그 절과 더 닮았는가 (한글 2글자 조각 겹침).
def _ko_grams(s):
    s = re.sub(r'[^가-힣]', '', str(s or ''))
    return set(s[i:i + 2] for i in range(len(s) - 1))


def _sim(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def ko_align_ok(text, byv_ko, n):
    """AI가 쓴 n절 한국어 문장이 정말 n절 자리인지 판정."""
    if not byv_ko or n not in byv_ko:
        return True                       # 대조할 실제 본문이 없으면 판정 보류
    g = _ko_grams(text)
    if len(g) < 6:
        return True                       # 너무 짧은 절은 판정 불가
    here = _sim(g, _ko_grams(byv_ko.get(n)))
    if here < 0.22:
        return False                      # 그 절과 너무 안 닮음 = 다른 본문
    for m in (n - 2, n - 1, n + 1, n + 2):
        if m in byv_ko and _sim(g, _ko_grams(byv_ko[m])) > here + 0.05:
            return False                  # 이웃 절과 더 닮음 = 한 절씩 밀려 씀
    return True


def merge_ai_korean(rows, ai_verses, byv_ko, ai_ko=None):
    """받아온 실제 본문 rows에 AI가 쓴 한국어 번역본(개역개정·새번역)을 절 번호로 붙인다.
    한 절이라도 검증에 걸리면 그 칸은 통째로 비운다 (틀린 본문을 보여 주느니 안 보여 준다).
    개역한글은 이미 받아온 그대로라 여기서 건드리지 않는다."""
    ai_ko = list(ai_ko) if ai_ko is not None else list(DEF_KO)
    if not ai_ko:
        return rows, []
    byn = {}
    for v in (ai_verses or []):
        if not isinstance(v, dict):
            continue
        n = _vnum(v.get('n'))
        if n is not None:
            byn[n] = v
    bad = set()
    for r in rows:
        v = byn.get(r['n']) or {}
        for k in ai_ko:
            t = re.sub(r'\s+', ' ', str(v.get(k) or '')).strip()
            if not t:
                bad.add(k)
                continue
            if not ko_align_ok(t, byv_ko, r['n']):
                bad.add(k)
                continue
            r[k] = t
    for r in rows:
        for k in bad:
            r.pop(k, None)
    return rows, [k for k in ai_ko if k not in bad]


def verify_phrases(data, sc, en1=None):
    """originals의 phrases가 '실제 본문에 그대로 있는 어구'인지 확인한다.
    없는 어구는 지운다 — 엉뚱한 자리를 칠하는 것보다 안 칠하는 게 낫다."""
    ko_all = ' '.join(str(r.get('개역한글') or '') for r in (sc or {}).get('rows', []))
    en1 = en1 or (sc or {}).get('en1') or DEF_EN[0]
    en_all = ' '.join(str(r.get(en1) or '') for r in (sc or {}).get('rows', []))
    en_low = en_all.lower()
    for o in (data.get('originals') or []):
        if not isinstance(o, dict):
            continue
        ph = o.get('phrases')
        if not isinstance(ph, dict):
            o.pop('phrases', None)
            continue
        ko = re.sub(r'\s+', ' ', str(ph.get('ko') or '')).strip()
        en = re.sub(r'\s+', ' ', str(ph.get('en') or '')).strip()
        if ko and ko not in re.sub(r'\s+', ' ', ko_all):
            ko = ''
        if en and en.lower() not in re.sub(r'\s+', ' ', en_low):
            en = ''
        if ko or en:
            o['phrases'] = {'ko': ko, 'en': en}
        else:
            o.pop('phrases', None)
    return data


def rows_to_translations(rows, order=None):
    """절별 본문 배열 → 화면용 translations. 절 번호를 윗첨자로 붙이고 절마다 줄을 바꾼다.
    order = 화면에 보여 줄 칸 이름 순서 (고른 한국어 + 고른 영어)."""
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get('n')]
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r['n'])
    tr = {}
    for k in (list(order) if order else list(TR_KEYS)):
        parts = ['<sup class="vn">%d</sup>%s' % (r['n'], mark_highlights(_esc(r.get(k))))
                 for r in rows if str(r.get(k) or '').strip()]
        if parts:
            tr[k] = ' <br>'.join(parts)      # 절마다 줄을 바꿔서 보여 준다
    return tr or None


def scripture_block(sc):
    """AI에게 넘길 '실제 본문' 블록. AI는 이 본문만 보고 단어·원어·배경을 설명한다."""
    if not sc:
        return ''
    en1 = sc.get('en1') or DEF_EN[0]
    lines = []
    for r in sc['rows'][:40]:
        ko = r.get('개역한글') or ''
        en = r.get(en1) or ''
        lines.append('%d. %s / %s' % (r['n'], ko, en))
    return '\n'.join(lines)


def assemble(data):
    """AI가 준 verses(절별 배열) → 화면용 translations.
    절 번호를 윗첨자로 붙이고 절마다 줄을 바꾼다 (하이라이트 없음, 본문 그대로)."""
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
            tr[k] = ' <br>'.join(parts)      # 절마다 줄을 바꿔서 보여 준다
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
        got_w = set()
        for _k in EN_CHOICES:                    # 고른 영어 칸이 무엇이든 다 본다
            if r.get(_k):
                got_w |= _content_words(r.get(_k))
        if not got_w:
            return False
        if len(base & got_w) / float(len(base)) < 0.4:
            return False                         # 그 절 자리에 다른 절 내용이 들어옴
        checked += 1
    return checked > 0 or not byv


_ARCHAIC = re.compile(r'\b(ye|thou|thy|thee|hath|doth|saith|hither|unto|'
                      r'\w+eth)\b', re.I)


def archaic_bad(data):
    """현대 영어 번역본 자리에 KJV 옛 문체가 들어왔는지. (KJV 자체는 옛 문체가 맞으므로 제외)"""
    tr = (data or {}).get('translations') or {}
    for k in [x for x in EN_CHOICES if x != 'KJV']:
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
      "english": "★본문에 나온 꼴 그대로★의 영어 단어 하나 또는 굳어진 표현/숙어. 원형으로 되돌리지 마라 (본문이 heard면 heard, commanded면 commanded). 문장·해석 금지 (예: eternal, perish, heard, commanded, lay down).",
      "verse": 16,
      "korean": "그 꼴에 맞는 짧은 한국어 (heard면 '듣다'가 아니라 '들었다', commanded면 '명령했다')",
      "pos": "품사. 꼴이 바뀐 낱말이면 괄호로 밝혀라 (예: '동사(과거형)', '동사(과거분사)', '명사(복수형)', '형용사(비교급)'). 원형 그대로면 그냥 '동사'처럼 적는다.",
      "meaning": "★꼴이 바뀐 낱말이면 맨 앞에 '원형(한국어 뜻)의 ○○형.' 을 먼저 적어라 (예 heard → 'hear(듣다)의 과거형. 들었다, 전해 들었다, 알게 되었다'). 그다음★ 표준 영어사전(옥스퍼드·메리엄웹스터 급)에서 흔한 뜻부터 차례로 한국어로 2~4개, 쉼표 구분. 이 본문에서 쓰인 뜻만 적지 말고 그 뜻도 목록 안에 함께 넣어라. 의역·직역·성경식 풀이 금지. (예 so: '매우, 많이, 이 정도로, 이렇게')",
      "nuance": "먼저 '보통 영어에서의 어감·용법' + ★일상 예문 하나★ (한국어 해석 포함), 그다음 필요하면 특수한 상황에서의 쓰임을 한 문장. 예문에 하나님·성경 인물·성경 사건을 넣지 마라. (예 so: '정도를 강조하는 강조부사. too가 부정적 과함을 뜻하는 것과 달리 so는 주로 긍정적 강조에 쓴다. 예: It''s so beautiful! 정말 아름다워)"
    }}
  ],
  "originals": [
    {{
      "strong": "Strong's 번호 (히브리어면 H로 시작 예:H7462, 헬라어면 G로 시작 예:G26)",
      "original": "헬라어/히브리어 원어",
      "reading": "음역 (예: 로이)",
      "korean": "해당하는 한국어 단어",
      "where": "★이 원어가 '이 본문 안에서' 나오는 자리. 반드시 절 번호와 그 자리의 한국어 표현을 함께 적어라. 예: 3절 '거룩하게 하시고' / 12절 '높이시며'. 여러 절에 나오면 쉼표로 두 곳까지.",
      "phrases": {{
        "ko": "★위에 준 개역한글 본문에서, 이 원어가 번역된 자리를 '글자 그대로' 오려낸 짧은 어구 (2~5어절)",
        "en": "★위에 준 NKJV 본문에서, 같은 자리를 '글자 그대로' 오려낸 짧은 어구 (2~5 words)"
      }},
      "core": "★바이블허브 Strong's 항목의 '어원·기본 뜻' 한 줄을 그대로. 영어 원문 + ' — ' + 한국어 직역. 네 말로 새로 짓지 마라. (예 λύω G3089: 'to loose, untie, release — 풀다, 끄르다, 놓아주다')",
      "senses": [
        {{
          "n": 1,
          "en": "★바이블허브에 실린 사전(헬라어=Thayer's, 히브리어=Brown-Driver-Briggs)의 번호별 뜻을 영어 그대로 옮긴 것. 요약·의역·창작 금지. (예: 'to arouse from sleep, to awake')",
          "ko": "그 영어 뜻의 한국어 직역만. 설명을 덧붙이지 마라. (예: '잠에서 깨우다, 깨어나다')"
        }}
      ],
      "meaning": "Strong's Exhaustive Concordance에 실린 짧은 뜻 나열 한 줄, 영어 그대로 (예 egeiro: 'awake, lift up, raise again, rear up, stand, take up')",
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
  "heading": "이 본문이 속한 단락의 '새번역 성경 소제목'. 새번역(표준새번역 개정) 성경이 본문 위에 달아 놓은 단락 제목을 그대로 적어라 (예: '믿음이란 무엇인가', '광야에서 시험을 받으시다'). 요청한 절이 그 소제목 단락 안에 있거나 바로 그 단락이 시작되는 자리일 때만 적고, 단락이 멀거나 확실하지 않으면 빈 문자열로 두어라. 지어내지 마라.",
  "background": "이 본문의 배경. 표준 주석에 근거해 3~4문장으로, 반드시 다음 세 가지를 모두 담아라 — (1) 이 본문이 쓰인 시대와 당시 상황, (2) 이 책을 쓴 저자가 누구인지, (3) 저자가 이 본문을 누구에게 왜 썼는지(집필 의도)."
}}

words 규칙 (★"영어 사전"이라고 생각하고 뽑아라. 성경 해석이 아니다★):
- 표제(english)는 이 본문의 영어 본문(NKJV/NASB)에 실제로 나오는 "영어 단어 하나" 또는 "굳어진 표현/숙어"여야 한다. 문장 조각이나 해석(예: "believes in Him", "should not perish")은 절대 넣지 마라 — 그건 단어가 아니라 문장이다. 사전 표제어가 될 만한 것만: 낱말(everlasting, eternal, perish, begotten, so) 또는 숙어/구동사(lay down, abide in).
- ★같은 낱말은 절대 두 번 넣지 마라.★ 꼴만 바뀐 것(give / gave, command / commanded)은 한 항목으로만 넣고, 절이 여러 개면 처음 나오는 절 번호를 쓴다.
- ★★표제(english)는 본문에 나온 꼴 그대로 적어라. 원형으로 되돌리지 마라.★★
  본문이 heard면 heard, commanded면 commanded, stretched out이면 stretched out이다. 읽는 사람이 본문에서 눈으로 찾을 수 있어야 하기 때문이다.
  대신 그 꼴이 무엇의 변형인지를 반드시 알려 줘라 — korean은 그 꼴에 맞는 한국어로, pos에는 괄호로 무슨 형인지, meaning은 원형 설명으로 시작한다.
  - ✗ 나쁜 예: english "hear" / korean "듣다" / pos "동사" / meaning "듣다, 알다"
  - ✓ 좋은 예: english "heard" / korean "들었다" / pos "동사(과거형)"
      meaning "hear(듣다)의 과거형. 들었다, 전해 들었다, (소문·소식을) 알게 되었다"
  - ✓ 좋은 예: english "commanded" / korean "명령했다" / pos "동사(과거형)"
      meaning "command(명령하다)의 과거형. 명령했다, 지시했다, 시켰다"
  - ✓ 좋은 예: english "elders" / korean "장로들" / pos "명사(복수형)"
      meaning "elder(어른, 연장자)의 복수형. 어른들, 연장자들, (공동체의) 장로들"
  - 원형 그대로 나온 낱말(perish, covenant 같은 것)은 이 표기가 필요 없다. 그냥 평소대로 적어라.
- everlasting과 eternal처럼 서로 다른 단어는 하나로 묶지 말고 각각 따로 항목으로 넣어라. ("everlasting life / eternal life"처럼 합치지 마라.)
- meaning = 표준 영어사전의 사전적 정의(한국어) 2~3개. 문맥 의역·성경식 풀이 금지. 그 단어를 사전에서 찾으면 나오는 뜻을 적어라.
- nuance = 그 단어가 "영어에서 일상적으로 어떤 어감·용법으로 쓰이는지" + 예문 하나. 성경적 의미로 설명하지 마라. (예: so는 too와 달리 긍정적 강조에 쓴다는 식.)
- ★★가장 중요: 보편적인 뜻을 "빠짐없이" 알려 주고, 본문의 뜻은 그중 하나로 다뤄라.★★
  이 본문은 특수한 상황(전쟁·왕과 신하·제사 같은)일 수 있다. 그 상황에서의 뜻만 적으면 독자는 그 단어를 반쪽만 배우게 된다.
  meaning은 사전에서 흔한 뜻부터 차례로 2~4개를 적되, 본문에서 쓰인 뜻도 빼지 말고 그 목록 안에 함께 넣어라.
  nuance도 먼저 "보통 영어에서 이 단어가 어떻게 쓰이는지"를 설명하고, 그다음에 필요하면 "이런 상황에서는 이런 결로도 쓴다"를 한 문장 덧붙여라. 순서가 바뀌면 안 된다.
  - ✗ 나쁜 예 (give) — 본문 상황에만 치우쳐서 금지:
      meaning "주다, 제공하다, 넘기다"
      nuance  "신이나 상위자가 하위자에게 무언가를 소유하도록 허락하거나 제공하는 행동. 권한과 소유권을 이전하는 뜻을 담는다. 예: God will give you the land."
  - ✓ 좋은 예 (give) — 보편적인 뜻을 두루 담고, 특수 용법은 뒤에 한 줄:
      meaning "주다, 건네주다, (선물로) 주다, (권리·소유를) 넘겨주다"
      nuance  "내가 가진 것을 상대에게 넘겨 주는 가장 기본적인 말. 물건뿐 아니라 시간·도움·정보·기회처럼 눈에 보이지 않는 것에도 두루 쓴다. 예: She gave me a book. 그녀가 나에게 책을 주었다. / 힘 있는 쪽이 아랫사람에게 땅이나 권한을 정식으로 넘겨줄 때도 같은 단어를 쓴다."
- ★nuance의 예문은 일상 문장으로 들어라.★ 하나님·예수님·성경 인물·성경 사건이 주인공인 예문은 쓰지 마라. 학교·집·친구·직장 같은 평범한 상황의 짧은 문장이 좋다.
- ★난이도: '영어 초보인 성인'이 대상이다. 우리나라 중학교 교과서 수준의 단어·표현을 뽑아라.★
  - 빼라 (초등 수준이라 이미 안다): go, come, see, know, say, tell, man, day, water, stone, land, big, good, old, house, name, take, give, make, put, walk, hand, word, God, the, and, is 같은 낱말.
  - 넣어라 (중학교 수준): commanded, remember, prepare, servant, memorial, inheritance, possess, provisions, tribe, officers, everlasting, perish, dedicate, righteous, covenant, gather, promise, obey, honor, refuge, deliver 같은 낱말과, lay down·pass over·set apart 같은 굳어진 표현.
  - 너무 어려운 신학 전문어(propitiation, sanctification 같은 것)도 빼라. 중학교 교과서에서 볼 법한 수준이 딱 좋다.
- ★개수: 반드시 8개 이상 12개까지 뽑아라.★ 5개만 주는 것은 부족하다. 본문이 여러 절이면 절마다 골고루 뽑아라. 위에 적은 '중학교 수준'에 해당하면 충분히 넣을 만하다. 그 수준의 단어가 정말 모자랄 때만 8개 미만이어도 된다 — 개수를 채우려고 초등 수준 단어를 억지로 넣지는 마라.
- ★verse는 그 단어가 실제로 나오는 절 번호를 숫자 하나로 반드시 적어라.★ 여러 절에 나오면 처음 나오는 절 번호를 적는다.
  위에 주어진 본문의 절 번호 중 하나여야 하고, 지어내면 안 된다. 절이 하나뿐인 본문이면 그 절 번호를 그대로 적어라.
- 뽑은 단어는 절 번호 순서대로 정렬해서 내보내라 (앞 절 단어가 먼저).
- 원어(헬라어/히브리어)는 words에 넣지 마 (originals에서).
originals 규칙 (★바이블허브 사전에 실린 뜻을 그대로 옮겨 주는 칸이다. 네 해석을 지어내지 마라★):
- 이 본문에서 가장 중요한 원어 딱 3개만. 같은 Strong's 번호를 두 번 넣지 마라.
- Strong's 번호는 반드시 정확해야 한다 (블루레터바이블·바이블허브에서 검증 가능해야 하므로).
- ★where는 반드시 채워라.★ 읽는 사람이 "몇 절 어느 단어가 이 원어인지"를 알아야 본문에서 찾을 수 있다. 절 번호 + 그 자리의 한국어 표현(개역한글 기준)을 꼭 같이 적어라. 절이 하나뿐인 본문이면 그 절 번호를 그대로 적어라.
- ★phrases는 위에 주어진 본문에 '글자 그대로' 있는 어구여야 한다.★ 화면에서 그 자리를 찾아 표시하는 데 쓰므로, 한 글자라도 다르면 못 찾는다. 요약하거나 고쳐 쓰지 말고 본문에서 그대로 오려 붙여라. 확실하지 않으면 빈 문자열로 두어라.
- ★★★이 칸은 "사전을 그대로 옮겨 주는 칸"이다. 네가 해석해서 새 문장을 지어내지 마라.★★★
  뜻은 바이블허브(biblehub.com/greek/ · /hebrew/)에 실린 사전 — 헬라어는 Thayer's Greek Lexicon, 히브리어는 Brown-Driver-Briggs, 그리고 Strong's Exhaustive Concordance — 를 기준으로 삼는다.
- ★★senses에는 그 사전에 1·2·3으로 나뉘어 실린 뜻을 "빠짐없이" 넣어라 (보통 2~5개).★★
  en은 사전의 영어 표현을 그대로, ko는 그 영어의 한국어 직역만. 설명·적용·신학 풀이를 섞지 마라. 사전에 없는 뜻은 지어내지 마라.
  - ✓ 본보기 ἐγείρω G1453:
      1  "to arouse from sleep, to awake"  /  "잠에서 깨우다, 깨어나다"
      2  "to arouse from the sleep of death, to recall the dead to life"  /  "죽음의 잠에서 깨우다, 죽은 자를 다시 살려 내다"
      3  "in later usage generally to cause to rise, raise, from a seat, bed, etc.; to rise, arise"  /  "후대 용법으로는 널리 일으키다 — 자리·침상에서 일으켜 세우다, 일어나다"
- core는 Strong's의 어원·기본 뜻 한 줄을 그대로 옮긴 것이다. 여기서도 네 말로 새로 짓지 마라.
- ★★★절대 금지: 영어 번역어의 한국어 뜻풀이를 적는 것.★★★
  영어 성경이 'destroy'라고 옮겨 놓았다고 해서 '파괴하다, 무너뜨리다'라고 적으면, 그건 원어를 알려 준 게 아니라 영어 단어를 한국어로 바꿔 준 것뿐이다.
  이 칸이 알려 줄 것은 "원어 자체가 가진 뜻"이다. λύω(뤼오)라면 'destroy=파괴하다'가 아니라 'loose, unleash, let go — 풀다, 매인 것을 놓아주다'이고,
  'release (unbind) so something no longer holds together — 붙잡고 있던 것이 더는 서로 붙어 있지 못하게 풀어 놓다'가 그 낱말이 실제로 그리는 동작이다.
  senses·core·nuance 어디에서도 번역어를 되풀이하지 마라. 원어에서 출발해라.
- ★★번역어와 원어가 어긋날 때는 그 어긋남을 반드시 알려 줘라 — 단, 딱 한두 줄로.★★
  한국어 성경이 '헐다', 영어 성경이 'destroy'라고 옮겨 놓았어도 원어의 뜻은 전혀 다를 수 있다. 예: 요 2:19 '이 성전을 헐라'의 λύω(뤼오)는 부수는 말이 아니라 '묶인 것을 푸는(loose, untie, let go)' 말이다.
  이런 경우 nuance의 첫 문장을 "번역은 ~지만, 원어 ~는 ~다"로 시작해라. 어긋남이 없으면 없는 차이를 지어내지 마라.
- meaning은 Strong's Concordance의 짧은 뜻 나열 한 줄로 끝내라. senses와 같은 말을 길게 되풀이하지 마라.
- ★nuance는 1~2문장 안으로 짧게 끝내라.★ 사전 뜻을 다시 풀어 말하지 말고, 번역어와 어긋나는 지점이나 이 단어만의 결을 딱 한 가지만 짚어라. 길게 늘어놓으면 안 된다.
- ★★refs는 반드시 2~3개 넣어라. 이 칸의 목적은 "아, 그 말이 이 말이었어?"다.★★
  "이 본문이 아닌 다른 성경 구절"에서 "같은 Strong's 번호"가 쓰인 자리를 골라라. 그중에서도 ★한국어 번역이 전혀 다르게 되어 있어서, 같은 단어인 줄 몰랐을 장면★을 우선하라. 독자가 이미 아는 유명한 장면일수록 좋다.
  - ✓ 본보기 λύω G3089 (요 2:19 '헐라'): 계 5:2 '누가 그 두루마리를 펴며 그 인을 떼기에' — 봉인을 푸는 그 말이 이 말이다 / 막 11:2 '나귀를 풀어 끌고 오라' / 눅 13:16 '이 매임에서 푸는 것이'
  - ✓ 본보기 H6942 카다쉬: 창 2:3 일곱째 날, 출 3:5 거룩한 땅, 출 19:23 시내산 경계
- refs의 phrase는 그 구절에 실제로 있는 어구를 그대로 적어라. note는 ★한 문장★으로, 그 장면이 이 단어의 실제 동작을 어떻게 보여 주는지 적어라 (예: '봉인을 푸는 것도 같은 낱말이다 — 부수는 게 아니라 묶인 것을 풀어 놓는 동작이다'). 구절 요약이 아니라 단어 해설이어야 한다.
- 세 원어의 refs가 서로 겹치지 않게 하라. 확실하지 않은 장절은 넣지 말고 확실한 것만 넣어라 (틀린 장절은 최악이다).
- point는 ★한두 문장★으로, 이 본문으로 돌아와 '묵상할 거리'가 되게 하라. 뜻풀이를 되풀이하지 말고, 그 낱말대로 읽으면 이 말씀이 우리에게 무엇을 하라는 말이 되는지 적어라.
  - ✓ 본보기 (λύω, 요 2:19): '헐라가 아니라 풀라라면, 이 말씀은 무엇을 부수라는 게 아니라 우리가 붙들고 있던 율법과 익숙한 관습을 놓아 주라는 부름이 된다.'
verses 규칙:
- 절 본문에는 아무 표시(별표·괄호·하이라이트)도 붙이지 마라. 그 번역본의 문장을 있는 그대로만 적어라.
- 절 번호는 "n"에만 숫자로 적고, 본문 문자열 안에는 절 번호를 쓰지 마라.
추측하지 말고 확실한 것만. JSON만 출력.'''


# ── 본문을 실제로 받아왔을 때 쓰는 프롬프트 ────────────────────────────────
# 번역본 본문은 bolls.life에서 그대로 받아 왔으므로, AI에게는 단어·원어·배경만 시킨다.
# (본문을 AI 기억에서 꺼내지 않으니 '엉뚱한 장절' 문제가 원천적으로 사라진다.)
def _prompt_slice(a, b):
    i = PROMPT.find(a)
    j = PROMPT.find(b)
    return PROMPT[i:j] if 0 <= i < j else ''


_STUDY_SCHEMA = _prompt_slice('  "words": [', '\n\nwords 규칙')
_STUDY_RULES = _prompt_slice('words 규칙', '\nverses 규칙:')

# 고른 번역본에 맞춰 프롬프트를 그때그때 짓는다.
# (개역한글과 고른 영어 번역본은 실제로 받아오므로, AI에게는 개역개정·새번역과
#  단어·원어·배경만 시킨다.)
_KO_HINT = {
    '개역개정': '개역개정은 개역한글의 개정판이라 문장 뼈대는 비슷하되, 옛 표기·띄어쓰기·어려운 낱말이 현대 표기로 다듬어져 있다.',
    '새번역': '새번역(표준새번역 개정)은 현대어체로 더 풀어 쓴다.',
}


def _sc_head(en1):
    """프롬프트 머리말 — 실제로 받아온 본문을 보여 주는 부분."""
    return ('아래는 "{passage}"의 실제 성경 본문이다. 성경 본문 제공처에서 그대로 받아온 것이라\n'
            '절 번호와 내용이 이미 정확하다. 이 본문 밖으로 나가지 마라.\n\n'
            '[실제 본문 — 절 번호. 개역한글 / ' + str(en1) + ']\n'
            '{text}\n')


def _got_names(extra, en_list):
    """이미 확보해 둔(=AI가 다시 쓸 필요 없는) 번역본 이름들."""
    seen, out = set(), []
    for k in (['개역한글'] + list(extra or []) + list(en_list or [])):
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return '·'.join(out)


def _verse_rules(ai_ko, en_list):
    """AI가 써야 할 한국어 번역본에 대한 규칙."""
    names = '과 '.join(ai_ko)
    hints = ' '.join(_KO_HINT[k] for k in ai_ko if k in _KO_HINT)
    same = ('\n  둘을 똑같이 쓰지 말고 각 번역본의 실제 문장으로 적어라.' if len(ai_ko) > 1 else '')
    return ("""
■ 절대 규칙
- 위에 있는 절 번호만 쓴다. 위에 없는 절을 만들거나 이웃 장·절 내용을 끌어오면 오답이다.
- %s 본문은 이미 확보했으니 다시 쓰지 마라.
- verses에는 **%s만** 적는다. 각 절의 내용은 위 같은 번호의 개역한글과
  같은 사건·같은 문장이어야 한다 (표기와 문체만 그 번역본의 것으로). 한 절이라도 밀려 쓰면 전부 버려진다.
- %s%s 확실하지 않으면 빈 문자열로 두어라.

아래 JSON 형식으로만 응답해줘. 코드블록 없이 JSON만.
""" % (_got_names(None, en_list), names, hints, same))


def _sc_verses(ai_ko):
    """verses 스키마 — 고른 한국어 번역본 칸만 넣는다."""
    fields = ',\n'.join('      "%s": "그 절의 %s 본문 (그 절만, 표시 없는 순수 본문)"' % (k, k)
                        for k in ai_ko)
    return '\n{{\n  "verses": [\n    {{\n      "n": 6,\n' + fields + '\n    }}\n  ],\n'


def _study_head(ai_ko, en_list):
    return ("""
■ 절대 규칙
- 위에 있는 절 번호만 쓴다. 위에 없는 절을 만들거나 이웃 장·절 내용을 끌어오면 오답이다.
- 번역본 본문(%s)은 이미 확보했으니 다시 쓰지 마라.
  verses 항목은 넣지 마라. 단어·원어·소제목·배경만 적는다.

아래 JSON 형식으로만 응답해줘. 코드블록 없이 JSON만.

{{
""" % _got_names(ai_ko, en_list))


_VERSE_TAIL = """
}}

- 절 본문에는 아무 표시(별표·괄호·하이라이트)도 붙이지 마라. 그 번역본의 문장을 있는 그대로만 적어라.
- 절 번호는 "n"에만 숫자로 적고, 본문 문자열 안에는 절 번호를 쓰지 마라.
- verses 말고 다른 항목은 넣지 마라. 추측하지 말고 확실한 것만. JSON만 출력."""

PROMPT_OK = bool(_STUDY_SCHEMA and _STUDY_RULES)


def build_prompt(stage, ai_ko, en_list):
    """stage(text/study/all) + 고른 번역본 → 프롬프트 한 덩이.
    'text'인데 AI가 쓸 한국어 칸이 없으면 None (AI를 부를 필요가 없다)."""
    if not PROMPT_OK:
        return None
    en_list = list(en_list) or list(DEF_EN)
    ai_ko = list(ai_ko or [])
    head = _sc_head(en_list[0])
    study = (_STUDY_SCHEMA + '\n\n' + _STUDY_RULES).replace('NKJV/NASB', '/'.join(en_list)) \
        + '\n추측하지 말고 확실한 것만. JSON만 출력.'
    if stage == 'text':
        if not ai_ko:
            return None
        return head + _verse_rules(ai_ko, en_list) + _sc_verses(ai_ko).rstrip().rstrip(',') + _VERSE_TAIL
    if stage == 'study' or not ai_ko:
        return head + _study_head(ai_ko, en_list) + study
    return head + _verse_rules(ai_ko, en_list) + _sc_verses(ai_ko) + study


DEFINE_PROMPT = '''아래 영어 단어들의 "영어사전 뜻"을 알려줘. 성경 해석이 아니라 표준 영어사전(옥스퍼드·메리엄웹스터 급) 기준이다.
코드블록 없이 JSON만 출력해.

단어 목록: {words}

{{
  "defs": [
    {{
      "english": "표제어 (입력받은 그대로)",
      "korean": "그 꼴에 맞는 한국어 하나 (heard면 '듣다'가 아니라 '들었다')",
      "pos": "품사. 꼴이 바뀐 낱말이면 괄호로 밝혀라 (예: '동사(과거형)', '명사(복수형)', '형용사(비교급)')",
      "meaning": "★꼴이 바뀐 낱말이면 맨 앞에 '원형(한국어 뜻)의 ○○형.' 을 먼저 적어라 (예 heard → 'hear(듣다)의 과거형. 들었다, 전해 들었다, 알게 되었다'). 그다음★ 표준 영어사전의 사전적 정의를 한국어로 2~3개, 쉼표로 구분. 의역·성경식 풀이 금지.",
      "nuance": "영어에서 실제 쓰이는 어감·용법 한 문장 + 짧은 ★일상★ 예문 하나 (한국어 해석 포함). 하나님·성경 인물이 들어간 예문 금지."
    }}
  ]
}}

규칙:
- 입력된 단어는 하나도 빠뜨리지 말고 모두 defs에 넣어라. english는 입력 그대로 적어라.
- meaning은 반드시 사전에 실린 정의여야 한다. 문맥에 맞춘 의역이나 신학적 설명을 넣지 마라.
- ★사전에서 흔한 뜻부터 차례로, 여러 개를 적어라.★ 성경에서 쓰인 뜻만 골라 적지 말고, 그 뜻도 목록 안에 함께 넣어라. (예 give → '주다, 건네주다, (선물로) 주다, (권리·소유를) 넘겨주다')
- everlasting과 eternal처럼 비슷한 단어도 각각 자기 사전 뜻을 정확히 구분해서 적어라.
- ★과거형·복수형처럼 꼴이 바뀐 낱말은 원형으로 되돌리지 말고, 무엇의 변형인지 밝혀라.★ english는 입력 그대로 두고 korean·pos·meaning으로 알려 준다. (예 heard → korean '들었다' / pos '동사(과거형)' / meaning 'hear(듣다)의 과거형. 들었다, 전해 들었다, 알게 되었다')
- 한국어 단어가 섞여 있으면 그 단어는 뜻풀이만 간단히 적어라.
JSON만 출력.'''


SCHEMA_VER = 29  # 분석 결과 형식 버전. 올리면 이전 캐시를 자동으로 무시하고 다시 분석함.


def _valid(d, stage='all'):
    """분석 결과가 화면에 그릴 만큼 온전한지.
    'study' 갈래는 본문이 없으니 단어나 원어가 있으면 온전한 것으로 본다."""
    if not isinstance(d, dict):
        return False
    if stage == 'study':
        return bool(d.get('words')) or bool(d.get('originals'))
    return isinstance(d.get('translations'), dict) and len(d.get('translations')) > 0


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
            '위에서 요청한 번역본들이다. 내용만 맞추고 문장은 각 번역본의 것으로 적어라.\n')


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

            # ── 두 갈래로 나눠 부르기 ──────────────────────────────
            # 'text'  = 번역본 본문만 (짧은 프롬프트라 훨씬 빨리 온다 → 화면에 먼저 띄운다)
            # 'study' = 단어·원어·소제목·배경만 (본문을 읽는 동안 뒤에서 계속 분석)
            # 'all'   = 예전처럼 한 번에 (‘내 기록’에서 옛 본문을 펼칠 때 등)
            stage = str(body.get('stage') or 'all').strip()
            if stage not in ('text', 'study', 'all'):
                stage = 'all'

            # ── 고른 번역본 (내 기록 → 번역본 고르기) ────────────────
            ko_list, en_list = norm_sel(body)
            ai_ko = [k for k in ko_list if k not in KO_VERBATIM]   # AI가 써야 할 한국어 칸
            order = list(ko_list) + list(en_list)                  # 화면 칸 순서
            en1 = en_list[0]

            key = normalize_key(passage)
            # 캐시 이름표에 고른 번역본을 함께 넣는다 (사람마다 고른 게 다르므로).
            # 단어·원어(study)는 개역한글과 '첫 영어 번역본'만 보고 만들어지므로 그것만 넣는다.
            if stage == 'study':
                ckey = 'std:%s:%s' % (en1, key)
            else:
                sel = '%s|%s' % (','.join(ko_list), ','.join(en_list))
                ckey = ('txt:' if stage == 'text' else '') + sel + ':' + key
            qkey = urllib.parse.quote(ckey)

            # 1. 캐시 확인.
            #    SCHEMA_VER 14부터는 '절 단위 검증을 통과한 결과'만 저장되므로, 저장된 것은
            #    이미 절 번호·내용이 실제 본문과 대조된 것이다. 검증 안 된 옛 캐시는 v가 달라 자동으로 무시된다.
            #    body에 fresh=true가 오면(‘다시 분석’ 버튼) 캐시를 건너뛰고 새로 분석한다.
            cached = None if body.get('fresh') else \
                sb('GET', 'analyses?passage_key=eq.' + qkey + '&select=data', silent=True)
            if cached and _valid(cached[0].get('data'), stage) and cached[0]['data'].get('v') == SCHEMA_VER:
                data = cached[0]['data']
                data['cached'] = True
                self._send_json(data)
                return

            # 1-b. ★번역본 본문을 '실제로' 받아온다.★
            #      AI 기억에서 꺼내면 장절이 밀리거나 지어내는 일이 생긴다.
            #      개역한글·NKJV·NASB는 성경 본문 제공처에서 그대로 받아 화면에 쓰고,
            #      개역개정·새번역은 (공개 API가 없어) AI가 쓰되 받아온 본문과 절 단위로 대조한다.
            #      AI는 그 밖에 단어·원어·배경 설명에만 쓴다.
            sc = fetch_scripture(passage, en_list)
            if sc and PROMPT_OK:
                text = scripture_block(sc)
                base = build_prompt(stage, ai_ko, en_list)
                data = {}
                if base:
                    prompt = base.format(passage=passage, text=text)
                    try:
                        data = call_ai_raw(prompt)
                    except (ValueError, json.JSONDecodeError):
                        try:
                            data = call_ai_raw(prompt, strict=True)
                        except Exception:
                            data = {}
                    except Exception:
                        data = {}
                if not isinstance(data, dict):
                    data = {}

                # 받아온 실제 본문에 AI가 쓴 개역개정·새번역을 붙이고 절 단위로 검증.
                # 검증에 걸린 칸은 아예 빼 버린다 (틀린 본문보다 없는 게 낫다).
                if stage != 'study':
                    byv_ko = dict((r['n'], r.get('개역한글') or '') for r in sc['rows'])
                    rows, ko_ok = merge_ai_korean(sc['rows'], data.get('verses'), byv_ko, ai_ko)
                    # 고른 영어 두 칸에서 '뜻·낱말 선택이 분명히 다른 자리'만 표시 (어미·조동사 차이는 제외)
                    rows = mark_rows_en(rows, en_list[0], en_list[1] if len(en_list) > 1 else None)
                    tr = rows_to_translations(rows, order)
                    if not tr:
                        raise RuntimeError('본문 조립 실패')

                    data['translations'] = tr      # ★개역한글과 영어 번역본은 받아온 그대로★
                    data['verbatim'] = [k for k in (list(KO_VERBATIM) + list(en_list)) if tr.get(k)]
                    data['tr_got'] = sc['got'] + ko_ok
                if stage != 'text':
                    verify_phrases(data, sc, en1)
                data['source'] = 'bolls'
                data['stage'] = stage
                data['tr'] = {'ko': list(ko_list), 'en': list(en_list)}
                if isinstance(data.get('words'), list):
                    data['words'] = dedupe_words(data['words'])[:12]
                if isinstance(data.get('originals'), list):
                    data['originals'] = dedupe_originals(data['originals'])
                data.pop('diffs', None)
                data.pop('verses', None)
                data.pop('verses_rows', None)
                data['v'] = SCHEMA_VER

                sb('DELETE', 'analyses?passage_key=eq.' + qkey, silent=True)
                sb('POST', 'analyses',
                   {'passage_key': ckey, 'passage': passage, 'data': data}, silent=True)
                data['cached'] = False
                self._send_json(data)
                return

            # 1-c. 본문 제공처가 응답하지 않을 때만 예비 경로: AI가 본문을 쓰되,
            #      공개 도메인 KJV를 '정답표'로 삼아 절 단위로 대조·검증한다.
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
                data['words'] = dedupe_words(data['words'])[:12]
            if isinstance(data.get('originals'), list):
                data['originals'] = dedupe_originals(data['originals'])

            # 하이라이트는 하지 않는다. 번역본 비교 칸은 '본문 그대로'만 정확히 보여 준다.
            data.pop('diffs', None)
            data.pop('verses', None)        # 조립이 끝났으면 원본 절 배열은 보낼 필요 없다
            data.pop('verses_rows', None)
            data['stage'] = stage
            data['tr'] = {'ko': list(DEF_KO), 'en': list(DEF_EN)}   # 예비 경로는 기본 칸
            data['v'] = SCHEMA_VER   # 형식 버전 기록 (옛 캐시 자동 무효화용)

            # 3. 캐시 교체 저장 (예전/깨진 캐시가 있으면 지우고 새로 저장). 실패해도 응답엔 지장 없음.
            sb('DELETE', 'analyses?passage_key=eq.' + qkey, silent=True)
            sb('POST', 'analyses', {'passage_key': ckey, 'passage': passage, 'data': data}, silent=True)

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
