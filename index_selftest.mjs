// index.html self-test runner: eval the <script> block (minus the DOM boot tail)
// under light DOM stubs. Self-tests are IIFEs that call console.error on failure.
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync(process.argv[2] || 'index.html', 'utf8');
let script = html.slice(html.indexOf('<script>') + '<script>'.length, html.indexOf('</script>'));
// Cut the DOM boot tail (refresh()/selectWeapon('') need heavy canvas/DOM). Self-tests run before it.
const bootAt = script.indexOf("$('unit').value = unit;");
if (bootAt < 0) { console.error('boot marker not found'); process.exit(2); }
script = script.slice(0, bootAt);

const fakeCtx = () => new Proxy({}, { get: () => () => {} });
function fakeEl() {
  const t = { style: {}, dataset: {}, classList: { add(){}, remove(){}, toggle(){}, contains(){return false} },
              value: '', textContent: '', innerHTML: '', checked: false, children: [], options: [] };
  return new Proxy(t, {
    get(o, k) {
      if (k in o) return o[k];
      if (typeof k === 'symbol') return undefined;
      if (['appendChild','removeChild','addEventListener','removeEventListener','setAttribute',
           'removeAttribute','insertBefore','append','remove','focus','blur','click','scrollIntoView'].includes(k))
        return () => {};
      if (k === 'getContext') return () => fakeCtx();
      if (k === 'getBoundingClientRect') return () => ({x:0,y:0,top:0,left:0,right:0,bottom:0,width:0,height:0});
      if (k === 'querySelector' || k === 'closest') return () => null;  // 실제 빈 DOM처럼 "없음"
      if (k === 'querySelectorAll' || k === 'getElementsByClassName') return () => [];
      return fakeEl();
    },
    set(o, k, v) { o[k] = v; return true; },
  });
}

