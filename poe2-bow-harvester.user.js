// @updateURL / @downloadURL 이 없으면 한 번 깐 사람은 그 버전에 영원히 고정된다 —
// 행 스키마가 바뀌면 옛 스크립트가 조용히 안 맞는 데이터를 보낸다. 이 두 줄이면 텀퍼몽키가
// 알아서 갱신한다. @name 은 일부러 안 바꾼다 — 바꾸면 이미 깐 사람에게 별개 스크립트로 잡힌다.
// ==UserScript==
// @name         POE2 활 시세 채집기
// @namespace    poe2-bow-appraiser
// @version      0.2.1
// @description  거래소에서 이미 보고 있는 무기 매물을 주워 'PoE2 시세 감정소'로 흘려보낸다. 추가 요청 0.
// @updateURL    https://skekdi4561.github.io/poe2-bow/poe2-bow-harvester.user.js
// @downloadURL  https://skekdi4561.github.io/poe2-bow/poe2-bow-harvester.user.js
// @match        https://poe.kakaogames.com/trade2/*
// @match        http://localhost:8731/*
// @match        http://127.0.0.1:8731/*
// @match        https://skekdi4561.github.io/*
// @run-at       document-start
// @grant        GM_getValue
// @grant        GM_setValue
// @noframes
// ==/UserScript==

/* 설치: Tampermonkey(또는 Violentmonkey)를 깐 뒤 이 파일을 브라우저로 열면 설치 화면이 뜬다.
   감정소 실행 중이면 http://localhost:8731/poe2-bow-harvester.user.js 로도 열린다.

   동작 원리: 거래소 페이지가 "자기 API 에서 이미 받아온" 응답을 옆으로 복사할 뿐,
   거래소로 요청을 새로 만들지 않는다 — 추가 부담 0, 레이트 리밋 무관.
   쿠키·세션·계정 정보에는 손대지 않는다. 읽는 것은 화면에 뜬 매물 JSON 뿐이다.

   감정소를 다른 포트로 띄웠다면 위 @match 의 8731 을 바꿔줄 것.
   공개판(https://skekdi4561.github.io/poe2-bow/)과 로컬 실행기 양쪽에 다리가 이어져 있다.

   자체 검증: node poe2-bow-harvester.user.js --test */

