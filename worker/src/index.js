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
// 수집 대상 무기(캐스터 제외·POE2 에 실제 있는 6종, serve.py ATTACK_WEAPONS 와 같은 목록).
// row JSON 안에 담으므로 D1 스키마 변경이 없다. 값이 없는 옛 행은 수집기가 활로 본다.
const CATEGORIES = new Set([
  "weapon.bow",
  "weapon.crossbow",
  "weapon.onemace",
  "weapon.twomace",
  "weapon.spear",
  "weapon.warstaff",
]);
const RARITIES = new Set(["Normal", "Magic", "Rare", "Unique", ""]);

let schemaReady = false;
async function ensureSchema(db) {
  if (schemaReady) return;
  await db.exec(
    "CREATE TABLE IF NOT EXISTS harvest(lid TEXT PRIMARY KEY, t INTEGER NOT NULL, fee INTEGER, row TEXT NOT NULL)",
  );
  schemaReady = true;
}

// serve.py normalize 스키마와 같은 행만 통과 — 이상한 값은 조용히 버린다.
// 상한은 조작 방어의 핵심: isFinite 만 보면 pdps:1e300 이 통과해 곡선의 DPS 축을
// 통째로 날려버린다(실측 재현됨). 현실 활 최대치보다 넉넉하되 유한하게 잡는다.
const MAX = { dps: 100000, aps: 100, crit: 100, price: 1e9, fee: 1e12 };
function validRow(r) {
  if (!r || typeof r !== "object") return null;
  const num = (v) => typeof v === "number" && isFinite(v);
  if (typeof r.id !== "string" || !r.id || r.id.length > 64) return null;
  if (!num(r.price) || r.price <= 0 || r.price > MAX.price) return null;
  if (!CURRENCIES.has(r.cur)) return null;
  if (!num(r.pdps) || !num(r.edps) || r.pdps < 0 || r.edps < 0) return null;
  if (r.pdps > MAX.dps || r.edps > MAX.dps) return null;
  if (r.pdps + r.edps <= 0) return null;
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
    price: r.price,
    cur: r.cur,
    rarity: r.rarity ?? "",
    mods: r.mods,
    fee: num(r.fee) && r.fee > 0 ? r.fee : null,
    league: r.league,
    cat: r.cat ?? "weapon.bow",
  };
}

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
      if (env.RL) {
        const { success } = await env.RL.limit({ key: req.headers.get("cf-connecting-ip") || "" });
        if (!success) return json({ error: "rate limited" }, 429);
      }
      const rows = (Array.isArray(body?.rows) ? body.rows : [])
        .slice(0, 60)
        .map(validRow)
        .filter(Boolean);
      const now = Date.now();
      // 쓰기 절약: 이미 저장된 매물 id 는 쓰기 전에 걸러낸다(정직한 재전송만 접힌다 — 소진 공격은 위 속도 제한이 맡는다).
      // fee 없는 행은 lid 를 따로 둔다 — 같은 id 로 fee 없는 행이 먼저 오면 INSERT OR IGNORE 가
      // 48시간 동안 정당한 fee 행을 막았다(공개된 실제 매물 id 로 선점 가능).
      const lidOf = (r) => (r.fee == null ? "nofee:" + r.id : r.id);
      // (읽기는 하루 500만 행 무료라 사실상 공짜, 쓰기는 10만 행 한도가 병목)
      let fresh = rows;
      if (rows.length) {
        const marks = rows.map((_, i) => "?" + (i + 1)).join(",");
        const { results } = await env.DB.prepare(
          "SELECT lid FROM harvest WHERE lid IN (" + marks + ")",
        )
          .bind(...rows.map(lidOf))
          .all();
        const known = new Set(results.map((r) => r.lid));
        fresh = rows.filter((r) => !known.has(lidOf(r)));
      }
      const stmt = env.DB.prepare(
        "INSERT OR IGNORE INTO harvest(lid, t, fee, row) VALUES (?1, ?2, ?3, ?4)",
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
      const { results } = await env.DB.prepare(
        "SELECT t, fee, row FROM harvest WHERE t >= ?1 AND fee IS NOT NULL ORDER BY t DESC LIMIT 3000",   // fee 없는 행은 수집기가 버리므로 창(3000)을 낭비하지 않는다
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
