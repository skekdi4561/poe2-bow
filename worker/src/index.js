// 활 시세 감정소 — 크라우드 수집 수합 서버
// POST /harvest : 오버레이 앱이 보낸 활 매물 행(익명, 공개 정보만)을 저장
// GET  /recent  : 최근 24시간 행을 감정소 수집기(serve.py)가 끌어감
// 저장은 매물 id 로 중복 제거, 48시간 지나면 지운다.

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
};
// 미러는 시장 최상위 매물이 몰려 있어 받는다(serve.py PRICE_CURRENCIES 와 같은 목록).
// 빼면 최상위 무기가 크라우드에서 통째로 사라진다.
const CURRENCIES = new Set(["exalted", "chaos", "divine", "annul", "mirror"]);
// 수집 대상(캐스터 제외 공격 무기 7종 + 방패, serve.py ATTACK_WEAPONS 와 **같은 목록**).
// row JSON 안에 담으므로 D1 스키마 변경이 없다. 값이 없는 옛 행은 수집기가 활로 본다.
const CATEGORIES = new Set([
  "weapon.bow",
  "weapon.crossbow",
  "weapon.onemace",
  "weapon.twomace",
  "weapon.spear",
  "weapon.warstaff",
  "weapon.talisman",
  "armour.shield",
]);
const RARITIES = new Set(["Normal", "Magic", "Rare", "Unique", ""]);

let schemaReady = false;
async function ensureSchema(db) {
  if (schemaReady) return;
  await db.exec(
    "CREATE TABLE IF NOT EXISTS harvest(lid TEXT PRIMARY KEY, t INTEGER NOT NULL, fee INTEGER, row TEXT NOT NULL)",
  );
  // /recent 두 쿼리(ORDER BY t DESC)와 청소 DELETE(WHERE t <) 가 인덱스 없이는 전부
  // 풀스캔 + 임시 B-tree 정렬이다(EXPLAIN QUERY PLAN 실측). D1 은 rows_read 로 과금·한도를
  // 매기므로 비용이 "반환한 800행"이 아니라 **테이블 전체 크기**에 비례했다.
  await db.exec("CREATE INDEX IF NOT EXISTS harvest_t ON harvest(t)");
  schemaReady = true;
}

