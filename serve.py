"""활 시세 감정소 로컬 실행기.

    python serve.py

브라우저가 http://localhost:8731 로 열린다. index.html 을 그대로 서빙하면서
/api/trade 로 거래소 검색 URL을 대신 조회해준다(브라우저는 CORS 때문에 직접 못 부름).

비공개 검색이라 403이 나오면 POESESSID 환경변수를 직접 넣고 다시 실행:
    Windows PowerShell:  $env:POESESSID="..."; python serve.py
쿠키값은 이 스크립트만 읽고 페이지로는 절대 안 내려간다.
"""
import json, os, re, sqlite3, sys, threading, time, urllib.error, urllib.request, webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from statistics import median

# 한국어 윈도우 콘솔은 기본이 cp949라 —, ≥ 같은 글자에서 UnicodeEncodeError 로 죽는다.
# line_buffering 은 진행 표시용 — 이게 없으면 파이프로 넘길 때 20분짜리 수집이
# 끝날 때까지 한 줄도 안 보여서 살아 있는지조차 알 수 없다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

PORT = int(os.environ.get("PORT", 8731))
ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "poe2-bow-appraiser/1.0 (personal price-efficiency tool; low volume)"

# 아무 URL이나 대신 열어주면 로컬 프록시가 SSRF 통로가 된다 — 거래소 호스트만 허용
ALLOWED_HOSTS = {
    "www.pathofexile.com", "pathofexile.com", "pathofexile.tw",
    "poe.kakaogames.com", "poe.game.daum.net",
    "ru.pathofexile.com", "jp.pathofexile.com", "de.pathofexile.com",
    "es.pathofexile.com", "br.pathofexile.com", "fr.pathofexile.com",
}
# 화폐는 거래소가 준 이름 그대로 넘긴다. 버리면 매물 대부분이 사라진다
# (실측: 레어 활 최저가가 transmute/aug/exalted 로 흩어져 있음).
# 일정 이하 DPS 는 사실상 공짜라(실측: 391 DPS 도 1엑잘) 볼 이유가 없다.
# "dps >= T 중 최저가"가 곧 최전선 위의 점이므로, 문턱값을 훑으면 곡선이 바로 나온다.
# 값이 폭증하는 상단일수록 촘촘하게 — 그 구간이 보간 오차가 가장 큰 곳이다.
THRESHOLDS = [400, 450, 500, 505, 510, 515, 520, 540, 560, 580, 600, 610, 620, 640, 660]
# 505~520 이 촘촘한 이유: 실측에서 501 DPS 5카오스 -> 533 DPS 1디바인(=300엑잘) 으로
# 절벽이 바로 이 사이에 있었다. 절벽 위치를 1~2 DPS 단위로 짚어야 살지 말지가 갈린다.
# frameType 은 rarity 문자열이 없을 때의 대비책 (12~14 는 룬 박힌 변형)
FRAME_RARITY = {0: "Normal", 1: "Magic", 2: "Rare", 3: "Unique",
                12: "Magic", 13: "Rare", 14: "Unique"}
# 아이템 거래에 실제로 쓰이는 화폐. 이 넷만 받고 나머지 가격의 매물은 통째로 버린다 —
# 진화/확장 오브 같은 하급 화폐는 교환 매물이 거의 없어 환율을 못 믿고, 그걸 섞으면
# 곡선의 세로 축척이 통째로 틀어진다.
TRADE_CURRENCIES = ["exalted", "chaos", "divine", "annul"]
FETCH_BATCH = 10          # GGG가 한 번에 받아주는 개수
DEFAULT_LIMIT = 20        # 기본 20개 = fetch 2회. 더 필요하면 &limit=
PAUSE = 0.7               # 배치 사이 간격 (레이트 리밋 예의)


class TradeError(Exception):
    pass


# 엔드포인트마다 레이트 리밋 버킷이 따로다 (검색 GET / 검색 POST / 대량 교환).
# 하나로 뭉쳐두면 검색 직후 교환을 부를 때 남의 잔여량으로 판단해 헛되이 쉬거나,
# 반대로 여유가 있다고 착각하고 차단당한다.
RATE_STATE = {}          # 버킷 이름 -> {"rules":..., "state":...}


def bucket_of(url, payload=None):
    """버킷은 경로로 갈린다 — GET/POST 가 아니다.
    실측 한도: /search/ 는 10초당 5회, /fetch/ 는 4초당 12회, /exchange/ 는 15초당 5회.
    fetch 를 search 와 같은 통에 넣으면 서로의 잔여량으로 판단해 잘못 쉬거나 차단당한다."""
    for seg in ("exchange", "fetch", "search"):
        if "/%s/" % seg in url:
            return seg
    return "other"


PACING_WINDOW = 600      # 이보다 긴 창은 속도 제한이 아니라 예산으로 본다
MAX_WAIT = 600           # 이보다 오래 기다려야 하면 자지 말고 접는다
LAST_CALL = {}           # 버킷 -> 마지막 요청 시각
# ThreadingHTTPServer 라 /api/trade 가 동시에 여러 개 들어올 수 있다(탭 두 개, 연타).
# 잠그지 않으면 세 요청이 전부 같은 LAST_CALL 을 읽고 같은 순간에 나간다 —
# 실측: 1초씩 벌어져야 할 요청 3개가 간격 0.00초로 한꺼번에 나갔다. 재는 의미가 사라진다.
_THROTTLE_LOCK = threading.Lock()


def throttle(bucket, max_wait=None):
    """거래소가 헤더로 알려준 한도에 맞춰 스스로 속도를 맞춘다.

    핵심은 '한도에 닿기 전에 고르게 펴는' 것이다. 닿고 나서 쉬면 창 전체(300초)를
    통째로 기다려야 해서, 예상 13분짜리 수집이 25분을 넘긴 적이 있다.
    그래서 매 요청마다 window/limit 만큼(검색은 10초) 최소 간격을 둔다.
    한도를 넘기면 30분 차단이라 여유분 한 칸은 끝까지 남겨둔다.
    """
    max_wait = MAX_WAIT if max_wait is None else max_wait
    with _THROTTLE_LOCK:
        b = RATE_STATE.get(bucket) or {}
        rules, state = b.get("rules"), b.get("state")
        # 헤더는 '그 응답 시점'의 사용량이다. 그 뒤로 흐른 시간만큼 창은 이미 지나갔다.
        age = time.time() - b.get("at", 0)
        gap, panic = 0.0, 0.0
        if rules and state:
            for ru, st in zip(rules.split(","), state.split(",")):
                try:
                    limit, window, _ = (int(x) for x in ru.split(":"))
                    used, _, _ = (int(x) for x in st.split(":"))
                except ValueError:
                    continue
                if not limit:
                    continue
                # 긴 창(6시간 600회 같은 것)은 '하루 예산'이지 속도 제한이 아니다 —
                # 그걸로 간격을 잡으면 36초씩 쉬느라 한 번 수집에 한 시간이 걸린다.
                # 페이싱은 짧은 창으로만 하고, 긴 창은 아래 한도 직전 검사로만 지킨다.
                if window <= PACING_WINDOW:
                    gap = max(gap, window / float(limit))   # 이 속도면 한도에 안 닿는다
                # 이미 벽 앞이면 창이 지나야 한다. 단 '남은 만큼'만 — 창 전체를 다시 세면,
                # 요청이 실패해 헤더가 안 온 사이(pair_rate 는 TradeError 를 삼킨다) 같은 낡은
                # 사용량으로 매번 창 전체를 다시 잔다(실측: 교환 15초 창을 네 번 연속 재잠).
                if used >= limit - 1 and window > age:
                    panic = max(panic, window - age)
        if panic > max_wait:
            # 하루 예산이 바닥난 상황. 여기서 6시간을 자면 매시간 도는 수집이 그냥 멈춘 것과 같다.
            raise TradeError("거래소 요청 한도에 가까워 지금은 쉬어야 합니다"
                             "(%s, %.0f초 뒤 회복). 잠시 뒤에 다시 시도해주세요." % (bucket, panic))
        if panic:
            print("     레이트 리밋(%s) 한도 직전 — %.0f초 대기" % (bucket, panic))
            time.sleep(panic)
        elif gap:
            wait = gap - (time.time() - LAST_CALL.get(bucket, 0))
            if wait > 0:
                time.sleep(wait)
        LAST_CALL[bucket] = time.time()


def api_get(url, payload=None, _retried=False):
    """payload 를 주면 POST. 거래소는 저장된 검색을 GET 하면 질의문만 돌려주고,
    실제 결과는 그 질의문을 다시 POST 해야 나온다."""
    headers = {"Accept": "application/json", "User-Agent": UA}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    sess = os.environ.get("POESESSID")
    if sess:
        req.add_header("Cookie", "POESESSID=" + sess)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            RATE_STATE[bucket_of(url, payload)] = {
                "rules": r.headers.get("X-Rate-Limit-Ip"),
                "state": r.headers.get("X-Rate-Limit-Ip-State"),
                "at": time.time()}
            body = r.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except ValueError:
                # 거래소는 Cloudflare 뒤에 있어서 점검·차단·로그인 화면이 HTML 로 온다.
                # 그냥 두면 JSONDecodeError 가 TradeError 를 안 거치고 그대로 터진다.
                raise TradeError("거래소가 JSON 이 아닌 응답을 보냈습니다"
                                 "(점검·차단·로그인 화면일 수 있습니다): %s" % body[:120].strip())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code in (401, 403):
            raise TradeError(
                "거래소가 로그인을 요구합니다(%d). POESESSID 환경변수를 넣고 실행기를 다시 켜주세요." % e.code)
        if e.code == 429:
            after = int(e.headers.get("Retry-After") or 60)
            if _retried or after > MAX_WAIT:
                # 여기서 몇십 분을 자면 매시간 도는 수집이 겹쳐 쌓인다. 접고 다음 회차에 맡긴다.
                raise TradeError("429 — %d초 뒤에 풀립니다. 이번 회차는 건너뜁니다." % after)
            print("     429 — %d초 쉬었다가 한 번 더 시도합니다" % after)
            time.sleep(after + 2)
            return api_get(url, payload, _retried=True)
        raise TradeError("거래소 응답 %d: %s" % (e.code, body))
    except urllib.error.URLError as e:
        raise TradeError("거래소에 연결하지 못했습니다: %s" % e.reason)