(function () {
  'use strict';

  var STORE = 'harvest1';   // GM 저장소 열쇠 — 거래소 탭이 쓰고, 감정소 탭이 읽는 다리
  var MAX_ROWS = 800;       // 저장소 예산. 넘치면 오래 본 것부터 버린다
  var MAX_IDS = 3000;
  var CURRENCIES = { exalted: 1, chaos: 1, divine: 1, annul: 1, mirror: 1 };  // serve.py PRICE_CURRENCIES 와 같은 기준
  var FRAME = { 0: 'Normal', 1: 'Magic', 2: 'Rare', 3: 'Unique', 12: 'Magic', 13: 'Rare', 14: 'Unique' };  // serve.py FRAME_RARITY 와 같은 표(12~14 = 룬 박힌 변형)
  var RARITIES = { Normal: 1, Magic: 1, Rare: 1, Unique: 1 };
  var curLeague = '';       // 마지막 검색 URL 의 리그 — 행에 실어 보내 감정소가 스냅샷 리그와 대조한다
  var leagueOf = function (url) {
    var m = /\/api\/trade2\/search\/poe2\/([^/?#]+)/.exec(String(url || ''));
    if (m) { try { curLeague = decodeURIComponent(m[1]); } catch (e) { curLeague = m[1]; } }
    return curLeague;
  };

  // ---- 순수 함수 (node 로 검증됨) ----

  var clean = function (s) {           // "[Bow|활]" -> "활"
    return String(s == null ? '' : s)
      .replace(/\[([^\]|]*)\|([^\]]*)\]/g, '$2')
      .replace(/\[([^\]]*)\]/g, '$1');
  };
  var num = function (s) {
    var m = String(s == null ? '' : s).match(/[\d.]+/);
    return m ? parseFloat(m[0]) : 0;
  };
  var prop = function (item, res) {
    var lists = (item.properties || []).concat(item.additionalProperties || []);
    for (var i = 0; i < lists.length; i++) {
      if (lists[i] && res.test(clean(lists[i].name))) {
        var v = lists[i].values;
        if (v && v[0] && v[0][0] != null) return String(v[0][0]);
      }
    }
    return null;
  };

  // 활인지 확인. 석궁(게임 데이터는 [Crossbow|쇠뇌])에도 pdps 가 있어서 "Bow" 부분 일치로는 안 된다 — 정확 일치만.
  var isBowClass = function (item) {
    var lists = (item && item.properties) || [];
    for (var i = 0; i < lists.length; i++) {
      var name = clean(lists[i] && lists[i].name).trim();
      if (name === '활' || name === 'Bow') return true;
    }
    return false;
  };

  // 거래소 fetch 응답의 항목 하나 -> 감정소 매물 한 줄. 확신이 없으면 버린다(null).
  // serve.py 의 normalize() 와 같은 규칙: 실거래 화폐 넷 밖 버림, DPS 없으면 버림.
  var normalizeRow = function (res, bowIds) {
    if (!res || typeof res !== 'object') return null;
    var item = res.item || {}, price = (res.listing || {}).price || {};
    if (!price.currency || !price.amount) return null;
    if (!(price.currency in CURRENCIES)) return null;   // 환율 모르는 화폐는 곡선만 틀어놓는다
    var ext = item.extended || {};
    if (ext.pdps == null && ext.edps == null) return null;
    // 활이라는 확인: ①속성에 클래스가 있거나 ②활 카테고리 검색 결과의 id 였거나. 둘 다 아니면 안 줍는다.
    if (!isBowClass(item) && !(bowIds && res.id && bowIds[res.id])) return null;
    var rarity = (item.rarity in RARITIES) ? item.rarity : FRAME[item.frameType];   // 4종 밖 문자열은 frameType 으로
    if (!rarity) return null;                            // 등급 불명이 곡선에 섞이면 못 가려낸다
    // 길이 상한: 페이지 세계는 신뢰할 수 없다(다른 스크립트가 가짜 이벤트를 쏠 수 있다).
    // 거대한 문자열로 GM 저장소·감정소 localStorage 를 부풀리는 것을 막는다.
    var mods = [];
    // serve.py MOD_KEYS / harvest.ts modLines 와 같은 7개 키·같은 순서로 뽑아야 지문(fp)이
    // 일치한다. 예전엔 implicit+explicit 2개만 떠서, 룬/제작/균열 모드가 붙은 활(룬 소켓은
    // 흔하다)의 fp 가 수집기·오버레이 크라우드와 달라져 relist 중복제거를 빠져나갔다.
    ['implicitMods', 'explicitMods', 'runeMods', 'craftedMods',
     'fracturedMods', 'enchantMods', 'desecratedMods'].forEach(function (key) {
      (item[key] || []).forEach(function (m) {
        var t = typeof m === 'string' ? m : (m && typeof m.description === 'string' ? m.description : null);
        if (t !== null && mods.length < 40) mods.push(t.slice(0, 200));
      });
    });
    return {
      id: String(res.id || ''),
      league: curLeague,
      name: ([item.name, item.typeLine || item.baseType].filter(Boolean).join(' ').trim() || '이름 없음').slice(0, 120),
      pdps: Math.round((ext.pdps || 0) * 10) / 10,
      edps: Math.round((ext.edps || 0) * 10) / 10,
      aps: num(prop(item, /Attacks per Second|초당 공격/)),
      crit: num(prop(item, /Critical.*Chance|치명타/)),
      price: price.amount, cur: price.currency,
      rarity: rarity, mods: mods, t: Date.now(),
    };
  };

  // ---- 거래소 페이지 쪽: 사이트 자신의 API 응답을 옆으로 복사 ----

  // 페이지 세계에 심는 훅. 요청을 만들지 않고, 지나가는 응답을 복제해서 이벤트로만 넘긴다.
  var HOOK = '(' + String(function () {
    if (window.__poe2bowHooked) return;
    window.__poe2bowHooked = true;
    document.documentElement.setAttribute('data-poe2bow-hook', '1');
    var emit = function (obj) {
      try { window.dispatchEvent(new CustomEvent('__poe2bow', { detail: JSON.stringify(obj) })); } catch (e) {}
    };
    // ⚠️ 이 함수는 문자열로 페이지 세계에 주입된다 — 바깥(샌드박스) 변수를 참조하면 ReferenceError 로
    // 거래소의 모든 fetch/XHR 이 죽는다(0.2.0 실사고). 리그는 여기서 잡아 emit 에 실어 넘긴다.
    var curLeague = '';
    var leagueOf = function (url) {
      var m = /\/api\/trade2\/search\/poe2\/([^/?#]+)/.exec(String(url || ''));
      if (m) { try { curLeague = decodeURIComponent(m[1]); } catch (e) { curLeague = m[1]; } }
      return curLeague;
    };
    var catOf = function (bodyText) {
      try { return JSON.parse(bodyText).query.filters.type_filters.filters.category.option || ''; }
      catch (e) { return ''; }
    };
    var onSearch = function (bodyText, json) {   // 활 카테고리 검색의 결과 id 만 기억해 둔다
      if (catOf(bodyText) === 'weapon.bow' && json && json.result) emit({ kind: 'search', ids: json.result });
    };
    var onFetch = function (json) {
      if (json && json.result) emit({ kind: 'fetch', data: json, league: curLeague });
    };
    var F = window.fetch;
    window.fetch = function (input, init) {
      var url = typeof input === 'string' ? input : ((input && input.url) || '');
      var body = init && typeof init.body === 'string' ? init.body : '';
      leagueOf(url);
      var p = F.apply(this, arguments);
      if (/\/api\/trade2\/fetch\//.test(url)) {
        p.then(function (r) { r.clone().json().then(onFetch)['catch'](function () {}); })['catch'](function () {});
      } else if (/\/api\/trade2\/search\//.test(url) && init && String(init.method).toUpperCase() === 'POST') {
        p.then(function (r) { r.clone().json().then(function (j) { onSearch(body, j); })['catch'](function () {}); })['catch'](function () {});
      }
      return p;
    };
    var XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) { this.__poe2bowU = u; this.__poe2bowM = m; return XO.apply(this, arguments); };
    XMLHttpRequest.prototype.send = function (body) {
      var u = String(this.__poe2bowU || ''), self = this;
      leagueOf(u);
      if (/\/api\/trade2\/(fetch|search)\//.test(u)) {
        this.addEventListener('load', function () {
          try {
            var j = JSON.parse(self.responseText);
            if (/\/fetch\//.test(u)) onFetch(j);
            else if (String(self.__poe2bowM).toUpperCase() === 'POST') onSearch(typeof body === 'string' ? body : '', j);
          } catch (e) {}
        });
      }
      return XS.apply(this, arguments);
    };
  }) + ')()';

  var addRows = function (rows) {
    if (!rows.length) return;
    var cur = GM_getValue(STORE, null);
    if (!cur || typeof cur !== 'object' || !cur.rows) cur = { v: 1, rows: {} };
    rows.forEach(function (r) { cur.rows[r.id] = r; });
    var keys = Object.keys(cur.rows);
    if (keys.length > MAX_ROWS) {
      keys.sort(function (a, b) { return cur.rows[a].t - cur.rows[b].t; });
      keys.slice(0, keys.length - MAX_ROWS).forEach(function (k) { delete cur.rows[k]; });
    }
    GM_setValue(STORE, cur);
  };

  var runTrade = function () {
    var bowIds = {};                    // 활 카테고리 검색이 돌려준 id (탭 수명 동안만)
    window.addEventListener('__poe2bow', function (e) {
      var msg; try { msg = JSON.parse(e.detail); } catch (err) { return; }
      if (msg.kind === 'search') {
        (msg.ids || []).forEach(function (id) { bowIds[id] = 1; });
        var ks = Object.keys(bowIds);
        if (ks.length > MAX_IDS) ks.slice(0, ks.length - MAX_IDS).forEach(function (k) { delete bowIds[k]; });
      } else if (msg.kind === 'fetch') {
        var out = [];
        if (typeof msg.league === 'string') curLeague = msg.league;   // 페이지 세계가 잡은 리그
        ((msg.data && msg.data.result) || []).forEach(function (res) {
          var r = normalizeRow(res, bowIds);
          if (r) out.push(r);
        });
        addRows(out);
      }
    });
    try {
      var s = document.createElement('script');
      s.textContent = HOOK;
      (document.head || document.documentElement).appendChild(s);
      s.remove();
    } catch (e) {}
    setTimeout(function () {
      if (document.documentElement.getAttribute('data-poe2bow-hook') !== '1')
        console.warn('[활 채집기] 훅 주입이 막혔습니다(사이트 CSP 추정). 채집이 동작하지 않습니다 — 제보해 주세요.');
    }, 1000);
    // ponytail: CSP 차단 시 unsafeWindow 대체 훅은 미구현 — 실제로 막히는 게 관측되면 추가.
  };

  // ---- 감정소 페이지 쪽: GM 저장소 -> 페이지 localStorage 로 다리 놓기 ----

  var runBridge = function () {
    var last = '';
    var push = function () {
      var cur = GM_getValue(STORE, null);
      if (!cur || !cur.rows) return;
      var rows = Object.keys(cur.rows).map(function (k) { return cur.rows[k]; });
      var body = JSON.stringify({ v: 1, at: Date.now(), rows: rows });
      var sig = JSON.stringify(rows);                           // 내용 기준 — 같은 자릿수 가격 변경도 잡는다
      if (sig === last) return;                                 // 변화 없으면 조용히
      last = sig;
      try {
        localStorage.setItem('poe2harvest', body);
        window.dispatchEvent(new CustomEvent('poe2harvest-updated'));
      } catch (e) {}
    };
    push();
    setInterval(push, 5000);
  };

  if (typeof GM_getValue === 'function') {
    if (/kakaogames\.com$/.test(location.host)) runTrade();   // 카카오(한국) 서버만 — 국제 서버 매물은 다른 시장
    else runBridge();
  }

  // ---- node 검증/내보내기 (Tampermonkey 에서는 module 이 없어 건너뛴다) ----
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { clean: clean, num: num, prop: prop, isBowClass: isBowClass,
                       normalizeRow: normalizeRow, HOOK: HOOK, leagueOf: leagueOf };
  }
  if (typeof process !== 'undefined' && process.argv && process.argv.indexOf('--test') >= 0) {
    var assert = function (ok, msg) { if (!ok) { console.error('FAIL: ' + msg); process.exit(1); } };
    var kr = { id: 'abc', listing: { price: { amount: 5, currency: 'divine' } },
      item: { name: '파멸의 노래', typeLine: '고급 광신자 활', rarity: 'Rare',
        extended: { pdps: 412.34, edps: 88.12 },
        properties: [{ name: '[Bow|활]' },
          { name: '치명타 명중 확률', values: [['6.50%', 0]] },
          { name: '초당 공격 횟수', values: [['1.42', 1]] }],
        explicitMods: ['[Physical|물리] 피해 168% 증가'],
        implicitMods: [{ description: '모든 스킬 레벨 +1' }] } };
    var mut = function (path, v) {
      var d = JSON.parse(JSON.stringify(kr)), ref = d;
      for (var i = 0; i < path.length - 1; i++) ref = ref[path[i]];
      ref[path[path.length - 1]] = v;
      return d;
    };
    var r = normalizeRow(kr, null);
    assert(r && r.pdps === 412.3 && r.edps === 88.1, '정상 활 수치');
    assert(r.aps === 1.42 && r.crit === 6.5, 'aps/crit 추출');
    assert(r.cur === 'divine' && r.price === 5 && r.rarity === 'Rare', '가격/등급');
    assert(r.mods.length === 2 && r.mods[0] === '모든 스킬 레벨 +1', '옵션(문자열+description) 합침');
    // 7개 mod 키를 serve.py MOD_KEYS 와 같은 순서로 뽑아야 지문(fp) 일치 — 룬/제작 포함(mods 는 raw)
    var runed = mut(['item', 'runeMods'], ['룬 피해 +10']);
    runed.item.craftedMods = ['제작 생명력 +50'];
    var rr = normalizeRow(runed, null);
    assert(JSON.stringify(rr.mods) === JSON.stringify(
      ['모든 스킬 레벨 +1', '[Physical|물리] 피해 168% 증가', '룬 피해 +10', '제작 생명력 +50']),
      '7키 추출·순서(implicit→explicit→rune→crafted, serve.py MOD_KEYS 정합)');
    var big = mut(['item', 'explicitMods'], Array(200).fill(Array(9999).join('가')));
    var br = normalizeRow(big, null);
    assert(br.mods.length <= 40 && br.mods[0].length <= 200, '옵션 길이 상한');
    assert(normalizeRow(mut(['item', 'name'], Array(9999).join('활')), null).name.length <= 120, '이름 길이 상한');
    assert(r.name === '파멸의 노래 고급 광신자 활', '이름');
    assert(normalizeRow(mut(['item', 'properties', 0, 'name'], '[Crossbow|쇠뇌]'), null) === null, '석궁은 버린다');
    assert(normalizeRow(mut(['item', 'properties', 0, 'name'], 'Bow'), null) !== null, '영문 Bow');
    assert(normalizeRow(mut(['item', 'properties', 0, 'name'], 'Crossbow'), null) === null, '영문 Crossbow 버림');
    var noClass = mut(['item', 'properties', 0, 'name'], '품질');
    assert(normalizeRow(noClass, null) === null, '클래스 확인 불가 + id 확인 불가 -> 버린다');
    assert(normalizeRow(noClass, { abc: 1 }) !== null, '활 검색 id 로는 확인된다');
    assert(normalizeRow(mut(['listing', 'price', 'currency'], 'transmute'), null) === null, '규격 밖 화폐 버림');
    assert(normalizeRow(mut(['listing', 'price'], {}), null) === null, '가격 없음 버림');
    assert(normalizeRow(mut(['item', 'extended'], {}), null) === null, 'DPS 없음 버림');
    var noRar = mut(['item', 'rarity'], null);
    assert(normalizeRow(noRar, null) === null, '등급 불명 버림');
    noRar.item.frameType = 3;
    assert(normalizeRow(noRar, null).rarity === 'Unique', 'frameType 대체');
    noRar.item.frameType = 13;
    assert(normalizeRow(noRar, null).rarity === 'Rare', '룬 박힌 레어(frameType 13)도 Rare');
    var weird = mut(['item', 'rarity'], 'Weird'); weird.item.frameType = 2;
    assert(normalizeRow(weird, null).rarity === 'Rare', '4종 밖 등급 문자열은 frameType 으로');
    assert(normalizeRow(mut(['item', 'rarity'], 'Weird'), null) === null, '4종 밖 + frameType 없음 = 등급 불명');
    assert(normalizeRow(mut(['listing', 'price', 'currency'], 'mirror'), null) !== null, '미러 가격은 받는다');
    assert(leagueOf('https://poe.kakaogames.com/api/trade2/search/poe2/Runes%20of%20Aldur') === 'Runes of Aldur', '검색 URL 에서 리그');
    assert(normalizeRow(kr, null).league === 'Runes of Aldur', '행에 리그가 실린다');
    assert(normalizeRow(null, null) === null && normalizeRow({}, null) === null, '빈 입력');
    assert(clean('[Physical|물리] 피해') === '물리 피해' && clean(null) === '', 'clean');
    assert(num('6.50%') === 6.5 && num(null) === 0, 'num');
    assert(HOOK.indexOf('__poe2bowHooked') > 0 && HOOK.indexOf('weapon.bow') > 0, 'HOOK 문자열');
    // HOOK 은 문자열로 페이지 세계에 주입된다 — 샌드박스 변수를 참조하면 거래소 전체가 죽는다(0.2.0 실사고).
    // 페이지 세계를 흉내 낸 빈 컨텍스트에서 실제로 평가해 fetch/XHR 이 던지지 않는지, 리그가 emit 에 실리는지 본다.
    var vm = require('vm');
    var emitted = [];
    var page = {
      window: { fetch: function () { return Promise.resolve({ clone: function () { return this; }, json: function () { return Promise.resolve({ result: [{ id: 'z' }] }); } }); },
                dispatchEvent: function (ev) { emitted.push(JSON.parse(ev.detail)); } },
      document: { documentElement: { setAttribute: function () {} } },
      CustomEvent: function (name, init) { this.type = name; this.detail = init && init.detail; },
      XMLHttpRequest: function () {}, JSON: JSON, Promise: Promise, String: String,
    };
    page.XMLHttpRequest.prototype = { open: function () {}, send: function () {}, addEventListener: function () {} };
    vm.runInNewContext(HOOK, page);
    var threw = null;
    try {
      page.window.fetch('https://poe.kakaogames.com/api/trade2/search/poe2/Runes%20of%20Aldur/abc', { method: 'POST', body: '{}' });
      page.window.fetch('https://poe.kakaogames.com/api/trade2/fetch/a,b?query=abc');
      page.window.fetch('https://poe.kakaogames.com/unrelated');
      var x = new page.XMLHttpRequest(); x.open('GET', 'https://poe.kakaogames.com/api/trade2/fetch/c?query=abc'); x.send();
    } catch (err) { threw = err; }
    assert(threw === null, '페이지 세계에서 훅이 던지면 안 된다: ' + threw);
    setTimeout(function () {
      var f = emitted.filter(function (m) { return m.kind === 'fetch'; });
      assert(f.length >= 1 && f[0].league === 'Runes of Aldur', '훅이 잡은 리그가 fetch emit 에 실린다: ' + JSON.stringify(f));
      console.log('harvester self-test PASS');
    }, 20);
  }
})();