// serve.py normalize 스키마와 같은 행만 통과 — 이상한 값은 조용히 버린다.
// 상한은 조작 방어의 핵심: isFinite 만 보면 pdps:1e300 이 통과해 곡선의 DPS 축을
// 통째로 날려버린다(실측 재현됨). 현실 활 최대치보다 넉넉하되 유한하게 잡는다.
const MAX = { dps: 100000, aps: 100, crit: 100, price: 1e9, fee: 1e12, block: 100 };
// 하한이 없으면 price 5e-324 / pdps 1e-9 같은 행이 통과한다. 유한하기만 하면 되는 게 아니라
// **거래에 실재할 수 있는 값**이어야 한다 — 그런 행은 언제나 최전선을 갈아치우는 것처럼 보여
// 수집기의 진위 확인 예산과 거래소 검색 호출을 매 사이클 태운다.
const MIN = { price: 0.01, metric: 1 };
export function validRow(r) {
  if (!r || typeof r !== "object") return null;
  const num = (v) => typeof v === "number" && isFinite(v);
  if (typeof r.id !== "string" || !r.id || r.id.length > 64) return null;
  if (!num(r.price) || r.price < MIN.price || r.price > MAX.price) return null;
  if (!CURRENCIES.has(r.cur)) return null;
  if (!num(r.pdps) || !num(r.edps) || r.pdps < 0 || r.edps < 0) return null;
  if (r.pdps > MAX.dps || r.edps > MAX.dps) return null;
  if (r.pdps + r.edps < MIN.metric) return null;
  // 방패 막기(%). 방어구에만 있고 언제나 양수라 0 은 "미수집" 이다 — serve.py normalize 와 같은 규약.
  if (r.block != null && (!num(r.block) || r.block <= 0 || r.block > MAX.block)) return null;
  if (!num(r.aps) || !num(r.crit)) return null;
  if (r.aps < 0 || r.aps > MAX.aps || r.crit < 0 || r.crit > MAX.crit) return null;
  if (r.fee != null && (!num(r.fee) || r.fee < 0 || r.fee > MAX.fee)) return null;
  if (!RARITIES.has(r.rarity ?? "")) return null;
  if (typeof r.name !== "string" || r.name.length > 120) return null;
  if (!Array.isArray(r.mods) || r.mods.length > 40) return null;
  if (!r.mods.every((m) => typeof m === "string" && m.length <= 200)) return null;
  // 리그는 여기서 고르지 않는다 — 값만 검증해 그대로 싣고, **수집기가 자기 리그와**
  // **대조해 거른다**(serve.py merge_harvest). 예전엔 "Standard" 만 받았는데 수집기는
  // 도전 리그를 뜨고 있어서, 통과한 스탠다드 매물이 도전 리그 곡선에 섞여 들어갔다.
  if (typeof r.league !== "string" || !r.league || r.league.length > 64) return null;
  // 무기 종류: 없으면 활(게이트가 활 전용이던 시절의 옛 행), 있으면 목록 안이어야 한다.
  if (r.cat != null && !CATEGORIES.has(r.cat)) return null;
  return {
    id: r.id,
    name: r.name,
    pdps: r.pdps,
    edps: r.edps,
    aps: r.aps,
    crit: r.crit,
    // 방패만 갖는다. 없으면 키 자체를 빼서 "미수집"과 "0" 을 구분한다(serve.py 와 같은 규약).
    ...(r.block == null ? {} : { block: r.block }),
    price: r.price,
    cur: r.cur,
    rarity: r.rarity ?? "",
    mods: r.mods,
    fee: num(r.fee) && r.fee > 0 ? r.fee : null,
    league: r.league,
    cat: r.cat ?? "weapon.bow",
  };
}