def prop(item, *patterns):
    """properties/additionalProperties 에서 첫 번째로 맞는 값 문자열을 꺼낸다."""
    for p in (item.get("properties") or []) + (item.get("additionalProperties") or []):
        name = p.get("name") or ""
        if any(re.search(pat, name) for pat in patterns):
            vals = p.get("values") or []
            if vals and vals[0]:
                return str(vals[0][0])
    return None


def to_number(s):
    if not s:
        return None
    m = re.search(r"[\d.]+", s.replace(",", ""))
    return float(m.group()) if m else None


def rarity_of(item):
    r = item.get("rarity")
    return r if r in ("Normal", "Magic", "Rare", "Unique") else FRAME_RARITY.get(item.get("frameType"), "")


MOD_KEYS = ("implicitMods", "explicitMods", "runeMods", "craftedMods",
            "fracturedMods", "enchantMods", "desecratedMods")


def mod_lines(item):
    """옵션 줄을 문자열로 모은다. GGG는 같은 자리에 문자열도 객체도 보낸다."""
    out = []
    for key in MOD_KEYS:
        for m in item.get(key) or []:
            if isinstance(m, str):
                out.append(m)
            elif isinstance(m, dict) and m.get("description"):
                out.append(m["description"])
    return out


def normalize(res):
    item = res.get("item") or {}
    listing = res.get("listing") or {}
    price = listing.get("price") or {}
    cur = price.get("currency")
    amount = price.get("amount")
    if not cur or not amount:
        return None                                    # 값이 아예 없으면 비교가 불가능
    if cur not in TRADE_CURRENCIES:
        return None                # 실거래 화폐 넷 밖은 버린다 — 환율을 못 믿으면 곡선이 틀어진다
    ext = item.get("extended") or {}
    pdps, edps = ext.get("pdps"), ext.get("edps")
    if pdps is None and edps is None:
        return None                                    # 활이 아니거나 GGG가 DPS를 안 준 항목
    name = " ".join(x for x in (item.get("name"), item.get("typeLine") or item.get("baseType")) if x)
    return {
        "name": name.strip() or "이름 없음",
        "pdps": round(pdps or 0, 1),
        "edps": round(edps or 0, 1),
        "aps": to_number(prop(item, r"Attacks per Second", r"초당 공격")) or 0,
        "crit": to_number(prop(item, r"Critical .*Chance", r"치명타")) or 0,
        "price": amount,
        "cur": cur,
        "rarity": rarity_of(item),
        "mods": mod_lines(item),
    }


def search_min_dps(base, league_path, query, lo):
    """dps >= lo 중 가격 오름차순. 첫 결과가 곧 그 문턱값의 최전선 점이다."""
    import copy
    q = copy.deepcopy(query)
    q.setdefault("filters", {}).setdefault("equipment_filters", {})      .setdefault("filters", {})["dps"] = {"min": lo}
    return api_get(base + league_path, {"query": q, "sort": {"price": "asc"}})


def fetch_ids(base, query_id, ids):
    out = []
    for i in range(0, len(ids), FETCH_BATCH):
        throttle("fetch")
        data = api_get("%s/api/trade2/fetch/%s?query=%s"
                       % (base, ",".join(ids[i:i + FETCH_BATCH]), query_id))
        for res in data.get("result") or []:
            if res:
                row = normalize(res)
                if row:
                    out.append(row)
        time.sleep(PAUSE)
    return out


EXCHANGE_PAUSE = 16      # 대량 교환 API 는 15초당 5회 — 넉넉히 띄운다


def pair_rate(base, league, have, want):
    """have 를 주고 want 를 받는 매물들에서 "1 want 당 have 개수"를 모은다."""
    body = {"query": {"status": {"option": "online"}, "have": [have], "want": [want]},
            "sort": {"have": "asc"}, "engine": "new"}
    throttle("exchange")
    try:
        r = api_get("%s/api/trade2/exchange/%s" % (base, league), body)
    except TradeError as e:
        print("     %s->%s 실패 (%s)" % (have, want, e))
        return []
    vals = []
    for v in (r.get("result") or {}).values():
        for off in (v.get("listing") or {}).get("offers") or []:
            ex, it = off.get("exchange") or {}, off.get("item") or {}
            if ex.get("currency") == have and it.get("currency") == want and it.get("amount"):
                vals.append(ex["amount"] / it["amount"])
    vals.sort()
    return vals


def best_offer(vals):
    """1개를 손에 넣는 가장 싼 값. 사기 매물은 언제나 비싼 쪽이므로 최저가면 걸러진다
    (실측 divine->chaos 6건 중 4건이 1:1, 7:1 사기 — 중앙값을 쓰면 사기값이 나온다)."""
    return min(vals)


# 화폐가 4개뿐이라 3홉이면 모든 경로가 닿는다. 더 돌리면 안 된다 —
# 잡음 탓에 곱이 1보다 작은 차익 순환(A->B->C->A)이 생기면 값이 무한히 깎인다.
MAX_HOPS = 3


def solve_rates(obs, base_cur="exalted", rounds=MAX_HOPS):
    """엑잘 기준 가치.

    직접 매물(엑잘 -> X)이 있으면 그걸 쓴다 — 한 다리 건널 때마다 잡음이 곱해지므로
    우회로가 더 정확할 이유가 없다. 우회는 직접 매물이 아예 없는 화폐를 메울 때만 쓴다.
    """
    rate = {k: best_offer(v) for k, v in obs.items() if v}
    val = {base_cur: 1.0}
    how = {base_cur: "기준"}

    for (have, want), r in rate.items():                  # 1단계: 직접 매물
        if have == base_cur and want != base_cur:
            val[want] = r
            how[want] = "직접"

    for _ in range(rounds):                               # 2단계: 남은 것만 우회로 메움
        changed = False
        for (have, want), r in rate.items():
            if want == base_cur or how.get(want) == "직접" or have not in val or r <= 0:
                continue
            cand = r * val[have]
            if want not in val or cand < val[want] * (1 - 1e-12):
                val[want] = cand
                how[want] = "우회(%s 경유)" % have
                changed = True
        if not changed:
            break

    # 관측이 서로 안 맞으면(차익 순환) 우회 계산이 값을 계속 깎아내린다 — MAX_HOPS 로 횟수를
    # 막아도 3회면 이미 0 에 닿는다(실측: chaos 0.0000, annul 0.0004). 그 값이 그대로 나가면
    # 페이지에서 그 화폐 매물의 가격이 0 이 되어 "DPS 501 활을 0.0000 ex 에 산다"가 최전선
    # 맨 앞에 뜬다(브라우저에서 실제로 재현). 엑잘이 이 넷 중 가장 작은 단위라 1 보다 싼 값은
    # 관측이 깨졌다는 뜻이다 — 지어낸 환율을 내보내느니 그 화폐를 빼고 기본값에 맡긴다.
    for cur in [c for c in val if c != base_cur]:
        v = val[cur]
        if not (v == v and v not in (float("inf"), float("-inf")) and v >= 1.0):
            print("     %s 환율이 %r 로 나와 버립니다 (관측이 서로 안 맞음)" % (cur, v))
            del val[cur], how[cur]
    return val, how


def fetch_rates(base, league, currencies):
    """엑잘 기준 환율. 실제 거래에 쓰이는 네 화폐끼리 모든 쌍을 재고 삼각 계산으로 합친다.

    한 쌍만 보면 카카오 서버는 매물이 3건뿐이라 이상값 하나에 통째로 끌려간다.
    쌍을 전부 재면 표본이 6배로 늘고, 직접 매물이 없는 쌍도 다른 화폐를 거쳐 메울 수 있다.
    """
    obs = {}
    for i, a in enumerate(TRADE_CURRENCIES):
        for b in TRADE_CURRENCIES[i + 1:]:
            for have, want in ((a, b), (b, a)):
                vals = pair_rate(base, league, have, want)
                if vals:
                    obs[(have, want)] = vals
                    print("     %-8s -> %-8s  1개당 %g %s  (매물 %d건)"
                          % (have, want, median(vals), have, len(vals)))

    val, how = solve_rates(obs)
    out = {}
    for cur in sorted(set(list(val) + [c for c in currencies if c])):
        if cur == "exalted":
            out[cur] = {"rate": 1.0, "how": "기준"}
        elif cur in val:
            direct = sorted(obs.get(("exalted", cur)) or [])
            out[cur] = {"rate": round(val[cur], 6), "how": how[cur], "direct": len(direct),
                        "lo": round(direct[0], 6) if direct else None,
                        "hi": round(direct[-1], 6) if direct else None}
    print("     --- 환율 (엑잘 기준) ---")
    for cur, d in sorted(out.items(), key=lambda kv: -kv[1]["rate"]):
        print("     1 %-8s = %10.4f ex  (%s%s)"
              % (cur, d["rate"], d.get("how"),
                 "" if not d.get("direct") else ", 매물 %d건" % d["direct"]))
    return out