const errors = [];
// 표의 헤더 칸 수와 본문 칸 수가 어긋나면 열이 통째로 밀린다. 실제로 밀려 있었다 —
// 방패에서 JS 가 th 두 개만 display:none 해서 헤더 7 / 본문 9 였고, 스크린샷으로도
// "값이 한 칸씩 밀렸다" 정도로만 보여 원인을 못 짚는 종류였다. 지금은 헤더·본문이 같은
// 클래스(c-phys/c-ele)로 묶여 CSS 한 규칙에 같이 숨지만, 나중에 td 만 추가하는 실수는 여전히 가능하다.
{
  const thead = html.slice(html.indexOf('<thead>'), html.indexOf('</thead>'));
  const body = html.slice(html.indexOf('.innerHTML = view.map('), html.indexOf('</tr>`;'));
  const nTh = (thead.match(/<th[\s>]/g) || []).length;
  const nTd = (body.match(/<td[\s>]/g) || []).length;
  if (nTh !== nTd) errors.push(`매물 표 칸 수 불일치 — thead ${nTh}개 vs 행 템플릿 ${nTd}개`);
  // 숨기는 열은 반드시 헤더·본문 양쪽에 같은 수로 있어야 한다(한쪽만 숨으면 다시 밀린다)
  for (const c of ['c-phys', 'c-ele']) {
    const inHead = (thead.match(new RegExp(c, 'g')) || []).length;
    const inBody = (body.match(new RegExp(c, 'g')) || []).length;
    if (inHead !== 1 || inBody !== 1)
      errors.push(`열 클래스 ${c} — thead ${inHead}개 / 본문 ${inBody}개 (각각 1개여야 함)`);
  }
  if (!/#tbl\.arm[^{]*\.c-phys/.test(html))
    errors.push('#tbl.arm .c-phys 숨김 규칙이 없다 — 방패에서 물리 열이 안 숨는다');
}

// 방패 화면에서 지표 이름이 'DPS' 로 굳어 있으면, 한 화면 안에서 같은 값을 두 이름으로 부른다.
// 실행 시 만들어지는 문구(프리미엄·범례·툴팁·빈 상태)는 metricName() 을 거쳐야 한다.
{
  const script = html.slice(html.indexOf('<script>'));
  const bad = [];
  for (const [i, line] of script.split(/\r?\n/).entries()) {
    const s = line.trim();
    if (s.startsWith('//') || !/DPS/.test(s)) continue;
    if (/throw new Error|console\.|assert/.test(s)) continue;   // 자체 테스트 내부 문구는 화면에 안 나간다
    // 템플릿/문자열 안에서 화면에 나가는 'DPS' 만 본다. 주석·상수명·정규식은 뺀다.
    if (!/['\"`]/.test(s)) continue;
    if (/metricName|PER_DPS|COUNTED|isOffDps|th-dps|총 DPS|물리 DPS|원소 DPS/.test(s)) continue;
    if (/거래소 DPS|DPS 곡선|DPS는 거래소/.test(s)) continue;   // 무기 설명문(방패에선 안 보임)
    bad.push(`${i + 1}: ${s.slice(0, 90)}`);
  }
  if (bad.length) errors.push(`화면 문구에 지표 이름이 굳어 있다 — metricName() 을 쓸 것:
    ${bad.join('\n    ')}`);
}

// 무기 목록이 세 벌(수집기·워커·이 페이지) 있는데 셋이 어긋나면 무기 탭 하나가 조용히
// 빈 화면이 된다 — 수집기가 안 뜨거나, 워커가 크라우드를 거부하거나, 페이지가 없는 파일을 찾는다.
// 지금까지 아무도 이 정합을 검사하지 않았다(부적이 09-05 에 추가된 최신 항목이라 딱 이 자리다).
{
  const pick = (t, re) => [...t.matchAll(re)].map((m) => m.slice(1));
  const py = fs.readFileSync('serve.py', 'utf8');
  const wk = fs.readFileSync('worker/src/index.js', 'utf8');
  const block = py.slice(py.indexOf('ATTACK_WEAPONS = ['), py.indexOf(']', py.indexOf('ATTACK_WEAPONS = [')));
  // serve.py: ("weapon.bow", "", "활")  — 카테고리 id 와 접미사만 본다(라벨은 콘솔 전용)
  // 방패는 armour.shield 라 weapon.* 만 훑으면 놓친다
  const pyW = pick(block, /\("((?:weapon|armour)\.[a-z]+)",\s*"([a-z]*)"/g);
  // CATEGORIES 블록만 훑는다. 파일 전체를 보면 76행의 기본값 "weapon.bow" 가 섞여서
  // 허용목록에서 그 한 종을 지워도 집합이 그대로라 검사가 통과한다(실측).
  const wkBlock = wk.slice(
    wk.indexOf('CATEGORIES = new Set(['),
    wk.indexOf(']', wk.indexOf('CATEGORIES = new Set([')),
  );
  const wkW = [...wkBlock.matchAll(/"((?:weapon|armour)\.[a-z]+)"/g)].map((m) => m[1]);
  const htmlW = pick(html, /\{ suffix: '([a-z]*)',\s*label: '([^']+)' \}/g).map((x) => x[0]);

  const pyIds = pyW.map((x) => x[0]).sort();
  const wkIds = [...new Set(wkW)].sort();
  // 정확히 같아야 한다. 한때 "워커는 부분집합이면 맞다"로 느슨하게 뒀는데, 그 순간
  // armour.shield 누락이 이 검사를 그대로 통과했고 첫 방패 사이클에서 크라우드가
  // HTTP 400(bad cat)으로 통째로 버려졌다. 수집기가 보내는 것은 워커가 다 받아야 한다.
  if (pyIds.join(",") !== wkIds.join(","))
    errors.push(`무기 id 불일치 — serve.py [${pyIds}] vs worker [${wkIds}]`);

  const pySfx = pyW.map((x) => x[1]).sort();
  const htmlSfx = [...htmlW].sort();
  if (pySfx.join(',') !== htmlSfx.join(','))
    errors.push(`무기 접미사 불일치 — serve.py [${pySfx}] vs index.html [${htmlSfx}]`);
}

// 화면 문구가 다시 '활'로 굳는 것을 막는다 — 무기 7종이 같은 화면을 쓴다.
// 이 문구들은 DOM 이 다 필요한 함수 안에 있어 sandbox 실행으로는 안 닿는다 → 원문으로 본다.
for (const bad of ['조건에 맞는 활이', 'DPS가 0인 활뿐', '최전선에 활이', '활 2개 이상부터']) {
  if (html.includes(bad)) errors.push(`무기 이름이 문구에 굳어 있음: ${bad}`);
}
const documentStub = {
  getElementById: () => fakeEl(),
  createElement: () => fakeEl(),
  querySelector: () => fakeEl(),
  querySelectorAll: () => [],
  addEventListener: () => {},
  body: fakeEl(),
  documentElement: fakeEl(),
};
const sandbox = {
  document: documentStub,
  window: { addEventListener: () => {}, matchMedia: () => ({ matches:false, addEventListener(){}, addListener(){} }),
            location: { href:'', search:'', hash:'' }, devicePixelRatio: 1 },
  localStorage: { _s:{}, getItem(k){ return k in this._s ? this._s[k] : null; },
                  setItem(k,v){ this._s[k]=String(v); }, removeItem(k){ delete this._s[k]; } },
  navigator: { language: 'ko', clipboard: { writeText: async () => {} } },
  fetch: async () => ({ ok: false, status: 404, json: async () => ({}) }),
  requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  Image: function(){ return fakeEl(); },
  console: { log: () => {}, warn: () => {}, info: () => {}, error: (...a) => errors.push(a.map(String).join(' ')) },
};
// 재방문자를 흉내낸다 — 저장된 매물이 있어야 부팅 첫 줄의 filter 콜백이 실제로 돈다.
// 빈 localStorage 로만 돌리면 부팅 순서 결함(선언 전 호출 등)이 통째로 안 잡힌다:
// numOk 를 쓰는 줄이 선언보다 위에 있어 재방문자 화면이 통째로 죽던 것을 이 검사가 못 잡았다.
sandbox.localStorage._s['poe2bows'] = JSON.stringify([
  { id: 'seed1', pdps: 100, edps: 0, aps: 1.2, crit: 5, price: 1, cur: 'exalted',
    rarity: 'Rare', mods: [], t: Date.now() },
]);
sandbox.self = sandbox; sandbox.globalThis = sandbox;

try {
  vm.runInNewContext(script, sandbox, { filename: 'index.html.script' });
} catch (e) {
  console.error('UNCAUGHT during eval:', e && e.stack || e);
  process.exit(1);
}
if (errors.length) { console.error('SELF-TEST FAIL:\n' + errors.join('\n')); process.exit(1); }
console.log('index.html self-test PASS');