// 저장 키. fee 없는 행은 lid 를 따로 둔다 — 같은 id 로 fee 없는 행이 먼저 오면 INSERT OR IGNORE 가
// 48시간 동안 정당한 fee 행을 막았다(공개된 실제 매물 id 로 선점 가능). (test.mjs 가 import 하므로 export)
export const lidOf = (r) => (r.fee == null ? "nofee:" + r.id : r.id);

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(req.url);
    await ensureSchema(env.DB);

    if (req.method === "POST" && url.pathname === "/harvest") {
      let body;
      try {
        const text = await req.text();
        if (text.length > 300_000) return json({ error: "too big" }, 413);
        body = JSON.parse(text);
      } catch {
        return json({ error: "bad json" }, 400);
      }
      // 익명 POST 라 플러딩(쓰기 한도 소진·/recent 창 밀어내기)은 코드로 못 막는다 — IP 별 속도 제한
      const rows = (Array.isArray(body?.rows) ? body.rows : [])
        .slice(0, 60)
        .map(validRow)
        .filter(Boolean);
      // 리밋을 **요청**이 아니라 **행 수**에 건다. 요청 단위면 60요청×60행 = 3600행/분인데
      // /recent 창은 카테고리당 800행이라, 리밋이 정상 작동해도 한 IP가 ~14초면 창을 100%
      // 자기 행으로 채운다(재현: 정직한 900행 + 공격 3600행 → 반환 800행 중 정직 0개).
      // 10행당 1회로 세면 같은 리밋에서 600행/분이 되어 창을 한 번에 못 덮는다.
      if (env.RL) {
        const key = req.headers.get("cf-connecting-ip") || "";
        const cost = Math.max(1, Math.ceil(rows.length / 10));
        for (let i = 0; i < cost; i++) {
          const { success } = await env.RL.limit({ key });
          if (!success) return json({ error: "rate limited" }, 429);
        }
      }
      const now = Date.now();
      // 쓰기 절약: 값이 **그대로인** 재전송만 걸러낸다. 예전엔 lid 가 이미 있으면 무조건
      // 건너뛰었는데, 그건 **선점**이 된다 — 공격자가 실재 매물 id 로 price 999 를 먼저 올리면
      // 진짜 관측(price 50)도, 그 뒤의 가격 인하도 48시간 내내 조용히 거부됐다(재현됨).
      // 이제 저장된 가격을 같이 읽어 값이 달라진 행만 통과시키고, 아래 UPSERT 가 덮어쓴다.
      // (읽기는 하루 500만 행 무료라 사실상 공짜, 쓰기는 10만 행 한도가 병목)
      let fresh = rows;
      if (rows.length) {
        const marks = rows.map((_, i) => "?" + (i + 1)).join(",");
        const { results } = await env.DB.prepare(
          "SELECT lid, row FROM harvest WHERE lid IN (" + marks + ")",
        )
          .bind(...rows.map(lidOf))
          .all();
        const known = new Map();
        for (const x of results) {
          let price = null;
          try {
            price = JSON.parse(x.row).price;
          } catch {
            price = null; // 못 읽으면 "다르다"로 보고 갱신시킨다
          }
          known.set(x.lid, price);
        }
        fresh = rows.filter((r) => {
          const lid = lidOf(r);
          return !known.has(lid) || known.get(lid) !== r.price;
        });
      }
      // 같은 매물의 재관측은 갱신한다. 값이 같으면 위에서 이미 걸러졌으므로 쓰기 절약은 그대로다.
      const stmt = env.DB.prepare(
        "INSERT INTO harvest(lid, t, fee, row) VALUES (?1, ?2, ?3, ?4)" +
          " ON CONFLICT(lid) DO UPDATE SET t = excluded.t, fee = excluded.fee, row = excluded.row",
      );
      if (fresh.length) {
        await env.DB.batch(
          fresh.map((r) => stmt.bind(lidOf(r), now, r.fee, JSON.stringify(r))),
        );
      }
      // ponytail: 요청 2% 확률로 이틀 지난 행 청소 — 전용 cron 은 필요해지면
      if (Math.random() < 0.02) {
        await env.DB.prepare("DELETE FROM harvest WHERE t < ?1")
          .bind(now - 48 * 3600 * 1000)
          .run();
      }
      return json({ ok: true, accepted: rows.length, written: fresh.length});
    }

    if (req.method === "GET" && url.pathname === "/recent") {
      const cut = Date.now() - 24 * 3600 * 1000;
      // 무기(cat)를 주면 그 무기 행만 — 창을 무기별로 나눠 준다.
      // 안 나누면 창 하나(3000행)를 7종이 공유해서, 한 무기로 밀어넣는 플러딩이 다른 무기의
      // 정직한 표본까지 통째로 밀어낸다. 수집기도 사이클마다 같은 3000행을 7번 받아 거의 다 버렸다.
      // cat 없는 옛 행은 수집기와 같은 기본값(weapon.bow)으로 본다.
      const cat = url.searchParams.get("cat");
      if (cat && !CATEGORIES.has(cat)) return json({ error: "bad cat" }, 400);
      const { results } = cat
        ? await env.DB.prepare(
            "SELECT t, fee, row FROM harvest WHERE t >= ?1 AND fee IS NOT NULL" +
              " AND COALESCE(json_extract(row, '$.cat'), 'weapon.bow') = ?2" +
              " ORDER BY t DESC LIMIT 800",
          )
            .bind(cut, cat)
            .all()
        : await env.DB.prepare(
            "SELECT t, fee, row FROM harvest WHERE t >= ?1 AND fee IS NOT NULL ORDER BY t DESC LIMIT 3000",   // fee 없는 행은 수집기가 버리므로 창을 낭비하지 않는다
          )
            .bind(cut)
            .all();
      const rows = results.map((r) => ({ ...JSON.parse(r.row), t: r.t }));
      return json({ taken_at: Date.now(), rows });
    }

    return json({ error: "not found" }, 404);
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", ...CORS },
  });
}