# ---------- 옵션 조건 곡선 ----------
# 검색 한 번에 id 가 100개까지만 오므로, "가장 싼 100개"에는 비싼 옵션이 붙은 활이
# 아예 안 들어온다. 옵션을 로컬에서 거르는 방식으로는 그 프리미엄을 잴 수 없다.
# 그래서 옵션 자체를 검색 조건으로 넣어 그 조건만의 최저가 곡선을 따로 뜬다.
# (실측: DPS 500+ 최저가 50엑잘 -> "투사체 스킬 레벨 +2" 조건을 걸면 8디바인, 48배)

COND_MODS = 10                      # 조건 곡선 최대 개수 — 요청 수가 이것에 비례한다
COND_DPS = [500, 550, 600, 650]     # 조건 곡선은 문턱값을 성기게 잡는다

# 구매자가 실제로 값을 치르는 옵션들. 빈도순으로 자동 선택하면 치명타가 절대 안 뽑힌다 —
# 기본 표본이 "각 DPS의 최저가"라 비싼 옵션 붙은 활이 애초에 안 들어오기 때문이다.
# stat id 는 서버·언어와 무관하게 같으므로 문구가 아니라 id 로 고정한다.
# 기본 문턱값은 실측으로 매물이 충분히 남는 선에서 잡았다(DPS 550+ 기준 괄호 안이 매물 수).
# 옵션마다 문턱값을 여럿 두면 "좋은 굴림일수록 얼마나 더 비싼가"까지 보인다.
PRIORITY_STATS = [
    ("explicit.stat_518292764",  "치명타 확률 +{}%",        [2, 4]),    # DPS550+ 기준 3,055개
    ("explicit.stat_2694482655", "치명타 피해 보너스 +{}%",   [20, 30]),  #   535개
    ("explicit.stat_1202301673", "모든 투사체 스킬 레벨 +{}",  [1, 2]),    # 1,557개
    ("explicit.stat_803737631",  "정확도 +{}",              [100]),
    # 라벨은 실제 옵션 문구 그대로 써야 한다 — "생명력 흡수 #%" 는 거래소에 없는 문구다
    ("explicit.stat_2557965901", "물리 공격 피해의 {}%를 생명력으로 흡수", [6]),
]

# 카탈로그는 "+"를 안 쓰고(민첩 #), 음수 옵션도 증가로 적는다(아이템은 감소로 표시)
_MARKUP = re.compile(r"\[([^\]|]*)\|([^\]]*)\]")
_BRACKET = re.compile(r"\[([^\]]*)\]")


def clean_mod(m):
    return _BRACKET.sub(r"\1", _MARKUP.sub(r"\2", m))


def mod_key(m):
    k = re.sub(r"[\d.]+", "#", clean_mod(m))
    k = re.sub(r"\+\s*#", "#", k)
    return re.sub(r"\s+", " ", k).strip()


def mod_val(m):
    n = re.search(r"[\d.]+", clean_mod(m))
    return float(n.group()) if n else 0.0


# GGG 가 준 pdps/edps 에 이미 들어 있는 옵션 = 조건으로 따로 보여줄 이유가 없는 옵션.
# 한국어 쪽은 반드시 줄 맨 앞에 붙여 잡는다. `공격 속도.*증가` 처럼 느슨하게 두면
# "반려수의 공격 속도 14% 증가"(동료 펫 옵션, 무기 DPS 와 무관)까지 삼켜서 조건 목록에서
# 통째로 사라진다 — 실측 473개 중 241개가 이 옵션을 갖고 있었다. `피해.*추가` 도 마찬가지로
# "피해의 #%를 추가 카오스 피해로 획득"을 잘못 잡아서, 실제 "화염 피해 10~16 추가" 형태만
# 잡도록 범위 표기를 요구한다.
COUNTED_KR = [re.compile(p) for p in
              (r"^물리 피해 [\d.]+% 증가", r"피해 \d+~\d+ 추가", r"^공격 속도 [\d.]+% 증가",
               r"increased Physical Damage", r"Adds \d", r"increased Attack Speed")]


def is_off_dps(m):
    c = clean_mod(m)
    return not any(p.search(c) for p in COUNTED_KR)


def stat_catalog(base):
    """옵션 문구 -> stat id. 91% 매칭됨(남은 건 숫자가 둘인 복합 옵션)."""
    data = api_get("%s/api/trade2/data/stats" % base)
    cat = {}
    for grp in data.get("result", []):
        for e in grp.get("entries", []):
            t = re.sub(r"\s+", " ", (e.get("text") or "")).strip()
            cat.setdefault(t, e.get("id"))
    return cat


# 곡선을 뜰 가치가 없는 옵션들.
# rune. 은 룬으로 박은 것이라 활 자체의 값이 아니고, implicit. 은 베이스 고유라
# 같은 베이스면 전부 갖고 있어 구분이 안 된다 — 둘 다 "얼마를 더 내는가"의 답이 못 된다.
JUNK_PREFIX = ("rune.", "implicit.", "pseudo.")
# 활 성능과 무관하거나 아무도 안 찾는 옵션 — 곡선을 떠도 볼 사람이 없다.
# 페이지(index.html)의 JUNK_MOD 와 같은 목록이어야 한다. 투사체 사거리가 페이지에만 있어서
# 수집기가 이걸 조건으로 골라 요청을 낭비하고, 페이지는 그걸 다시 안 거르고 그대로 보여줬다.
JUNK_WORDS = ("시야 반경", "Light Radius", "능력치 요구사항", "Attribute Requirement",
              "투사체 사거리", "Projectile Range")
# 능력치 자체는 착용 요구치를 맞추는 용도라 DPS 와 무관하다. 다만 "민첩 10당 공격 속도"
# 처럼 능력치를 조건으로 삼는 진짜 옵션이 있으므로, 부분 일치가 아니라 정확히 일치할 때만 뺀다.
JUNK_KEYS = {"민첩 #", "힘 #", "지능 #", "모든 능력치 #",
             "Dexterity #", "Strength #", "Intelligence #", "All Attributes #"}
MIN_OBSERVED = 3         # 관측이 이보다 적으면 문턱값을 정할 근거가 없다


def is_useful_stat(sid, key):
    # "감소"가 붙은 옵션은 조건이 될 수 없다. 카탈로그는 음수 옵션을 "증가" 항목으로만 싣고
    # (resolve_stat 이 그렇게 뒤집어 찾는다) mod_val 은 부호를 버리므로, 관측한 "50% 감소"가
    # 검색에서는 `min: 50` = "50% 이상 증가"가 된다 — 정반대 집단을 재게 된다.
    # 실측: 투사체 사거리 감소(247건)가 이 경로로 조건에 뽑히고 있었다.
    return (sid and not sid.startswith(JUNK_PREFIX)
            and key not in JUNK_KEYS
            and "감소" not in key and "reduced" not in key.lower()
            and not any(w in key for w in JUNK_WORDS))


def resolve_stat(cat, key):
    if key in cat:
        return cat[key]
    flip = key.replace("감소", "증가")          # 음수는 카탈로그에 증가로 실려 있다
    return cat.get(flip) if flip != key else None


def pick_conditions(bows, cat):
    """검색에 걸 옵션 조건을 고른다.

    우선 목록(구매자가 값을 치르는 옵션)을 먼저 넣고, 남는 자리만 빈도로 채운다.
    우선 목록은 문턱값이 코드에 박혀 있고(steps), 빈도로 채우는 쪽만 관측값 상위 25% 지점을 쓴다.
    """
    seen = {}
    for b in bows:
        for m in b.get("mods") or []:
            if is_off_dps(m):
                seen.setdefault(mod_key(m), []).append(mod_val(m))
    by_id = {}
    for key, vals in seen.items():
        sid = resolve_stat(cat, key)
        if is_useful_stat(sid, key):
            by_id.setdefault(sid, (key, sorted(vals)))

    def threshold(sid, fallback):
        got = by_id.get(sid)
        if not got or not got[1]:
            return fallback
        v = got[1]
        return round(v[int(len(v) * 0.75)] if len(v) > 3 else v[-1], 1)

    out, used = [], set()
    for sid, label, steps in PRIORITY_STATS:              # 1) 구매자 우선 옵션
        for v in steps:
            # key 는 반드시 mod_key 를 통과시킨다. 안 그러면 "치명타 확률 +#%" 를 내보내는데
            # 실제 옵션 key 는 "치명타 확률 #%" 라서, 페이지가 측정/표본을 같은 옵션으로 못 묶는다.
            out.append({"id": sid, "label": label.format(("%g" % v)) + " 이상",
                        "key": mod_key(label.format("#")), "min": v,
                        "n": len(by_id.get(sid, ("", []))[1]), "why": "우선"})
        used.add(sid)
    if len(out) >= COND_MODS:
        return out[:COND_MODS]
    for key, vals in sorted(seen.items(), key=lambda kv: -len(kv[1])):   # 2) 나머지는 빈도순
        sid = resolve_stat(cat, key)
        if sid in used or not is_useful_stat(sid, key):
            continue
        thr = threshold(sid, 0)
        if thr <= 0 or len(vals) < MIN_OBSERVED:
            continue        # 관측 1~2건으로 문턱값을 잡으면 그 한 건이 곧 조건이 된다
        # 라벨의 "#" 를 문턱값으로 채운다 — "#% 증가" 같은 빈칸 라벨이 화면에 그대로 노출됐었다
        out.append({"id": sid, "label": key.replace("#", "%g" % thr) + " 이상", "key": key,
                    "min": thr, "n": len(vals), "why": "빈도"})
        used.add(sid)
        if len(out) >= COND_MODS:
            break
    return out


def sweep_condition(base, league_path, query, cond):
    """한 조건에 대해 DPS 문턱값을 훑어 최저가만 남긴다."""
    import copy
    pts = []
    for lo in COND_DPS:
        q = copy.deepcopy(query)
        q.setdefault("filters", {}).setdefault("equipment_filters", {}) \
         .setdefault("filters", {})["dps"] = {"min": lo}
        # 사용자가 저장한 검색에 이미 옵션 조건이 걸려 있을 수 있다. 통째로 덮어쓰면
        # 기준 곡선(그 조건을 그대로 둔 채 검색)과 **다른 집단**을 재게 되어, 비교 자체가
        # 성립하지 않고 조건 곡선이 기준보다 싸게 나오는 모순까지 생긴다.
        # 우리 조건은 기존 묶음에 한 덩어리로 덧붙인다.
        q["stats"] = list(q.get("stats") or []) + [
            {"type": "and", "filters": [{"id": cond["id"], "value": {"min": cond["min"]}}]}]
        throttle("search")
        r = api_get(base + league_path, {"query": q, "sort": {"price": "asc"}})
        ids = (r.get("result") or [])[:1]
        if not ids:
            print("     · DPS %s+ : 매물 없음" % lo)
            continue
        rows = fetch_ids(base, r["id"], ids)
        if rows:
            rows[0]["cond"] = cond["label"]
            pts.append(rows[0])
            print("     · DPS %s+ : %s개 중 최저 %g %s"
                  % (lo, r.get("total"), rows[0]["price"], rows[0]["cur"]))
    return pts


def search_api_url(page_url):
    """거래소의 '저장된 검색' 링크만 통과시키고 (호스트, API 경로) 를 돌려준다.

    진입점이 둘(--collect 의 resolve_search, /api/trade 의 load_trade)인데 검사가 따로 있었고
    resolve_search 쪽에만 경로 검사가 빠져 있었다 — 그래서 `/account/view-profile/...` 같은
    아무 페이지나 그대로 요청으로 나갔고(실측), 돌아온 HTML 을 JSON 으로 읽다 죽었다.
    검사는 여기 한 곳에만 둔다.
    """
    u = urlparse(page_url.strip())
    if u.scheme not in ("http", "https") or u.hostname not in ALLOWED_HOSTS:
        raise TradeError("거래소 주소가 아닙니다. 예: https://www.pathofexile.com/trade2/search/poe2/...")
    if "/trade2/search/" not in u.path:
        raise TradeError("저장된 '검색' 링크여야 합니다(주소에 /trade2/search/ 가 있어야 함).")
    return ("%s://%s" % (u.scheme, u.hostname),
            u.path.replace("/trade2/search/", "/api/trade2/search/", 1))


def resolve_search(page_url):
    """저장된 검색 링크에서 리그 주소와 질의문을 꺼낸다.
    환율을 먼저 뜨려면 아이템을 긁기 전에 리그를 알아야 한다."""
    base, api_path = search_api_url(page_url)
    league_path = api_path.rsplit("/", 1)[0]
    # 교환 API 경로에는 "poe2/" 가 없다: /api/trade2/exchange/<리그>
    league = league_path.rsplit("/", 1)[-1]
    query = api_get(base + api_path).get("query")
    if query is None:
        raise TradeError("저장된 검색을 읽지 못했습니다. 링크를 다시 확인해주세요.")
    return base, league_path, league, query


def load_banded(page_url, per_band):
    """DPS 문턱값을 훑는다. "dps >= T 중 최저가"가 곧 최전선 위의 점이다.
    한 번의 검색은 id 를 100개까지만 주므로 구간을 안 나누면 표본이 시장 바닥에만 몰린다."""
    base, league_path, league, query = resolve_search(page_url)

    out, seen, total, skipped = [], set(), 0, 0
    for lo in THRESHOLDS:
        throttle("search")
        r = search_min_dps(base, league_path, query, lo)
        ids = [i for i in (r.get("result") or [])[:per_band] if i not in seen]
        seen.update(ids)
        total = max(total, r.get("total") or 0)
        rows = fetch_ids(base, r["id"], ids) if ids else []
        out += rows
        # normalize() 가 되돌린 것 = 실거래 화폐 넷 밖이거나 GGG 가 DPS 를 안 준 매물.
        # 여기서 안 세면 "제외 0" 이 그냥 고정 문구가 된다 — 실제로 그랬다(수집 로그 전부 0).
        # 버려지는 게 대부분 싼 매물이라(화폐 필터 이전 스냅샷 #3 은 120개 중 51개가
        # transmute/aug 였다) 조용히 빠지면 곡선 아래쪽이 통째로 들린다.
        skipped += len(ids) - len(rows)
        # 거래소가 가격 오름차순으로 줬으므로 첫 줄이 최저가다. 화폐가 섞여 있어
        # 금액만 비교하면 안 된다 (1 divine 이 10 exalted 보다 싸 보인다).
        head_row = rows[0] if rows else None
        print("     DPS %s 이상: 매물 %s개 중 %d개 수집%s"
              % (lo, r.get("total"), len(rows),
                 "" if not head_row else " (최저 %g %s)" % (head_row["price"], head_row["cur"])))
        time.sleep(PAUSE)
    return out, total, skipped, base, league


# 화면에서 누르는 경로는 사람이 연타할 수 있다. 오래 자면 브라우저가 멈춘 것처럼 보이니,
# 벽 앞이면 길게 기다리는 대신 바로 "잠시 뒤에" 라고 답하고 끝낸다.
UI_MAX_WAIT = 20


def load_trade(page_url, limit):
    """화면의 '불러오기' 경로. 수집기와 달리 여기엔 스로틀이 아예 없었다 —
    개수 100 이면 한 번 누를 때 요청이 12개(검색 2 + fetch 10) 나가는데,
    실측 한도는 fetch 가 4초당 12회 / 12초당 16회다. 두 번만 눌러도 벽에 닿아
    429 와 IP 차단으로 이어진다. 수집기와 같은 버킷으로 같이 재도록 붙였다."""
    base, api_path = search_api_url(page_url)
    throttle("search", UI_MAX_WAIT)
    head = api_get(base + api_path)

    # 저장된 검색을 GET 하면 {id, query} 만 온다 — 결과를 받으려면 그 질의문을 리그 주소로 다시 POST.
    if "result" not in head and head.get("query") is not None:
        league_path = api_path.rsplit("/", 1)[0]        # 해시를 떼면 리그 주소
        throttle("search", UI_MAX_WAIT)
        head = api_get(base + league_path, {"query": head["query"]})

    query_id, ids = head.get("id"), head.get("result") or []
    if not query_id or not ids:
        return [], head.get("total", 0), 0

    out = []
    wanted = ids[:limit]
    for i in range(0, len(wanted), FETCH_BATCH):
        batch = wanted[i:i + FETCH_BATCH]
        throttle("fetch", UI_MAX_WAIT)
        data = api_get("%s/api/trade2/fetch/%s?query=%s" % (base, ",".join(batch), query_id))
        for res in data.get("result") or []:
            if res:
                row = normalize(res)
                if row:
                    out.append(row)
        if i + FETCH_BATCH < len(wanted):
            time.sleep(PAUSE)
    return out, head.get("total", len(ids)), len(wanted) - len(out)


# 페이지가 실제로 필요한 파일만 내보낸다. 폴더를 통째로 열면 snapshots.db 나
# 나중에 누가 여기 둘 파일까지 브라우저로 새어 나간다.
SERVED = {"/", "/index.html", "/latest.json", "/favicon.ico", "/favicon.png", "/og.png",
          "/poe2-bow-harvester.user.js"}   # 채집기 설치 링크용 (Tampermonkey 가 .user.js 를 감지)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def local_only(self):
        """Host 헤더가 로컬인지 본다.

        이게 없으면 DNS 리바인딩에 뚫린다: 공격자가 자기 도메인을 127.0.0.1 로
        가리키게 해두면 브라우저가 그 도메인을 오리진으로 여겨 같은 출처가 되고,
        /api/trade 응답을 그대로 읽어간다. 리바인딩 요청은 Host 에 공격자 도메인이
        실려 오므로 여기서 걸러진다.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host in ("localhost", "127.0.0.1", "::1", ""):
            return True
        self.send_json(403, {"error": "이 서버는 로컬에서만 씁니다 (Host: %s)" % host})
        return False

    def send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        # SimpleHTTPRequestHandler 가 do_HEAD 를 이미 갖고 있어서, do_GET 에만 검사를 걸면
        # HEAD 로 전부 우회된다(실측: HEAD /snapshots.db 가 200 에 Content-Length 노출,
        # 공격자 Host 로도 200). 본문은 안 나가지만 파일 존재·크기가 새고 리바인딩 방어가 뚫린다.
        if not self.local_only():
            return
        if urlparse(self.path).path not in SERVED:
            return self.send_json(404, {"error": "없는 경로입니다"})
        return super().do_HEAD()

    def do_GET(self):
        if not self.local_only():
            return
        path = urlparse(self.path).path
        if path == "/api/ping":
            return self.send_json(200, {"ok": True, "poesessid": bool(os.environ.get("POESESSID"))})
        if path == "/api/trade":
            qs = parse_qs(urlparse(self.path).query)
            url = (qs.get("url") or [""])[0]
            try:
                limit = max(1, min(100, int((qs.get("limit") or ["%d" % DEFAULT_LIMIT])[0])))
            except ValueError:
                limit = DEFAULT_LIMIT
            try:
                bows, total, skipped = load_trade(url, limit)
            except TradeError as e:
                return self.send_json(200, {"error": str(e)})
            except Exception as e:                       # 예상 못 한 응답 형태까지 화면에 보이게
                return self.send_json(200, {"error": "%s: %s" % (type(e).__name__, e)})
            return self.send_json(200, {"bows": bows, "total": total, "skipped": skipped})
        if path not in SERVED:
            return self.send_json(404, {"error": "없는 경로입니다"})
        return super().do_GET()

    def log_message(self, fmt, *args):
        # 요청 로그는 /api/ 만 남기고 정적 파일은 조용히 넘긴다. 다만 args[0] 이 늘 문자열은
        # 아니다 — send_error 는 `log_error("code %d, message %s", HTTPStatus.X, msg)` 로
        # **HTTPStatus 를** 넘긴다. 문자열로 가정하면 여기서 TypeError 가 나고, 그게 send_error
        # 밖으로 터져 나가 응답을 한 줄도 못 쓴 채 연결이 끊긴다.
        # 실측: /favicon.ico(파일이 없어 404)·OPTIONS·POST 가 전부 "Empty reply from server".
        # 오류 로그는 조용히 삼키면 안 되므로 문자열이 아닐 때는 그대로 남긴다.
        first = args[0] if args else ""
        if not isinstance(first, str) or "/api/" in first:
            super().log_message(fmt, *args)


"""수집기 — 시세를 통째로 떠서 DB 에 쌓고, 페이지가 열자마자 읽을 latest.json 을 쓴다.

    python serve.py --collect "<거래소 검색 URL>"      한 번 뜬다
    python serve.py --collect "<URL>" --every 3600    한 시간마다 반복
    python serve.py --collect --every 3600            지난번 URL 그대로 반복
    python serve.py --collect --every 3600 --push     수집 후 공개 페이지(GitHub Pages)까지 갱신

GitHub Actions 로 옮겨도 부르는 명령은 똑같다 — 호스트에 안 묶여 있다.
"""
DB = os.path.join(ROOT, "snapshots.db")
LATEST = os.path.join(ROOT, "latest.json")


def db():
    con = sqlite3.connect(DB)
    con.executescript("""
      CREATE TABLE IF NOT EXISTS snapshots(
        id INTEGER PRIMARY KEY, taken_at INTEGER NOT NULL,
        source_url TEXT NOT NULL, total INTEGER, kept INTEGER, rates TEXT);
      CREATE TABLE IF NOT EXISTS bows(
        snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
        name TEXT, pdps REAL, edps REAL, aps REAL, crit REAL,
        price REAL, cur TEXT, rarity TEXT, mods TEXT);
      CREATE INDEX IF NOT EXISTS bows_snap ON bows(snapshot_id);
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(snapshots)")}
    if "rates" not in cols:                      # 예전 스냅샷 DB 를 그대로 이어 쓰기 위한 이관
        con.execute("ALTER TABLE snapshots ADD COLUMN rates TEXT")
    if "cond" not in {r[1] for r in con.execute("PRAGMA table_info(bows)")}:
        con.execute("ALTER TABLE bows ADD COLUMN cond TEXT")
    return con


def last_url():
    if not os.path.exists(DB):
        return None
    with db() as con:
        row = con.execute("SELECT source_url FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def write_latest(payload, path=None):
    """옆에 새로 쓰고 성공했을 때만 갈아끼운다.

    `open(path, "w")` 는 여는 순간 파일을 비우고 json.dump 는 거기에 흘려 쓴다 —
    도중에 실패하면(직렬화 오류, Ctrl+C, 정전) 지난 스냅샷이 반토막 난 채 남고
    페이지는 그걸 아예 못 읽는다. 실측: 매물 하나가 직렬화에 실패했더니
    77720 -> 38931 바이트로 잘리고 JSON 파싱 불가.
    """
    path = path or LATEST
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    # 같은 폴더 안이면 원자적이다. 단 윈도우는 **대상 파일을 누가 읽는 중이면**
    # os.replace 를 WinError 5 로 거절한다 — 서버가 latest.json 을 내보내는 순간과 겹치면
    # 이번 수집분이 통째로 버려진다(실측: 서버가 계속 읽는 동안 40번 중 20번 실패).
    # 문서에 적힌 사용법이 서버와 수집기를 같이 띄우는 것이라 드문 일이 아니다.
    # 읽는 시간은 밀리초 단위라 잠깐 기다렸다 다시 걸면 통과한다. 원본은 실패해도 안 건드려진다.
    err = None
    for wait in (0, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6):
        if wait:
            time.sleep(wait)
        try:
            return os.replace(tmp, path)
        except PermissionError as e:
            err = e
    raise err


def collect(url, limit=100):
    """한 시점의 시세를 통째로 뜬다. 스냅샷 하나 = 한 시점 = 시점이 섞일 수 없다."""
    taken = int(time.time() * 1000)

    # 환율을 먼저 뜬다. 곡선의 세로 축척 전체가 여기 걸려 있고, 요청도 훨씬 적어서
    # 문제가 있으면 4분짜리 아이템 수집을 시작하기 전에 드러난다.
    base, _, league, q0 = resolve_search(url)
    # 사이트가 "즉시 구매 가능 매물만 집계"라고 공언한다 — 저장 검색이 바뀌면 그 문구가
    # 거짓말이 되므로, securable 이 아니면 크게 알린다(수집은 계속한다).
    if (q0.get("status") or {}).get("option") != "securable":
        print("     !! 경고: 저장 검색의 상태가 '즉시 구매 가능'(securable)이 아닙니다: %r"
              % (q0.get("status"),))
        print("     !! 사이트 문구와 어긋납니다 — 검색 필터를 확인하세요.")
    rates = fetch_rates(base, league, TRADE_CURRENCIES)

    bows, total, skipped, base, league = load_banded(url, limit)

    # 옵션 조건 곡선 — 검색 100개 상한 때문에 이것만이 옵션 프리미엄을 재는 방법이다
    conds = []
    try:
        b2, league_path, _, query = resolve_search(url)
        cat = stat_catalog(b2)
        conds = pick_conditions(bows, cat)
        for c in conds:
            print("     조건 [%s >= %g] (%s, 표본 관측 %d건)"
                  % (c["label"], c["min"], c.get("why", ""), c["n"]))
            bows += sweep_condition(b2, league_path, query, c)
    except TradeError as e:
        print("     조건 곡선 건너뜀: %s" % e)
    with db() as con:
        sid = con.execute(
            "INSERT INTO snapshots(taken_at, source_url, total, kept, rates) VALUES (?,?,?,?,?)",
            (taken, url, total, len(bows), json.dumps(rates, ensure_ascii=False))).lastrowid
        con.executemany(
            "INSERT INTO bows(snapshot_id,name,pdps,edps,aps,crit,price,cur,rarity,mods,cond) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, b["name"], b["pdps"], b["edps"], b["aps"], b["crit"],
              b["price"], b["cur"], b["rarity"], json.dumps(b["mods"], ensure_ascii=False),
              b.get("cond")) for b in bows])
    # 페이지는 최신 한 장만 읽는다. 과거는 DB 에만 쌓인다.
    write_latest({"taken_at": taken, "total": total, "skipped": skipped,
                  "rates": rates, "conds": conds, "bows": bows})
    print("[%s] %d개 저장 (검색 결과 %d, 제외 %d) → latest.json"
          % (time.strftime("%H:%M:%S"), len(bows), total, skipped))
    for name, b in RATE_STATE.items():
        if b.get("state"):
            print("     레이트 리밋 %-12s %s / 한도 %s" % (name, b["state"], b.get("rules")))
    if "--push" in sys.argv:                    # 데모(--test)는 이 플래그가 없어 안 탄다
        push_latest()
    return len(bows)


def push_latest(cwd=None):
    """--push: 수집 뒤 latest.json 을 저장소로 밀어 올린다 = 공개 페이지 갱신.
    실패해도 수집은 계속 돈다 — 푸시는 배포일 뿐 수집이 아니다."""
    import subprocess
    def run(*cmd):
        return subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    run("git", "add", "latest.json")
    c = run("git", "commit", "-m", "시세 갱신 " + time.strftime("%Y-%m-%d %H:%M"))
    if c.returncode != 0:
        out = (c.stdout or "") + (c.stderr or "")
        if "nothing to commit" in out or "변경 사항 없음" in out:
            print("     푸시 생략 — 지난번과 같은 스냅샷"); return "same"
        print("     커밋 실패: %s" % out.strip()[:120]); return "commit-fail"
    p = run("git", "push")
    if p.returncode == 0:
        print("     공개 페이지로 밀어 올림"); return "pushed"
    print("     푸시 실패(수집은 정상): %s" % ((p.stderr or p.stdout) or "").strip()[:120])
    return "push-fail"


def collect_loop(url, every, limit):
    while True:
        try:
            collect(url, limit)
        except TradeError as e:
            print("[%s] 수집 실패: %s" % (time.strftime("%H:%M:%S"), e))
        except Exception as e:                  # 한 번 실패했다고 루프까지 죽으면 안 된다
            print("[%s] 수집 오류: %s: %s" % (time.strftime("%H:%M:%S"), type(e).__name__, e))
        print("     다음 수집까지 %d초 대기 (Ctrl+C 로 종료)" % every)
        time.sleep(every)


def demo():
    """의존성 없이 순수 함수만 확인 — python serve.py --test"""
    assert to_number("1.42") == 1.42 and to_number("6.50%") == 6.5 and to_number(None) is None
    item = {"properties": [{"name": "Attacks per Second", "values": [["1.42", 1]]},
                           {"name": "치명타 명중 확률", "values": [["6.50%", 0]]}]}
    assert prop(item, r"Attacks per Second") == "1.42"
    assert prop(item, r"Critical .*Chance", r"치명타") == "6.50%"
    assert prop(item, r"Weapon Range") is None

    assert mod_lines({"explicitMods": ["38% increased Critical Hit Chance"],
                      "implicitMods": [{"description": "+3 to Level of all Skills"}],
                      "runeMods": [{"hash": "x"}]}) ==         ["+3 to Level of all Skills", "38% increased Critical Hit Chance"]
    assert mod_lines({}) == []

    # 환율: 직접 매물이 있으면 그걸 쓰고, 없으면 다른 화폐를 거쳐 메운다
    assert best_offer([300, 400, 449]) == 300
    assert best_offer([0.125, 0.164, 1, 1, 1, 7]) == 0.125   # 1:1 사기 4건은 최저가가 무시

    full = {("exalted", "chaos"): [60, 61, 59],
            ("exalted", "divine"): [300, 305],
            ("exalted", "annul"): [250],
            ("chaos", "divine"): [5, 5.1], ("divine", "chaos"): [1 / 5],
            ("chaos", "annul"): [4.2], ("annul", "divine"): [1 / 0.83]}
    v, how = solve_rates(full)
    assert (v["chaos"], v["divine"], v["annul"]) == (59, 300, 250), v
    assert how["divine"] == "직접"

    no_direct = {k: x for k, x in full.items() if k != ("exalted", "divine")}
    v2, how2 = solve_rates(no_direct)
    assert 0.9 * 300 <= v2["divine"] <= 1.02 * 300, v2["divine"]   # 카오스 경유로 복원
    assert how2["divine"].startswith("우회"), how2["divine"]

    # 직접 매물이 있으면 우회값이 더 싸도 직접을 쓴다 (다리를 건널수록 잡음이 곱해진다)
    tempting = dict(full)
    tempting[("chaos", "divine")] = [0.001]
    v3, how3 = solve_rates(tempting)
    assert v3["divine"] == 300 and how3["divine"] == "직접", (v3["divine"], how3["divine"])

    assert solve_rates({})[0] == {"exalted": 1.0}                  # 관측 없으면 지어내지 않는다

    # 옵션 문구 정규화 — 카탈로그 표기(부호 없음, 음수도 증가)에 맞춰야 stat id 가 붙는다
    assert clean_mod("[Physical|물리] 피해 168% 증가") == "물리 피해 168% 증가"
    assert mod_key("[Dexterity|민첩] +29") == "민첩 #"
    assert mod_key("[Accuracy|정확도] +58") == "정확도 #"
    assert mod_key("모든 투사체 스킬 레벨 +3") == "모든 투사체 스킬 레벨 #"
    assert mod_val("[Accuracy|정확도] +58") == 58
    assert not is_off_dps("[Physical|물리] 피해 168% 증가")     # DPS 에 반영됨
    assert not is_off_dps("[Attack|공격] 속도 16% 증가")
    assert is_off_dps("[Accuracy|정확도] +58")

    cat = {"민첩 #": "explicit.stat_3261801346",
           "능력치 요구사항 #% 증가": "explicit.stat_3639275092"}
    assert resolve_stat(cat, "민첩 #") == "explicit.stat_3261801346"
    assert resolve_stat(cat, "능력치 요구사항 #% 감소") == "explicit.stat_3639275092"   # 음수 뒤집기
    assert resolve_stat(cat, "없는 옵션 #") is None

    # 곡선 뜰 가치가 없는 것 걸러내기
    assert not is_useful_stat("rune.stat_1039491398", "결속됨: 방어구 효과 #% 증가")
    assert not is_useful_stat("implicit.stat_3398402065", "투사체 사거리 #% 감소")
    assert not is_useful_stat("explicit.stat_1", "시야 반경 #% 증가")
    assert not is_useful_stat("explicit.stat_3639275092", "능력치 요구사항 #% 감소")
    assert not is_useful_stat("explicit.stat_3261801346", "민첩 #")
    assert not is_useful_stat("explicit.stat_x", "모든 능력치 #")
    # 능력치를 조건으로 삼는 진짜 옵션은 살아남아야 한다 (부분 일치로 빼면 안 되는 이유)
    assert is_useful_stat("explicit.stat_889691035", "민첩 10당 공격 속도 #% 증가")
    assert is_useful_stat("explicit.stat_518292764", "치명타 확률 #%")

    # 조건 고르기: 구매자 우선 옵션이 먼저, 남는 자리만 빈도순
    fake = [{"mods": ["[Accuracy|정확도] +%d" % v]} for v in (10, 20, 30, 40, 50)]
    fake += [{"mods": ["[Physical|물리] 피해 100% 증가"]}] * 9      # DPS 반영분은 후보에서 빠져야
    cat = {"정확도 #": "explicit.stat_803737631", "치명타 확률 #%": "explicit.stat_518292764"}
    picked = pick_conditions(fake, cat)
    assert picked[0]["id"] == PRIORITY_STATS[0][0] and picked[0]["why"] == "우선"
    # 표본에 없어도 기본 문턱값으로 들어간다 — 빈도로만 뽑으면 치명타는 영영 안 잡힌다
    assert picked[0]["min"] == PRIORITY_STATS[0][2][0]
    assert picked[1]["min"] == PRIORITY_STATS[0][2][1]     # 같은 옵션의 두 번째 문턱값

    # 표본에서 관측되면 그 값(상위 25% 지점)이 기본값을 대신한다
    assert len({c["label"] for c in picked}) == len(picked)   # 라벨이 겹치면 곡선이 뭉개진다
    # 페이지가 표본 조건과 겹치는지 알려면 정규화 키가 있어야 한다
    # 우선 옵션의 key 는 실제 옵션에서 뽑은 key 와 글자까지 같아야 한다.
    # 어긋나면 페이지가 "측정"과 "표본"을 다른 옵션으로 보고 두 줄로 늘어놓는다.
    for _sid, _label, _steps in PRIORITY_STATS:
        emitted = mod_key(_label.format("#"))
        assert emitted == mod_key(emitted), (_label, emitted)      # 이미 정규형이어야 함
        assert "+#" not in emitted, (_label, emitted)              # 부호가 남으면 안 됨
    assert mod_key("치명타 확률 +#%") == "치명타 확률 #%"
    assert {c["key"] for c in picked} >= {"치명타 확률 #%"}, picked
    assert all("key" in c for c in picked)

    assert rarity_of({"rarity": "Unique"}) == "Unique"
    assert rarity_of({"frameType": 13}) == "Rare"      # 룬 박힌 희귀도 희귀
    assert rarity_of({"frameType": 3}) == "Unique"
    assert rarity_of({}) == ""

    row = normalize({"item": {"name": "파멸의 노래", "typeLine": "고급 광신도 활", "rarity": "Rare",
                              "extended": {"pdps": 112.5, "edps": 22.5}, **item},
                     "listing": {"price": {"amount": 9, "currency": "divine"}}})
    assert row["name"] == "파멸의 노래 고급 광신도 활" and row["pdps"] == 112.5 and row["cur"] == "divine"
    assert row["rarity"] == "Rare"
    assert normalize({"item": {"extended": {"pdps": 1}}, "listing": {}}) is None          # 값 없음
    assert normalize({"item": {"extended": {}}, "listing":                                 # 활 아님
                      {"price": {"amount": 1, "currency": "divine"}}}) is None
    # 화폐는 뭐가 오든 살린다 — 버리면 매물 대부분이 사라지는 걸 실측으로 확인했다
    # 실거래 화폐 넷만 받는다
    assert normalize({"item": {"extended": {"pdps": 1}}, "listing":
                      {"price": {"amount": 1, "currency": "transmute"}}}) is None
    assert normalize({"item": {"extended": {"pdps": 1}}, "listing":
                      {"price": {"amount": 1, "currency": "chaos"}}})["cur"] == "chaos"

    # Host 헤더 검사 (DNS 리바인딩 방어)
    class FakeHandler:
        local_only = Handler.local_only
        def __init__(self, host): self.headers = {"Host": host}; self.denied = None
        def send_json(self, code, payload): self.denied = (code, payload)
    for host, ok in [("localhost:8731", True), ("127.0.0.1:8731", True), ("[::1]:8731", True),
                     ("evil.example.com", False), ("evil.example.com:8731", False)]:
        h = FakeHandler(host)
        assert h.local_only() is ok, (host, ok)
        assert (h.denied is None) is ok

    assert "/latest.json" in SERVED and "/snapshots.db" not in SERVED

    # 로그 필터가 문자열이 아닌 인자에 터지면 send_error 가 응답을 못 쓰고 연결이 끊긴다
    from http import HTTPStatus
    import contextlib, io as _io
    _h = Handler.__new__(Handler)
    _h.client_address = ("127.0.0.1", 1)
    _buf = _io.StringIO()
    with contextlib.redirect_stderr(_buf):
        _h.log_message("code %d, message %s", HTTPStatus.NOT_IMPLEMENTED, "Unsupported")
        _h.log_message('"%s" %s %s', "GET /index.html HTTP/1.1", "200", "-")
        _h.log_message('"%s" %s %s', "GET /api/ping HTTP/1.1", "200", "-")
    _out = _buf.getvalue()
    assert "Unsupported" in _out, "오류 로그가 사라졌다: %r" % _out      # 오류는 남긴다
    assert "/api/ping" in _out, _out
    assert "/index.html" not in _out, "정적 요청까지 로그에 남는다: %r" % _out

    # 새로 추가되는 do_* 메서드도 반드시 Host 검사를 거쳐야 한다 —
    # do_GET 에만 걸어뒀다가 HEAD 로 통째로 우회당한 적이 있다.
    import inspect
    handlers = [n for n in Handler.__dict__ if n.startswith("do_")]
    assert set(handlers) >= {"do_GET", "do_HEAD"}, handlers
    for _n in handlers:
        assert "local_only" in inspect.getsource(Handler.__dict__[_n]), _n

    for bad in ["https://evil.example.com/trade2/search/poe2/x",
                "file:///etc/passwd",
                "https://www.pathofexile.com/account/view-profile/x"]:
        try:
            load_trade(bad, 1)
            raise AssertionError("차단됐어야 함: " + bad)
        except TradeError:
            pass
    # load_banded 는 normalize() 가 버린 매물 수를 실제로 세야 한다.
    # 예전엔 0 을 그대로 돌려줘서 "제외 N" 이 언제나 0 인 고정 문구였다.
    global PAUSE
    keep_pause = PAUSE
    saved = {k: globals()[k] for k in ("resolve_search", "search_min_dps", "fetch_ids", "throttle")}
    try:
        PAUSE = 0
        globals()["throttle"] = lambda b: None
        globals()["resolve_search"] = lambda u: ("https://h", "/api/trade2/search/poe2/L", "L", {})
        globals()["search_min_dps"] = lambda b, lp, q, lo: {
            "id": "q", "total": 7, "result": ["a", "b", "c"]}   # 세 밴드 모두 같은 id -> 첫 밴드만 샌다
        # 셋을 달라 했는데 하나만 살아 돌아옴 (화폐가 규격 밖이거나 DPS 가 없어서)
        globals()["fetch_ids"] = lambda b, qid, ids: [{"price": 1, "cur": "divine"}] if ids else []
        rows, total, skipped, _, _ = load_banded("https://h/trade2/search/poe2/L/x", 3)
        assert len(rows) == 1, rows
        assert skipped == 2, "버려진 매물을 안 셌다: %r" % skipped
        assert total == 7, total

        globals()["fetch_ids"] = lambda b, qid, ids: [{"price": 1, "cur": "divine"} for i in ids]  # 전부 살아 오면 0
        assert load_banded("https://h/trade2/search/poe2/L/x", 3)[2] == 0
    finally:
        PAUSE = keep_pause
        globals().update(saved)

    # 수집기: 임시 DB 로 저장 -> 조회 왕복까지 확인
    global DB, LATEST
    import tempfile
    keep = (DB, LATEST)
    d = tempfile.mkdtemp()
    DB, LATEST = os.path.join(d, "t.db"), os.path.join(d, "t.json")
    try:
        real = globals()["load_banded"]
        real_rates = globals()["fetch_rates"]
        real_resolve = globals()["resolve_search"]
        real_cat, real_sweep = globals()["stat_catalog"], globals()["sweep_condition"]
        # 자체 검증은 절대 네트워크를 타면 안 된다 — collect() 가 부르는 것은 전부 대역한다
        globals()["stat_catalog"] = lambda b: {"치명타 확률 #%": "explicit.stat_518292764"}
        globals()["sweep_condition"] = lambda b, lp, q, c: [
            {"name": "조건활", "pdps": 300.0, "edps": 0.0, "aps": 1.4, "crit": 8.0,
             "price": 5, "cur": "divine", "rarity": "Rare", "mods": [], "cond": c["label"]}]
        globals()["resolve_search"] = lambda u: (
            "https://poe.kakaogames.com", "/api/trade2/search/poe2/L", "L", {})
        globals()["fetch_rates"] = lambda b, l, c: {"exalted": {"rate": 1.0, "n": None},
                                                    "divine": {"rate": 300.0, "n": 3}}
        globals()["load_banded"] = lambda u, n: (
            [{"name": "활A", "pdps": 100.0, "edps": 5.0, "aps": 1.4, "crit": 6.0,
              "price": 9, "cur": "div", "rarity": "Rare",
              "mods": ["38% increased Critical Hit Chance"]}], 137, 2,
            "https://poe.kakaogames.com", "Runes%20of%20Aldur")
        u = "https://www.pathofexile.com/trade2/search/poe2/Standard/abc"
        n_all = collect(u)
        assert last_url() == u
        with db() as con:
            snap = con.execute("SELECT taken_at,total,kept FROM snapshots").fetchall()
            rows = con.execute("SELECT name,pdps,mods FROM bows").fetchall()
        assert len(snap) == 1 and snap[0][1] == 137 and snap[0][2] == n_all
        assert rows[0][0] == "활A" and rows[0][1] == 100.0
        assert json.loads(rows[0][2]) == ["38% increased Critical Hit Chance"]
        with open(LATEST, encoding="utf-8") as fh:
            latest = json.load(fh)
        assert latest["bows"][0]["name"] == "활A" and latest["total"] == 137
        assert latest["rates"]["divine"]["rate"] == 300.0      # 환율도 같이 실린다
        assert list(latest["rates"]) == ["exalted", "divine"]  # 환율이 아이템보다 먼저 잡힌다
        # 조건 곡선이 스냅샷에 실리고, 그 활에 조건 표시가 붙는다
        n_cond = sum(len(x[2]) for x in PRIORITY_STATS)
        assert len(latest["conds"]) == n_cond, latest["conds"]
        assert n_all == 1 + n_cond                        # 기본 1개 + 조건마다 1점
        conded = [b for b in latest["bows"] if b.get("cond")]
        assert len(conded) == n_cond and conded[0]["cond"] == latest["conds"][0]["label"]
        # 같은 옵션이라도 문턱값이 다르면 별개 곡선이다
        assert latest["conds"][0]["min"] != latest["conds"][1]["min"]
        with db() as con:
            assert con.execute("SELECT COUNT(*) FROM bows WHERE cond IS NOT NULL").fetchone()[0] > 0
        with db() as con:
            assert json.loads(con.execute("SELECT rates FROM snapshots").fetchone()[0])["exalted"]["rate"] == 1.0
        assert collect(u) == n_all                  # 두 번째 스냅샷도 쌓인다
        with db() as con:
            assert con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2
    finally:
        globals()["load_banded"] = real
        globals()["fetch_rates"] = real_rates
        globals()["resolve_search"] = real_resolve
        globals()["stat_catalog"], globals()["sweep_condition"] = real_cat, real_sweep
        DB, LATEST = keep

    # 스로틀: 헤더는 '응답 시점'의 사용량이라, 그 뒤 흐른 시간을 안 빼면
    # 요청이 실패해 헤더가 안 오는 동안 같은 값으로 창 전체를 계속 다시 잔다.
    class _Clock(object):
        def __init__(self): self.now, self.log = 1000.0, []
        def time(self): return self.now
        def sleep(self, s): self.log.append(round(s, 1)); self.now += s
    global time
    real_time, clk = time, _Clock()
    try:
        time = clk
        rules, state = "5:15:60,10:90:300,30:300:1800", "4:15:0,3:90:0,12:300:0"
        RATE_STATE["exchange"] = {"rules": rules, "state": state, "at": clk.now}
        LAST_CALL.clear()
        for _ in range(4):
            throttle("exchange")
        # 첫 회만 창(15초)을 기다리고, 이후는 페이싱 간격(10초)만 — 15초를 네 번 자면 회귀다
        assert clk.log == [15.0, 10.0, 10.0, 10.0], clk.log

        # 창이 절반 지났으면 남은 만큼만 잔다
        clk.now, clk.log[:] = 1000.0, []
        RATE_STATE["exchange"] = {"rules": rules, "state": state, "at": clk.now - 10}
        LAST_CALL.clear(); throttle("exchange")
        assert clk.log == [5.0], clk.log

        # 하루 예산이 바닥나면 자지 말고 접는다 (창이 MAX_WAIT 보다 길다)
        clk.now, clk.log[:] = 1000.0, []
        RATE_STATE["exchange"] = {"rules": "1000:21600:1800", "state": "999:21600:0", "at": clk.now}
        LAST_CALL.clear()
        try:
            throttle("exchange"); assert False, "하루 예산 소진인데 접지 않았다"
        except TradeError:
            pass
        assert clk.log == [], clk.log
    finally:
        time = real_time
        RATE_STATE.clear(); LAST_CALL.clear()

    # 스냅샷 쓰기: 실패해도 지난 스냅샷이 살아 있어야 한다
    import tempfile, shutil
    tdir = tempfile.mkdtemp()
    try:
        snap = os.path.join(tdir, "latest.json")
        write_latest({"bows": [{"name": "지난 것"}]}, snap)
        keep = open(snap, encoding="utf-8").read()
        try:
            write_latest({"bows": [{"n": 1}] * 400 + [{"mods": set()}]}, snap)
            assert False, "직렬화가 실패했어야 한다"
        except TypeError:
            pass
        assert open(snap, encoding="utf-8").read() == keep, "실패한 쓰기가 지난 스냅샷을 덮었다"
        assert json.load(open(snap, encoding="utf-8"))["bows"][0]["name"] == "지난 것"
        write_latest({"bows": [{"name": "새 것"}]}, snap)      # 성공하면 갈아끼운다
        assert json.load(open(snap, encoding="utf-8"))["bows"][0]["name"] == "새 것"

        # 윈도우는 대상을 누가 읽는 중이면 replace 를 거절한다 — 잠깐 뒤 다시 걸어야 한다
        held = threading.Event()
        def _hold():
            fh = open(snap, "rb"); fh.read(1); held.set()
            time.sleep(0.35); fh.close()
        t = threading.Thread(target=_hold); t.start(); held.wait(2)
        write_latest({"bows": [{"name": "읽는 중에 쓴 것"}]}, snap)   # 여기서 터지면 회귀다
        t.join()
        assert json.load(open(snap, encoding="utf-8"))["bows"][0]["name"] == "읽는 중에 쓴 것"
    finally:
        shutil.rmtree(tdir, ignore_errors=True)

    # 차익 순환이 있으면 우회 계산이 0 까지 깎인다 — 그런 값은 내보내지 말 것
    bad, _ = solve_rates({("exalted", "divine"): [400], ("divine", "chaos"): [0.1],
                          ("chaos", "annul"): [0.1], ("annul", "divine"): [0.1],
                          ("chaos", "divine"): [0.1], ("annul", "chaos"): [0.1]})
    assert bad["divine"] == 400, bad
    assert "chaos" not in bad and "annul" not in bad, bad     # 지어낸 값 대신 아예 빠진다
    z, _ = solve_rates({("exalted", "divine"): [0], ("exalted", "chaos"): [65]})
    assert "divine" not in z and z["chaos"] == 65, z          # 0 도 통과 못 한다
    ok, how_ok = solve_rates({("exalted", "divine"): [400], ("divine", "annul"): [0.4]})
    assert ok["annul"] == 160 and how_ok["annul"].startswith("우회"), ok   # 정상 우회는 그대로

    # 무기 DPS 와 무관한 동료 옵션을 "이미 셈"으로 삼키면 조건 목록에서 통째로 사라진다
    assert not is_off_dps("[Physical|물리] 피해 118% 증가")
    assert not is_off_dps("[Fire|화염] 피해 10~16 추가")
    assert not is_off_dps("공격 속도 12% 증가")
    assert is_off_dps("[Companion|반려수]의 [Attack|공격] 속도 14% 증가"), "반려수 공속은 DPS 밖이다"
    assert is_off_dps("접근해 있는 반려수의 공격 속도 9% 증가")
    assert is_off_dps("피해의 6%를 추가 카오스 피해로 획득"), "'추가 ~로 획득'은 무기 DPS 가 아니다"
    assert is_off_dps("[Accuracy|정확도] +58")

    # 조건 선택: 페널티 옵션과 잡옵션이 조건으로 새어나가면 안 된다
    assert not is_useful_stat("explicit.x", "투사체 사거리 #% 감소")   # 페널티 + 페이지도 거르는 것
    assert not is_useful_stat("explicit.x", "능력치 요구사항 #% 감소")
    assert not is_useful_stat("explicit.x", "#% reduced Projectile Speed")
    assert not is_useful_stat("explicit.x", "민첩 #")
    assert not is_useful_stat("implicit.stat_1", "치명타 확률 #%")     # 베이스 고유는 구분이 안 됨
    assert is_useful_stat("explicit.x", "치명타 확률 #%")
    assert is_useful_stat("explicit.x", "반려수의 공격 속도 #% 증가")
    assert is_useful_stat("explicit.x", "민첩 10당 공격 속도 #% 증가")  # 부분 일치로 빼면 안 된다

    # 두 진입점이 같은 링크 검사를 쓰는지 — 검색 링크가 아니면 요청이 나가기 전에 막혀야 한다
    for bad in ("https://poe.kakaogames.com/", "https://poe.kakaogames.com/account/view-profile/x",
                "https://poe.kakaogames.com/api/trade2/data/stats",
                "https://evil.com/trade2/search/poe2/L/x",
                "https://poe.kakaogames.com.evil.com/trade2/search/poe2/L/x",
                "ftp://poe.kakaogames.com/trade2/search/poe2/L/x", ""):
        try:
            search_api_url(bad); assert False, "막았어야 한다: %s" % bad
        except TradeError:
            pass
    ok = search_api_url("https://evil.com@poe.kakaogames.com/trade2/search/poe2/L/x")
    assert ok == ("https://poe.kakaogames.com", "/api/trade2/search/poe2/L/x"), ok  # userinfo 위장은 무시
    assert search_api_url("https://www.pathofexile.com/trade2/search/poe2/Standard/xy")[0]         == "https://www.pathofexile.com"

    # 조건 검색이 사용자의 저장된 옵션 조건을 지우면 안 된다 (기준 곡선과 다른 집단이 된다)
    saved_q = {"filters": {"type_filters": {"filters": {"rarity": {"option": "rare"}}}},
               "stats": [{"type": "and", "filters": [{"id": "explicit.내것", "value": {"min": 100}}]}]}
    sent = []
    keep_api, keep_fetch, keep_thr = api_get, fetch_ids, throttle
    globals()["api_get"] = lambda u, payload=None, _retried=False: (
        sent.append(payload) or {"id": "q", "total": 0, "result": []})
    globals()["fetch_ids"] = lambda *a: []
    globals()["throttle"] = lambda b: None
    try:
        sweep_condition("https://h", "/p", saved_q, {"id": "explicit.우리것", "min": 2, "label": "x"})
    finally:
        globals()["api_get"], globals()["fetch_ids"], globals()["throttle"] = keep_api, keep_fetch, keep_thr
    got = json.dumps(sent[0]["query"], ensure_ascii=False)
    assert "explicit.내것" in got, "사용자 조건이 지워졌다: " + got
    assert "explicit.우리것" in got, "우리 조건이 안 들어갔다: " + got
    assert "rare" in got, "다른 필터도 유지되어야 한다: " + got
    assert saved_q["stats"] == [{"type": "and",
                                 "filters": [{"id": "explicit.내것", "value": {"min": 100}}]}],         "원본 질의문을 건드리면 다음 문턱값에서 조건이 쌓인다"

    # 화면 경로도 스로틀을 거쳐야 한다 (예전엔 한 번 클릭 12요청이 전부 그냥 나갔다)
    seen_buckets, keep_api, keep_thr = [], api_get, throttle
    globals()["throttle"] = lambda b, mw=None: seen_buckets.append(b)
    globals()["api_get"] = lambda u, payload=None, _retried=False: (
        {"result": [None]} if "/fetch/" in u
        else {"id": "Q", "total": 5, "result": ["i%d" % k for k in range(25)]} if payload
        else {"query": {}})
    try:
        load_trade("https://poe.kakaogames.com/trade2/search/poe2/L/abc", 25)
    finally:
        globals()["api_get"], globals()["throttle"] = keep_api, keep_thr
    assert seen_buckets.count("search") == 2, seen_buckets     # 검색 GET + 리그 POST
    assert seen_buckets.count("fetch") == 3, seen_buckets      # 25개 -> 10개씩 3배치

    # 스로틀은 스레드끼리 겹쳐도 간격을 지켜야 한다 (ThreadingHTTPServer 라 동시 요청이 온다)
    RATE_STATE["search"] = {"rules": "3:3:60", "state": "1:3:0", "at": time.time()}
    LAST_CALL["search"] = time.time()
    at = []
    def _hit():
        throttle("search"); at.append(time.time())
    th = [threading.Thread(target=_hit) for _ in range(3)]
    t0 = time.time()
    [t.start() for t in th]; [t.join() for t in th]
    at.sort()
    assert at[-1] - at[0] > 1.5, "동시 요청이 간격 없이 한꺼번에 나갔다: %r" % [round(x - t0, 2) for x in at]
    RATE_STATE.clear(); LAST_CALL.clear()

    # --push: 임시 저장소에서 커밋 성립 / 같은 내용이면 생략 (원격이 없으니 push 실패는 정상)
    import subprocess, tempfile, shutil
    g = tempfile.mkdtemp()
    try:
        for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                    ["git", "config", "user.name", "t"]):
            subprocess.run(cmd, cwd=g, capture_output=True)
        open(os.path.join(g, "latest.json"), "w").write("{}")
        assert push_latest(g) == "push-fail"          # 커밋은 됐고 원격이 없어 푸시만 실패
        assert push_latest(g) == "same"               # 같은 내용 -> 커밋 없이 생략
    finally:
        shutil.rmtree(g, ignore_errors=True)

    # collect() 의 securable 경고: 대역 resolve_search 가 securable 아님 -> 경고가 찍혀야 한다
    # (위 수집기 자체 검증의 대역이 이미 status 없는 질의문을 쓰므로, 경고 경로는 그 실행에서 돈다)

    # 빈도 조건의 라벨은 빈칸(#)이 아니라 문턱값을 담아야 한다
    _fb = [{"mods": ["[Companion|반려수]의 [Attack|공격] 속도 %d%% 증가" % v]}
           for v in (12, 13, 14, 15, 16, 17)]
    _got = [c for c in pick_conditions(_fb, {"반려수의 공격 속도 #% 증가": "explicit.stat_X"})
            if c["why"] == "빈도"]
    assert _got and "#" not in _got[0]["label"] and "이상" in _got[0]["label"], _got

    print("serve.py self-test PASS")


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv[:-1] else default


if __name__ == "__main__":
    if "--test" in sys.argv:
        demo()
        sys.exit(0)

    if "--collect" in sys.argv:
        url = arg("--collect")
        if url and url.startswith("--"):
            url = None                              # "--collect --every 3600" 형태
        url = url or last_url()
        if not url:
            sys.exit('거래소 검색 URL 이 필요합니다:\n'
                     '  python serve.py --collect "https://.../trade2/search/poe2/..."')
        limit = int(arg("--limit", "10"))     # 문턱값 하나당 몇 개까지 뜰지
        every = arg("--every")
        if not os.environ.get("POESESSID"):
            print("참고: POESESSID 미설정 — 거래소가 로그인을 요구하면 파일 맨 위 설명을 보세요.")
        if every:
            collect_loop(url, max(600, int(every)), limit)   # 10분보다 자주는 안 뜬다
        else:
            collect(url, limit)
        sys.exit(0)
    url = "http://localhost:%d/" % PORT
    print("활 시세 감정소 → %s   (Ctrl+C 로 종료)" % url)
    if not os.environ.get("POESESSID"):
        print("참고: POESESSID 미설정 — 거래소가 로그인을 요구하면 위 파일 맨 위 설명을 보세요.")
    webbrowser.open(url)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
