// 風哨的冒煙測試：開得起來、雷電在天氣頁、延伸預報已移除、手機寬度不爆版。
import { test, expect } from '@playwright/test';

// 一個有座標的站臺（苗栗外海附近），落雷點就丟在它旁邊
const STATION = { id: 's1', name: '測試岸際站', type: 't1', status: 'on', hum: '', note: '',
  lat: 24.6, lng: 120.82 };

const PLACEHOLDER = { hasKey: false, updated: new Date().toISOString(), strikes: [],
  note: 'repo 尚未設定 CWA_API_KEY Secret' };

function lightningWithStrikes() {
  const t = new Date(Date.now() - 5 * 60000).toISOString();
  return {
    hasKey: true, updated: new Date().toISOString(), window: '過去 1 小時', source: 'fileapi KMZ',
    n: 2, cg: 1, ic: 1,
    strikes: [
      { lat: 24.62, lng: 120.83, t, cg: true },   // 約 2.5 km：要算進去
      { lat: 25.6, lng: 121.9, t, cg: false },    // 一百多公里外：不算
    ],
  };
}

/* 每個測試：擋掉所有對外連線、換掉後端落雷檔、預先放一個站臺並開自動更新
   （開了自動更新，載入時就會跑一輪 loadTyphoon，不用另外按「更新」）。 */
async function boot(page, lightning, typhoon = PLACEHOLDER) {
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.context().route(u => !/^(127\.0\.0\.1|localhost)$/.test(new URL(u).hostname),
    r => r.abort());
  await page.route('**/data/lightning.json', r => r.fulfill({ json: lightning }));
  await page.route('**/data/typhoon.json', r => r.fulfill({ json: typhoon }));
  // 停班停課、天氣（雨量陣風特報）後端檔、颱風強度表：當成還沒產生，結果不看 repo 裡有沒有這些檔
  await page.route(/\/data\/(dgpa|tyhist|wx)\.json/, r => r.fulfill({ status: 404, body: '' }));
  await page.addInitScript(st => {
    // 初始化腳本也會在 Windy 的 iframe（被擋掉後是錯誤頁）裡跑，那裡碰 localStorage 會丟錯
    if (window.top !== window) return;
    if (!localStorage.getItem('galepost.v2')) {
      const now = Date.now();
      localStorage.setItem('galepost.v2', JSON.stringify({
        v: 2, stations: [{ ...st, at: now, mt: now }],
        // 模擬舊版存檔：延伸預報開著。新版載入後要把這三個鍵清掉
        cfg: { auto: true, extOn: true, extUrl: 'https://api.open-meteo.com/x', extDays: 16 },
      }));
    }
  }, STATION);
  await page.goto('/index.html');
  return errors;
}

// 雷電以前在「實況」頁，八個子頁籤併成五個之後跟天氣、雷達同一頁
async function openLive(page) {
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  await expect(page.locator('#tyThunder')).toBeVisible();
}

test('開得起來，沒有 JavaScript 錯誤；頂欄與導覽上的時鐘都拿掉了', async ({ page }) => {
  const errors = await boot(page, PLACEHOLDER);
  await expect(page).toHaveTitle(/風哨/);
  await expect(page.locator('header')).toHaveCount(0);
  // 使用者：「回報頁面按鈕上方日期時機是什麼，把它刪除」——掛在導覽上緣、回報鈕正上方的那顆時鐘
  await expect(page.locator('#now, .clock')).toHaveCount(0);
  await expect(page.locator('.nav button[data-p="rp"]')).toBeVisible();
  await page.waitForTimeout(500);
  expect(errors).toEqual([]);
});

test('沒設金鑰：天氣頁的雷電寫「尚未啟用」，絕不顯示成綠色的「沒有落雷」', async ({ page }) => {
  const errors = await boot(page, PLACEHOLDER);
  await openLive(page);
  const card = page.locator('#tyThunder');
  await expect(card).toContainText('逐站落雷實測尚未啟用');
  await expect(card).toContainText('CWA_API_KEY');
  await expect(card).not.toContainText('km 內沒有落雷');
  // 不是紅色錯誤框：還沒設定不是壞掉
  await expect(card.locator('.msg.e')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('有後端落雷資料：逐站算出對地落雷，沒有颱風也照樣顯示', async ({ page }) => {
  const errors = await boot(page, lightningWithStrikes());
  await openLive(page);
  const card = page.locator('#tyThunder');
  await expect(card).toContainText('1 站 15 km 內有雲對地落雷');
  await expect(card).toContainText(STATION.name);
  await expect(card).toContainText('後端管線');
  await expect(card).not.toContainText('尚未啟用');
  expect(errors).toEqual([]);
});

test('延伸預報已移除：天氣頁、設定頁都沒有，舊存檔的開關被清掉', async ({ page }) => {
  const errors = await boot(page, PLACEHOLDER);
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  // 沒有颱風資料時天氣頁以前會被清空；現在要有內容
  await expect(page.locator('#tyWx')).toContainText('現在位置');     // 「天氣」大標拿掉了，看地點分頁
  await expect(page.locator('#tyWx')).not.toContainText('延伸');
  await page.locator('.nav button[data-p="cfg"]').click();
  await page.locator('#cfgSeg [data-sub="cfg:src"]').click();
  await expect(page.locator('#p-cfg')).toContainText('落雷');
  await expect(page.locator('#p-cfg')).toContainText('天氣預報');
  await expect(page.locator('#p-cfg')).not.toContainText('延伸預報');
  const cfg = await page.evaluate(() => JSON.parse(localStorage.getItem('galepost.v2')).cfg);
  expect(cfg.extOn).toBeUndefined();
  expect(cfg.extUrl).toBeUndefined();
  expect(errors).toEqual([]);
});

test('iPhone 寬度下天氣頁不會橫向爆版', async ({ page }) => {
  await boot(page, lightningWithStrikes());
  await openLive(page);
  await expect(page.locator('#tyThunder')).toContainText('雲對地');
  const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(over).toBeLessThanOrEqual(0);
});

/* ── 後端颱風檔（data/typhoon.json）：存的是氣象署原始 records，由前端同一套解析器解析 ── */
const iso = ms => new Date(ms).toISOString();
// 形狀照實際回應：PascalCase、座標分兩欄、單筆颱風不包陣列
function typhoonFile({ updated = Date.now(), cyclone = true, warnLog, warn } = {}) {
  const t0 = Date.now() - 3600000;
  const tc = {
    TyphoonName: 'TESTA', CwaTyphoonName: '測試颱風', CwaTdNo: '22', CwaTyNo: '29',
    AnalysisData: { Fix: [{ DateTime: iso(t0), CoordinateLatitude: '20.5', CoordinateLongitude: '124.5',
      MaxWindSpeed: '35', MaxGustSpeed: '45', PressureCenter: '960',
      MovingDirection: 'NW', MovingSpeed: '18', Circle15ms: { Radius: '220' } }] },
    ForecastData: { InitialTime: iso(t0), Fix: [
      { Tau: '24', CoordinateLatitude: '22.4', CoordinateLongitude: '122.3', MaxWindSpeed: '38',
        Radius70PercentProbability: '120' },
      { Tau: '48', CoordinateLatitude: '24.6', CoordinateLongitude: '120.6', MaxWindSpeed: '30',
        Radius70PercentProbability: '200' },
    ] },
  };
  return {
    v: 2, hasKey: true, updated: iso(updated), source: 'datastore', count: cyclone ? 1 : 0,
    raw: { 'W-C0034-005': cyclone ? { tropicalCyclones: { TropicalCyclone: tc } } : { tropicalCyclones: {} },
           'W-C0034-001': warn || { info: [] } },
    ...(warnLog ? { warnLog } : {}),
  };
}

async function openTyphoon(page) {
  await page.locator('.nav button[data-p="ty"]').click();
}

test('後端颱風檔：不必填授權碼就看得到官方路徑', async ({ page }) => {
  const errors = await boot(page, PLACEHOLDER, typhoonFile());
  await openTyphoon(page);
  await expect(page.locator('#tyBody')).toContainText('測試颱風');
  await expect(page.locator('#tyMsg')).toContainText('中央氣象署');
  await expect(page.locator('#tyMsg')).not.toContainText('GDACS');
  expect(errors).toEqual([]);
});

test('後端颱風檔：有金鑰但目前沒颱風 → 明講查無，不退去 GDACS', async ({ page }) => {
  const errors = await boot(page, PLACEHOLDER, typhoonFile({ cyclone: false }));
  await openTyphoon(page);
  await expect(page.locator('#tyMsg')).toContainText('目前查無活動中的熱帶氣旋');
  await expect(page.locator('#tyMsg')).not.toContainText('GDACS');
  expect(errors).toEqual([]);
});

test('後端颱風檔太舊：講出排程可能停了，不拿舊路徑當現況', async ({ page }) => {
  const errors = await boot(page, PLACEHOLDER, typhoonFile({ updated: Date.now() - 20 * 3600000 }));
  await openTyphoon(page);
  await expect(page.locator('#tyMsg')).toContainText('沒有更新');
  await expect(page.locator('#tyBody')).not.toContainText('測試颱風');
  expect(errors).toEqual([]);
});

test('後端颱風檔報錯：原因顯示出來，不說成氣象署沒發布', async ({ page }) => {
  const errors = await boot(page, PLACEHOLDER,
    { v: 2, hasKey: true, updated: iso(Date.now()), error: 'W-C0034-005: 授權碼被拒（HTTP Error 401）' });
  await openTyphoon(page);
  await expect(page.locator('#tyMsg')).toContainText('後端颱風資料抓取失敗');
  await expect(page.locator('#tyMsg')).toContainText('授權碼被拒');
  expect(errors).toEqual([]);
});

/* ── 陣風判斷、預設待機 ── */
// 站臺放在颱風中心旁邊，推估陣風一定超過待機門檻
const NEAR = { id: 'n1', name: '中心旁測試站', type: 't1', status: 'on', hum: null, note: '',
  lat: 20.6, lng: 124.6 };
// 有站臺時打開 App 會自動到戰情 › 總覽；要操作站臺卡的測試先切回站臺頁
async function toEq(page) {
  await page.locator('.nav button[data-p="ty"][aria-selected="true"]').waitFor();
  await page.locator('.nav button[data-p="eq"]').click();
}
async function bootWith(page, stations, extra = {}) {
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.context().route(u => !/^(127\.0\.0\.1|localhost)$/.test(new URL(u).hostname), r => r.abort());
  await page.route('**/data/lightning.json', r => r.fulfill({ json: extra.ltn || PLACEHOLDER }));
  await page.route('**/data/typhoon.json', r => r.fulfill({ json: extra.ty || typhoonFile() }));
  // 停班停課、天氣後端檔、颱風強度表：預設當成還沒產生（404），要用的測試自己給
  await page.route('**/data/dgpa.json', r => extra.dgpa ? r.fulfill({ json: extra.dgpa }) : r.fulfill({ status: 404, body: '' }));
  await page.route('**/data/tyhist.json', r => extra.tyhist ? r.fulfill({ json: extra.tyhist }) : r.fulfill({ status: 404, body: '' }));
  await page.route('**/data/wx.json', r => extra.wx ? r.fulfill({ json: extra.wx }) : r.fulfill({ status: 404, body: '' }));
  await page.addInitScript(([sts, ex]) => {
    if (window.top !== window) return;
    if (localStorage.getItem('galepost.v2')) return;
    const now = Date.now();
    if (sts) localStorage.setItem('galepost.v2', JSON.stringify({
      v: 2, stations: sts.map(s => ({ ...s, at: s.at || now, mt: now })), cfg: { auto: true, ...(ex.cfg || {}) },
      ...(ex.db || {}) }));
    if (ex.tysub) localStorage.setItem('galepost.ui.tysub', ex.tysub);
  }, [stations, { cfg: extra.cfg, db: extra.db, tysub: extra.tysub }]);
  await page.goto('/index.html');
  return errors;
}

test('風級一律看陣風：推估＝持續風×1.3，實測優先用測站最大陣風', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await toEq(page);
  await expect(page.locator('.st[data-id="n1"]')).toBeVisible();
  await page.waitForFunction(() => typeof SERIES !== 'undefined' && SERIES && SERIES.length > 1);
  const r = await page.evaluate(() => {
    const st = DB.stations[0], p = { lat: st.lat, lng: st.lng };
    const w = windAt(SERIES[0], p);
    return {
      bf: stationImpact(SERIES, st, DB.cfg).now.bf,
      want: ms2bf(w.ms * calFactor(st, w.dirFrom) * GUST_FACTOR),
      sustained: ms2bf(w.ms),
      measured: obsGust({ o: { gust: 20, wind: 9 } }),
      estimated: obsGust({ o: { gust: null, wind: 10 } }),
    };
  });
  expect(r.bf).toBe(r.want);
  expect(r.bf).toBeGreaterThanOrEqual(r.sustained);
  expect(r.measured).toEqual({ bf: 8, est: false });   // 20 m/s 陣風＝8 級，不是看平均風 9 m/s 的 5 級
  expect(r.estimated).toEqual({ bf: 6, est: true });   // 沒報陣風：10×1.3＝13 m/s＝6 級，標成估的
  expect(errors).toEqual([]);
});

test('達門檻建議「待機」而不是關機；按關機要再確認，取消就不變', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await toEq(page);
  const card = page.locator('.st[data-id="n1"]');
  await expect(card.locator('.st-alert')).toContainText('待機');
  await expect(card.locator('.st-alert')).not.toContainText('應立即關機');
  await expect(card.locator('button.k-sb')).toHaveClass(/rec/);
  // 關機改在視窗裡確認（不用系統 confirm）：關掉視窗＝不關機
  await card.locator('button.k-off').click();
  await expect(page.locator('#off_ok')).toBeVisible();
  await page.locator('#shClose').click();
  await expect(card.locator('.st-tag')).toHaveText('開機');
  await card.locator('button.k-off').click();
  await page.locator('#off_ok').click();
  await expect(page.locator('.st[data-id="n1"] .st-tag')).toHaveText('關機');
  expect(errors).toEqual([]);
});

test('衝擊預判：目前狀態（含時間）對照評估，不一致的排最上面並標出要做什麼', async ({ page }) => {
  const at = Date.now() - 2 * 3600000;
  const FAR_SB = { id: 'f1', name: '遠方待機站', type: 't1', status: 'standby', hum: null, note: '', lat: 25.3, lng: 121.5, at };
  const FAR_ON = { id: 'f2', name: '遠方開機站', type: 't1', status: 'on', hum: null, note: '', lat: 23.98, lng: 121.6, at };
  const errors = await bootWith(page, [FAR_ON, FAR_SB, NEAR]);
  await page.evaluate(() => goTySub('st'));
  const box = page.locator('#tyImpact');
  await expect(box.locator('table.impact th').nth(1)).toHaveText('目前狀態');
  await expect(box.locator('table.impact th').nth(2)).toHaveText('評估');
  const rows = box.locator('table.impact tr:has(td)');
  await expect(rows).toHaveCount(3);
  // 中心旁開機站：達門檻 → 評估「待機」、⚠ 應待機，排第一列
  const near = rows.filter({ hasText: '中心旁測試站' });
  await expect(near).toHaveClass(/imp-bad/);
  await expect(near.locator('td').nth(1)).toContainText('開機');
  await expect(near.locator('td').nth(2)).toContainText('待機');
  await expect(near.locator('td').nth(2)).toContainText('⚠ 應待機');
  // 遠方待機站：風已低於復機門檻 → 可復機；目前狀態帶上次改狀態的時間
  const sb = rows.filter({ hasText: '遠方待機站' });
  await expect(sb).toHaveClass(/imp-bad/);
  await expect(sb.locator('td').nth(1)).toContainText('待機');
  const want = await page.evaluate(t => fmtShort(t), at);
  await expect(sb.locator('td').nth(1)).toContainText(want);
  await expect(sb.locator('td').nth(2)).toContainText('⚠ 可評估復機');
  // 遠方開機站：一致，排在最後
  const ok = rows.filter({ hasText: '遠方開機站' });
  await expect(ok).not.toHaveClass(/imp-bad/);
  await expect(ok.locator('td').nth(2)).toContainText('✓ 一致');
  // 開機站比待機站離颱風近、風也較大；只按風排的話它會排在待機站前面——要靠「不一致優先」才會掉到最後
  await expect(rows.last()).toContainText('遠方開機站');
  // 欄位變多時站名欄不能被擠成一字一行（iPhone 寬 390px 實際踩過）
  expect((await near.locator('td.nm').boundingBox()).width).toBeGreaterThan(80);
  await expect(box.locator('.imp-sum')).toContainText('2 站跟評估不一致（1 站應待機、1 站可評估復機）；1 站一致');
  expect(errors).toEqual([]);
});

/* ── 子頁籤整併 ── */
test('打開 App 就在戰情 › 總覽（舊存檔記的子頁不管）；總覽旁是播報員；各站在站臺 › 預判；回報頁不再有播報稿', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { tysub: 'live' });
  await expect(page.locator('.nav button[data-p="ty"]')).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#ty-now')).toBeVisible();
  await expect(page.locator('#tySeg button')).toHaveText(['總覽', '播報員', '路徑', '天氣']);
  await expect(page.locator('#tySeg button[data-ty="now"]')).toHaveAttribute('aria-pressed', 'true');
  const tops = await page.locator('#tySeg button').evaluateAll(bs => bs.map(b => Math.round(b.getBoundingClientRect().top)));
  expect(new Set(tops).size).toBe(1);
  // 播報員：在戰情頁裡,總覽右邊
  await page.locator('#tySeg button[data-ty="brief"]').click();
  await expect(page.locator('#ty-brief #tyBrief')).toBeVisible();
  await expect(page.locator('#tyBrief')).toContainText('氣象員播報');
  await expect(page.locator('#ty-now')).toBeHidden();
  await expect(page.locator('#rpSeg [data-sub="rp:brief"]')).toHaveCount(0);
  await expect(page.locator('#rpSeg button')).toHaveText(['回報稿', '撤除檢核']);
  // 舊名字「各站」→ 站臺 › 預判
  await page.evaluate(() => goTySub('st'));
  await expect(page.locator('#p-eq')).toBeVisible();
  await expect(page.locator('#tyImpact')).toContainText('應完成待機');
  await expect(page.locator('#tyImpact')).toContainText('推估陣風');
  // 切到別的子頁再重開：一樣回到總覽
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="map"]').click();
  await page.reload();
  await expect(page.locator('#ty-now')).toBeVisible();
  // 網址指定分頁（主畫面捷徑）照舊有效
  await page.goto('/index.html#rp');
  await expect(page.locator('#p-rp')).toBeVisible();
  expect(errors).toEqual([]);
});

test('還沒有任何站臺的新使用者：打開留在站臺頁看「開始使用」，自動帶到戰情也不算完成「看颱風」', async ({ page }) => {
  const errors = await bootWith(page, null);
  await expect(page.locator('#p-eq')).toBeVisible();
  await expect(page.locator('.guide')).toBeVisible();
  // 有站臺的人自動進戰情頁,引導的「看颱風」不會因此被勾掉
  // save() 完立刻重新整理：沒有在 pagehide 時把延後的寫入先寫掉,這一站會不見（也就驗不到下面）
  await page.evaluate(() => { DB.stations.push({ id: 'x', name: '甲', type: 't1', status: 'on', lat: 25, lng: 121.5, at: Date.now(), mt: Date.now() }); save(); });
  await page.reload();
  await expect(page.locator('#ty-now')).toBeVisible();
  expect(await page.evaluate(() => !!DB.cfg.guideTy)).toBe(false);
  expect(errors).toEqual([]);
});

/* ── 新手引導 ── */
test('第一次打開：顯示開始使用 0/5；收起後可從設定的使用說明叫回來', async ({ page }) => {
  const errors = await bootWith(page, null);
  const g = page.locator('.guide');
  await expect(g).toContainText('開始使用　0 / 5');
  await expect(g.locator('li.cur')).toContainText('建立站臺');
  await g.getByRole('button', { name: '收起' }).click();
  await expect(page.locator('.guide')).toHaveCount(0);
  await page.locator('.nav button[data-p="cfg"]').click();
  await page.locator('#cfgSeg [data-sub="cfg:sys"]').click();
  await page.getByText('一次颱風的操作順序').click();
  await expect(page.locator('.help')).toContainText('關機只在特殊需要時才按');
  await page.getByRole('button', { name: '重新顯示新手引導' }).click();
  await expect(page.locator('.guide')).toBeVisible();
  expect(errors).toEqual([]);
});

test('引導的打勾看實際狀態：有站臺有座標就勾前兩步，下一步是看颱風', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await toEq(page);
  const g = page.locator('.guide');
  await expect(g).toContainText('開始使用　2 / 5');
  await expect(g.locator('li.cur')).toContainText('看颱風動態');
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('.nav button[data-p="eq"]').click();
  await expect(page.locator('.guide')).toContainText('開始使用　3 / 5');
  expect(errors).toEqual([]);
});

/* ── 按錯可以刪：復原、逐筆刪除、清除紀錄 ── */
const DAY_AGO = Date.now() - 86400000;
test('按錯了按「復原」：狀態與原本的時間都退回去，回報不會多一筆完成開機', async ({ page }) => {
  const errors = await bootWith(page, [{ ...NEAR, at: DAY_AGO }],
    { db: { ctl: { start: Date.now() - 600000, end: 0, ty: '' }, since: Date.now() - 600000 } });
  await toEq(page);
  const card = page.locator('.st[data-id="n1"]');
  await card.locator('button.k-sb').click();
  await expect(card.locator('.st-tag')).toHaveText('待機');
  await page.locator('#at_undo').click();
  await expect(page.locator('.st[data-id="n1"] .st-tag')).toHaveText('開機');
  const st = await page.evaluate(() => ({ at: DB.stations[0].at, log: DB.log.length }));
  expect(st.at).toBe(DAY_AGO);
  expect(st.log).toBe(0);
  await page.locator('.nav button[data-p="rp"]').click();
  await expect(page.locator('#out')).not.toHaveValue(/中心旁測試站於.*完成開機/);
  expect(errors).toEqual([]);
});

test('時序每筆都能刪；刪最新那筆會把站臺退回上一個狀態', async ({ page }) => {
  const errors = await bootWith(page, [{ ...NEAR, at: DAY_AGO }],
    { db: { ctl: { start: Date.now() - 600000, end: 0, ty: '' }, since: Date.now() - 600000 } });
  await toEq(page);
  const card = page.locator('.st[data-id="n1"]');
  await card.locator('button.k-sb').click();
  await page.locator('#at_ok').click();
  await page.locator('.st[data-id="n1"] button.k-on').click();
  await page.locator('#at_ok').click();
  await page.locator('#eqSeg [data-sub="eq:log"]').click();          // 時序在第三個子頁
  await expect(page.locator('.ev')).toHaveCount(2);
  page.on('dialog', d => d.accept());
  // 最新那筆（完成開機）→ 退回待機
  await page.locator('.ev').first().locator('[data-undo]').click();
  await expect(page.locator('.st[data-id="n1"] .st-tag')).toHaveText('待機');
  // 再刪實施待機 → 退回開機，時間回到原本
  await page.locator('.ev').first().locator('[data-undo]').click();
  await expect(page.locator('.st[data-id="n1"] .st-tag')).toHaveText('開機');
  expect(await page.evaluate(() => DB.stations[0].at)).toBe(DAY_AGO);
  await expect(page.locator('.ev')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('已結束的管制可以整段清掉，畫面回到「尚未開始管制」', async ({ page }) => {
  const t = Date.now() - 300000;
  const errors = await bootWith(page, [NEAR], { db: {
    ctl: { start: t, end: t + 60000, ty: '' },
    log: [{ t, name: NEAR.name, from: 'on', to: 'standby', at: t }, { t: t + 1, name: NEAR.name, from: 'standby', to: 'on', at: t + 30000 }] } });
  await toEq(page);
  await page.locator('#eqSeg [data-sub="eq:log"]').click();
  await expect(page.locator('.ev')).toHaveCount(2);           // 舊紀錄沒有 id 也刪得到
  await expect(page.locator('.ev [data-undo]')).toHaveCount(2);
  page.once('dialog', d => d.accept());
  await page.locator('[data-clearlog]').click();
  await expect(page.locator('#eqBody')).toContainText('尚未開始管制');
  await expect(page.locator('.ev')).toHaveCount(0);
  expect(errors).toEqual([]);
});

/* ── 官方警報發布時間 ── */
const W = (no, kind, headline, h) => ({ ty: '29', name: '測試颱風', no, kind, headline,
  effective: new Date(Date.now() - h * 3600000).toISOString() });
test('警報發布時間：第 1 報有收到就是確定時間；陸上前一報有收到也是確定', async ({ page }) => {
  const ty = typhoonFile({ warnLog: [W(1, 'SEA', '海上颱風警報', 15), W(4, 'SEA', '海上颱風警報', 6),
    W(5, 'SEA_LAND', '海上陸上颱風警報', 3)] });
  const errors = await bootWith(page, [NEAR], { ty, cfg: { introA: '氣象署於{海上警報}發布海上警報、{陸上警報}發布陸上警報' } });
  await page.locator('.nav button[data-p="ty"]').click();
  const box = page.locator('.wtime');
  await expect(box).toContainText('海上颱風警報');
  await expect(box).toContainText('第 1 報');
  await expect(box).toContainText('第 5 報');
  await expect(box).not.toContainText('推算');
  await page.locator('.nav button[data-p="rp"]').click();
  await expect(page.locator('#out')).toHaveValue(/氣象署於\d+月\d+日\d+時發布海上警報、\d+月\d+日\d+時發布陸上警報/);
  expect(errors).toEqual([]);
});

test('警報發布時間：第 1 報沒收到寫「推算」；陸上沒發布寫「尚未發布」；播報稿也有', async ({ page }) => {
  const ty = typhoonFile({ warnLog: [W(2, 'SEA', '海上颱風警報', 6), W(3, 'SEA', '海上颱風警報', 3)] });
  const errors = await bootWith(page, [NEAR], { ty });
  await page.locator('.nav button[data-p="ty"]').click();
  const box = page.locator('.wtime');
  await expect(box).toContainText('推算');
  await expect(box).toContainText('尚未發布');
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="brief"]').click();
  await expect(page.locator('#tyBrief')).toContainText('海上颱風警報發布：約');
  await expect(page.locator('#tyBrief')).toContainText('陸上颱風警報發布：尚未發布');
  expect(errors).toEqual([]);
});

test('警報單過了下次更新時間還沒新報：照樣顯示並提醒，不當成沒有警報', async ({ page }) => {
  const eff = new Date(Date.now() - 6 * 3600000).toISOString(), exp = new Date(Date.now() - 2 * 3600000).toISOString();
  const warn = { info: [{ headline: '海上颱風警報', effective: eff, expires: exp, area: [{ areaDesc: '巴士海峽' }],
    description: { 'typhoon-info': [{ section: [{ title: '警報報數', value: '3' }, { title: '颱風編號', value: '29' }] }] } }] };
  const errors = await bootWith(page, [NEAR], { ty: typhoonFile({ warn }) });
  await page.locator('.nav button[data-p="ty"]').click();
  // 警報卡併進名牌了
  await expect(page.locator('.tyh')).toContainText('海上颱風警報');
  await expect(page.locator('.tyh')).toContainText('第 3 報');
  await expect(page.locator('.tyh')).toContainText('可能已經有更新的警報單');
  expect(errors).toEqual([]);
});

/* ── 風速與陣風都列，取最高為待機標準 ── */
test('站臺卡列出推估與實測的平均風、陣風；判斷取四者最高', async ({ page }) => {
  const errors = await bootWith(page, [{ ...NEAR, lat: 23.0, lng: 121.5 }]);
  const card = page.locator('.st[data-id="n1"]');
  await expect(card.locator('.st-w')).toContainText('推估');
  await expect(card.locator('.st-w')).toContainText('平均');
  await expect(card.locator('.st-w')).toContainText('陣風');
  await expect(card.locator('.st-w')).toContainText('待機判斷取最高');
  const r = await page.evaluate(() => {
    const st = DB.stations[0];
    const imp = stationImpact(SERIES, st, DB.cfg);
    // 放一個就在站旁邊的測站：平均風 25 m/s（10 級）、陣風 30 m/s（11 級），比推估大
    OBS = [{ name: '旁邊測站', pos: { lat: st.lat + 0.01, lng: st.lng }, wind: 25, gust: 30 }];
    const a = windRead(st, imp);
    OBS = [{ name: '旁邊測站', pos: { lat: st.lat + 0.01, lng: st.lng }, wind: 0.5, gust: null }];
    const b = windRead(st, imp);
    return { a, b, est: ms2bf(imp.now.gust) };
  });
  expect(r.a.bf).toBe(11);
  expect(r.a.src).toBe('實測陣風');
  expect(r.b.bf).toBe(r.est);                  // 實測很小 → 取推估陣風
  expect(r.b.src).toBe('推估陣風');
  expect(errors).toEqual([]);
});

/* ── 時間校正、統計卡、建議處置、編輯儲存 ── */
test('按待機後跳出時間校正：改成 10 分鐘前，站臺與時序都用改過的時間', async ({ page }) => {
  const errors = await bootWith(page, [{ ...NEAR, at: DAY_AGO }],
    { db: { ctl: { start: Date.now() - 3600000, end: 0, ty: '' }, since: Date.now() - 3600000 } });
  await toEq(page);
  await page.locator('.st[data-id="n1"] button.k-sb').click();
  await expect(page.locator('#at_in')).toBeVisible();
  await page.locator('[data-ago="10"]').click();
  await page.locator('#at_ok').click();
  const r = await page.evaluate(() => ({ at: DB.stations[0].at, log: DB.log[DB.log.length - 1].at }));
  expect(Math.abs(r.at - (Date.now() - 600000))).toBeLessThan(120000);
  expect(r.log).toBe(r.at);
  expect(errors).toEqual([]);
});

test('按過狀態鈕之後，編輯視窗的儲存、刪除照樣按得到（提示列不會擋住）', async ({ page }) => {
  const errors = await bootWith(page, [{ ...NEAR, at: DAY_AGO }, { ...NEAR, id: 'n2', name: '第二站' }]);
  await toEq(page);
  await page.locator('.st[data-id="n1"] button.k-sb').click();
  await page.locator('#at_ok').click();
  await page.waitForTimeout(3000);              // 等提示列消失
  await page.locator('[data-edit="n1"]').click();
  await page.fill('#f_name', '改過的名字');
  await page.locator('#f_save').click({ timeout: 3000 });
  await expect.poll(() => page.evaluate(() => DB.stations[0].name)).toBe('改過的名字');
  await page.locator('[data-edit="n2"]').click();
  page.once('dialog', d => d.accept());
  await page.locator('#f_del').click({ timeout: 3000 });
  await expect.poll(() => page.evaluate(() => DB.stations.length)).toBe(1);
  expect(errors).toEqual([]);
});

test('開機／待機／關機統計卡可以點，點了只看那種狀態，再點一次回到全部', async ({ page }) => {
  const errors = await bootWith(page, [NEAR, { ...NEAR, id: 'n2', name: '待機站', status: 'standby' }]);
  await toEq(page);
  await expect(page.locator('.st')).toHaveCount(2);
  await page.locator('.stat button[data-v="standby"]').click();
  await expect(page.locator('.st')).toHaveCount(1);
  await expect(page.locator('.st')).toContainText('待機站');
  await page.locator('.stat button[data-v="standby"]').click();
  await expect(page.locator('.st')).toHaveCount(2);
  expect(errors).toEqual([]);
});

test('「建議處置」只列出有建議動作的站（該待機或可復機）', async ({ page }) => {
  // n1 在颱風中心旁（該待機）；far 在很遠的地方、開機中（沒有建議）
  const errors = await bootWith(page, [NEAR, { ...NEAR, id: 'far', name: '遠方站', lat: 26.5, lng: 119.5 }]);
  await toEq(page);
  const chip = page.locator('.stat button[data-v="advice"]');
  await expect(chip).toContainText('1');
  await expect(chip).toContainText('建議處置');
  await chip.click();
  await expect(page.locator('.st')).toHaveCount(1);
  await expect(page.locator('.st')).toContainText(NEAR.name);
  expect(errors).toEqual([]);
});

/* ── 回報排序與括號、停班停課用詞、官方連結、關機改成待機 ── */
test('回報：時間早的排前面；日期括號不會套兩層', async ({ page }) => {
  const t0 = new Date(); t0.setHours(9, 0, 0, 0);
  const sts = [
    { ...NEAR, id: 'a', name: '北邊晚到站', lat: 25.1, status: 'standby', at: t0.getTime() + 2 * 3600000 },
    { ...NEAR, id: 'b', name: '南邊早到站', lat: 22.1, status: 'standby', at: t0.getTime() },
  ];
  const errors = await bootWith(page, sts);
  await page.locator('.nav button[data-p="rp"]').click();
  const out = await page.locator('#out').inputValue();
  expect(out.indexOf('南邊早到站')).toBeLessThan(out.indexOf('北邊晚到站'));
  expect(out).not.toMatch(/\)）/);
  expect(out).toMatch(/南邊早到站於\d+月\d+日\d+時實施待機。/);   // 日期在前、時間在後
  expect(errors).toEqual([]);
});

test('停班停課寫「全國」、一縣市一列（官方實心、推估虛線框），不再出現「推估達基準」', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await page.locator('.nav button[data-p="dg"]').click();
  await expect(page.locator('#tyDgpa')).toContainText('停班停課　全國');
  await expect(page.locator('#tyDgpa')).not.toContainText('全臺');
  await expect(page.locator('#tyDgpa')).not.toContainText('達基準');
  await expect(page.locator('#tyDgpa .cty')).toHaveCount(22);
  await expect(page.locator('#tyDgpa .cty-h')).toHaveText(/縣市\s*暴風圈侵襲機率\s*推估\s*官方/);
  // 一列只有一行高（以前每列三四行,二十二個縣市捲不完）
  const h = await page.locator('#tyDgpa .cty').first().evaluate(el => el.getBoundingClientRect().height);
  expect(h).toBeLessThan(60);
  expect(errors).toEqual([]);
});

test('有官方警報時附上氣象署官方頁面連結', async ({ page }) => {
  const eff = new Date(Date.now() - 3600000).toISOString(), exp = new Date(Date.now() + 2 * 3600000).toISOString();
  const warn = { info: [{ headline: '海上颱風警報', effective: eff, expires: exp, web: 'https://www.cwa.gov.tw/V8/C/P/Warning/FIFOWS.html',
    description: { 'typhoon-info': [{ section: [{ title: '警報報數', value: '2' }, { title: '颱風編號', value: '29' }] }] } }] };
  const errors = await bootWith(page, [NEAR], { ty: typhoonFile({ warn }) });
  await page.locator('.nav button[data-p="ty"]').click();
  // 使用者指定的官方颱風警報頁；警報單 JSON 的 web（一般警特報頁）不是颱風警報單，不能出現
  await expect(page.locator('.tyh a[href="https://www.cwa.gov.tw/V8/C/P/Typhoon/TY_WARN.html"]').first()).toBeVisible();
  await expect(page.locator('#p-ty a[href*="FIFOWS"]')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('關機視窗可直接改成待機；篩到沒有站時有說明與「顯示全部」', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await toEq(page);
  await page.locator('.stat button[data-v="off"]').click();
  await expect(page.locator('#stlist')).toContainText('目前沒有「關機」的站臺');
  await page.locator('#stlist button[data-v="all"]').click();
  await expect(page.locator('.st')).toHaveCount(1);
  await page.locator('.st[data-id="n1"] button.k-off').click();
  await page.locator('#off_sb').click();
  await expect(page.locator('#at_ok')).toBeVisible();
  await page.locator('#at_ok').click();
  await expect(page.locator('.st[data-id="n1"] .st-tag')).toHaveText('待機');
  expect(errors).toEqual([]);
});

/* ── 加報 5-1、地圖分段與全螢幕拉桿 ── */
test('加報「5-1」發布陸上警報：認得出報數，前一報（第 5 報）有收到就是確定時間', async ({ page }) => {
  const W2 = (no, sub, h, hl) => ({ ty: '29', name: '測試颱風', no, sub, kind: '', headline: hl,
    effective: new Date(Date.now() - h * 3600000).toISOString() });
  const ty = typhoonFile({ warnLog: [W2(1, 0, 10, '海上颱風警報'), W2(5, 0, 2, '海上颱風警報'), W2(5, 1, 1, '海上陸上颱風警報')] });
  const errors = await bootWith(page, [NEAR], { ty });
  await page.locator('.nav button[data-p="ty"]').click();
  await expect(page.locator('.wtime')).toContainText('第 5-1 報');
  await expect(page.locator('.wtime')).not.toContainText('前已發布');
  const r = await page.evaluate(() => parseBulletins({ info: [{ headline: '海上陸上颱風警報', effective: '2026-10-22T12:15:00+08:00',
    description: { 'typhoon-info': [{ section: [{ title: '警報報數', value: '5-1' }] }] } }] })[0]);
  expect([r.no, r.sub]).toEqual([5, 1]);
  expect(errors).toEqual([]);
});

test('路徑依警報範圍分段上色；全螢幕時播放列跟著放大，拖動會更新讀數', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="map"]').click();
  await expect(page.locator('#tyTl')).toContainText('進入海警範圍');
  const ph = await page.evaluate(() => SERIES.map(s => phaseAt(s.t)));
  expect(new Set(ph).size).toBeGreaterThan(1);               // 不止一段顏色
  await page.locator('#mapFull').click();
  await expect(page.locator('#mapWrap')).toHaveClass(/full/);
  const r = page.locator('#tlR');
  await expect(r).toBeVisible();                              // 播放列在 #mapWrap 裡,全螢幕時也看得到
  await r.evaluate(el => { el.value = '12'; el.dispatchEvent(new Event('input', { bubbles: true })); });
  expect(await page.evaluate(() => TCUR)).toBe(12);
  await expect(page.locator('#tlNow')).toContainText('+12h');
  await expect(page.locator('#tlOut')).toContainText('+12h');
  expect(errors).toEqual([]);
});

/* ── 應變會議、停班停課公告時機與比對 ── */
test('應變：依推估進入海警範圍往前推，設定改提前時數會跟著變；開設與撤除排進時間軸、標「建議」', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await page.locator('.nav button[data-p="ty"]').click();
  await expect(page.locator('#tyEvt')).toContainText('應變開設');
  await expect(page.locator('#tyEvt')).toContainText('應變撤除');
  await expect(page.locator('#tyEvt .esrc.adv').first()).toHaveText('建議');
  const a = await page.evaluate(() => meetPlan());
  expect(a.affect - a.open).toBe(3 * 3600000);
  await page.evaluate(() => { DB.cfg.meetLead = 6; refreshAll(); });
  const b = await page.evaluate(() => meetPlan());
  expect(b.affect - b.open).toBe(6 * 3600000);
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="brief"]').click();
  await expect(page.locator('#tyBrief')).toContainText('應變開設與撤除（本機建議，以縣市災害應變中心通知為準）');
  expect(errors).toEqual([]);
});

test('停班停課公告時機照第 10 條：上午達標→前晚 19–22 時；下午達標→當日 10:30 前', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  const r = await page.evaluate(() => {
    const d = new Date(); d.setDate(d.getDate() + 2);
    const am = new Date(d); am.setHours(9, 0, 0, 0);
    const pm = new Date(d); pm.setHours(15, 0, 0, 0);
    return { am: announceHint(am.getTime()), pm: announceHint(pm.getTime()) };
  });
  expect(r.am).toContain('晚間 19–22 時');
  expect(r.pm).toContain('10:30 前');
  expect(errors).toEqual([]);
});

test('推估與官方比對：分出一致、多報、漏報；明天的公告看「明天」那幾行', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await page.locator('.nav button[data-p="ty"]').click();
  const r = await page.evaluate(() => {
    const t = new Date().setHours(0, 0, 0, 0), k = dayKey(t);
    const o = { lines: [{ txt: '今天下午停止上班、停止上課。', stop: true, scope: 'all' },
                        { txt: '明天照常上班、照常上課。', stop: false, scope: 'all' }] };
    const res = { today: officialFor(o, t, t), tmr: officialFor(o, t + 86400000, t),
                  none: officialFor({ lines: [{ txt: '今天照常上班上課。', stop: false, scope: 'all' }] }, t + 86400000, t) };
    DB.dgpaCmp = { [k]: { 甲縣: { e: 1, o: 'full' }, 乙縣: { e: 1, o: 'none' }, 丙縣: { e: 0, o: 'part' }, 丁縣: { e: 1 } } };
    return res;
  });
  expect(r).toEqual({ today: 'full', tmr: 'none', none: null });
  await page.locator('.nav button[data-p="dg"]').click();
  await page.evaluate(() => renderDg());
  const card = page.locator('#dgCmp');
  await expect(card).toContainText('推估與官方比對');
  await expect(card).toContainText('一致 1');
  await expect(card).toContainText('推估多報 1');
  await expect(card).toContainText('推估漏報 1');
  // 不數「待公告」幾個：半夜跑的時候推估也會算到花蓮、臺東這些真縣市，數量跟著時段變
  await expect(card).toContainText(/推估停、官方尚未公告：丁縣/);
  expect(errors).toEqual([]);
});

test('路徑頁時間軸「現在」的站臺級數跟站臺頁一樣取最高（含實測）', async ({ page }) => {
  const errors = await bootWith(page, [{ ...NEAR, lat: 23.0, lng: 121.5 }]);
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="map"]').click();
  const r = await page.evaluate(() => {
    const st = DB.stations[0];
    OBS = [{ name: '旁邊測站', pos: { lat: st.lat + 0.01, lng: st.lng }, wind: 25, gust: 30 }];   // 11 級
    TCUR = 0;
    const html = timelineOut();
    return { html, card: windRead(st, stationImpact(SERIES, st, DB.cfg)).bf };
  });
  expect(r.card).toBe(11);
  expect(r.html).toMatch(/>11<\/span>\s*<span>中心旁測試站/);
  expect(errors).toEqual([]);
});

test('停班停課在底部導覽回報右邊；長截圖產生 PNG（不支援分享時改下載）', async ({ page }) => {
  await page.addInitScript(() => {
    // 測試擋外連，cdnjs 拿不到：換成一個會畫出同尺寸 canvas 的替身
    window.html2canvas = async (el, o) => { const c = document.createElement('canvas');
      const r = el.getBoundingClientRect(); c.width = Math.round(r.width * o.scale); c.height = Math.round(r.height * o.scale);
      window.__shotScale = o.scale; return c; };
  });
  const errors = await bootWith(page, [NEAR]);
  const labels = await page.locator('.nav button span').allTextContents();
  expect(labels.indexOf('停班課')).toBe(labels.indexOf('回報') + 1);
  await page.locator('.nav button[data-p="dg"]').click();
  await expect(page.locator('#p-dg #tyDgpa')).toContainText('停班停課　全國');
  await page.evaluate(() => { Object.defineProperty(navigator, 'canShare', { value: undefined, configurable: true }); });
  const dl = page.waitForEvent('download');
  await page.locator('#dgShot').click();
  const f = await dl;
  expect(f.suggestedFilename()).toMatch(/^galepost-dgpa-\d{4}-\d{4}\.png$/);
  expect(await page.evaluate(() => window.__shotScale)).toBeGreaterThan(0);
  await expect(page.locator('#dgShot')).toHaveText('長截圖（存成圖片）');
  expect(errors).toEqual([]);
});

/* ── 回報用詞、播報天氣、長截圖範圍 ── */
test('回報：只有待機寫「實施待機」；沒有項次的區塊不出現；新句型匯入也認得', async ({ page }) => {
  const errors = await bootWith(page, [{ ...NEAR, status: 'standby' }]);
  await page.locator('.nav button[data-p="rp"]').click();
  const out = await page.locator('#out').inputValue();
  expect(out).toContain('實施待機情形如下');
  expect(out).not.toContain('關（待）機');
  expect(out).not.toContain('無。');
  expect(out).not.toMatch(/港區雷達站0處/);
  // 正式回報的句型（日期在前）也讀得出來；名單裡沒有的站標「new」等人確認
  const r = await page.evaluate(() => readGroup('一、岸際雷達1處：\n1.甲雷達站於10月9日1640時完成關機。').items
    .map(x => ({ cand: x.cand, why: x.why, status: x.status, m: new Date(x.at).getMonth() + 1, d: new Date(x.at).getDate(),
                 h: new Date(x.at).getHours(), mi: new Date(x.at).getMinutes() })));
  expect(r).toEqual([{ cand: '甲雷達站', why: ['new'], status: 'off', m: 10, d: 9, h: 16, mi: 40 }]);
  expect(errors).toEqual([]);
});

test('播報：天氣逐站用一、二編號寫雨量與分類；不再有預報有雨、停班停課、站臺衝擊', async ({ page }) => {
  const errors = await bootWith(page, [NEAR, { ...NEAR, id: 'n2', name: '第二站', lat: 20.2 }]);
  await page.locator('.nav button[data-p="ty"]').click();
  const txt = await page.evaluate(() => {
    const [a, b] = DB.stations;
    RAIN = [{ name: '甲測站', pos: { lat: a.lat + 0.01, lng: a.lng }, now: 0.5, h1: 1.5, h24: 12 },
            { name: '乙測站', pos: { lat: b.lat + 0.01, lng: b.lng }, now: 10, h1: 45, h24: 90 }];
    return briefText();
  });
  expect(txt).toMatch(/一、中心旁測試站：時雨量 1\.5 毫米、24 小時累積 12 毫米（小雨）/);
  expect(txt).toMatch(/二、第二站：時雨量 45 毫米、24 小時累積 90 毫米（大雨）/);
  expect(txt).not.toContain('預報有雨');
  expect(txt).not.toContain('停班停課');
  expect(txt).not.toContain('站臺衝擊');
  expect(errors).toEqual([]);
});

test('長截圖範圍不含「推估與官方比對」', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await page.locator('.nav button[data-p="dg"]').click();
  await page.evaluate(() => { DB.dgpaCmp = { [dayKey(Date.now())]: { 甲縣: { e: 1, o: 'none' } } }; renderDg(); });
  await expect(page.locator('#dgCmp')).toContainText('推估與官方比對');
  await expect(page.locator('#dgCap')).not.toContainText('推估與官方比對');
  expect(errors).toEqual([]);
});

/* ── 戰情圖（參考「颱風戰情室」）：離線底圖、潛勢錐、暴風圈、圖例、播放、總覽名牌、時間軸 ── */
// 氣象署現在的格式：預報點同時給七級、十級風半徑與 70% 機率半徑；分析點有十級風半徑
function typhoonFileFull(opt = {}) {
  const f = typhoonFile(opt);
  const tc = f.raw['W-C0034-005'].tropicalCyclones.TropicalCyclone;
  tc.AnalysisData.Fix[0].Circle25ms = { Radius: '100' };
  tc.AnalysisData.Fix.unshift({ DateTime: iso(Date.now() - 7 * 3600000), CoordinateLatitude: '19.8',
    CoordinateLongitude: '125.8', MaxWindSpeed: '30' });
  tc.ForecastData.Fix = [
    { Tau: '24', CoordinateLatitude: '22.4', CoordinateLongitude: '122.3', MaxWindSpeed: '38',
      Circle15ms: { Radius: '240' }, Circle25ms: { Radius: '90' }, Radius70PercentProbability: '60' },
    { Tau: '48', CoordinateLatitude: '24.6', CoordinateLongitude: '120.6', MaxWindSpeed: '30',
      Circle15ms: { Radius: '200' }, Radius70PercentProbability: '130',
      StateTransfer: [{ value: '變性為溫帶氣旋', lang: 'zh-hant' }, { value: 'BECOMING EXTRATROPICAL LOW', lang: 'en-us' }] },
  ];
  return f;
}
const goMap = async page => {
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="map"]').click();
};

test('預報點的 70% 機率半徑不再被七級風半徑蓋掉；七級／十級與 70% 分欄列', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull() });
  await page.locator('.nav button[data-p="ty"]').click();
  await page.waitForFunction(() => DB.ty.data && DB.ty.data.track.length === 2);
  const r = await page.evaluate(() => DB.ty.data.track.map(f => [f.tau, f.r15, f.r25, f.r70, f.st]));
  // 以前 r70 會是 240／200（七級風半徑）
  expect(r).toEqual([[24, 240, 90, 60, ''], [48, 200, 0, 130, '變性為溫帶氣旋']]);
  // 逐時內插用官方的七級半徑：24 小時那點就是 240,不是照強度縮放的推估值
  expect(await page.evaluate(() => [SERIES[24].r15, SERIES[24].r25, SERIES[0].r25])).toEqual([240, 90, 100]);
  await goMap(page);
  const row = page.locator('#tyFc tr').nth(1);
  await expect(row.locator('td').nth(2)).toHaveText('240／90');
  await expect(row.locator('td').nth(3)).toHaveText('60');
  await expect(page.locator('#tyFc')).toContainText('變性為溫帶氣旋');
  expect(errors).toEqual([]);
});

test('戰情圖：預設離線向量底圖、不抓圖磚；有潛勢錐、七級十級圈與圖例', async ({ page }) => {
  const tiles = [];
  page.on('request', q => { if (/arcgisonline|openstreetmap|cartocdn/.test(q.url())) tiles.push(q.url()); });
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull() });
  await goMap(page);
  await expect(page.locator('#map.hud')).toBeVisible();
  // 海岸線（assets/coast.json）畫進陸地圖層：鄰近陸地一塊、臺灣兩塊（光暈＋本體）
  await page.waitForFunction(() => document.querySelectorAll('.leaflet-land-pane path').length >= 3);
  expect(tiles).toEqual([]);
  const lg = page.locator('.map-lg');
  for (const t of ['已發生路徑', '70% 機率範圍', '七級風暴風圈', '十級風暴風圈', '風場流線']) await expect(lg).toContainText(t);
  const r = await page.evaluate(() => {
    const ls = []; MLAYER.eachLayer(l => ls.push(l));
    return {
      cone: ls.some(l => l.options && l.options.fillRule === 'nonzero'),
      r7: ls.some(l => l instanceof L.Circle && l.getRadius() === 220000),
      r10: ls.some(l => l instanceof L.Circle && l.getRadius() === 100000),
      grid: document.querySelectorAll('.grid-cv').length,
    };
  });
  expect(r).toEqual({ cone: true, r7: true, r10: true, grid: 2 });
  expect(errors).toEqual([]);
});

test('舊裝置存過「衛星影像」：更新後先換成戰情圖一次，之後的選擇照常記住', async ({ page }) => {
  await page.addInitScript(() => {
    if (window.top !== window || sessionStorage.getItem('seeded')) return;
    sessionStorage.setItem('seeded', '1');
    localStorage.setItem('galepost.ui.basemap', 'sat');
  });
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull() });
  await goMap(page);
  await expect(page.locator('#map.hud')).toBeVisible();
  await page.evaluate(() => localStorage.setItem('galepost.ui.basemap', 'street'));
  await page.reload();
  await goMap(page);
  await expect(page.locator('#map')).not.toHaveClass(/hud/);            // 遷移只做一次
  expect(await page.evaluate(() => localStorage.getItem('galepost.ui.basemap'))).toBe('street');
  expect(errors).toEqual([]);
});

test('播放鍵往後走、再按就停；拖到 48 小時讀數卡列出暴風圈內縣市（推估）', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull() });
  await goMap(page);
  await page.locator('#tlPlay').click();
  await page.waitForFunction(() => TCUR >= 3);
  await page.locator('#tlPlay').click();
  await expect(page.locator('#tlPlay')).toHaveAttribute('aria-pressed', 'false');
  const t1 = await page.evaluate(() => TCUR);
  await page.waitForTimeout(400);
  expect(await page.evaluate(() => TCUR)).toBe(t1);
  await page.locator('#tlR').evaluate(el => { el.value = '48'; el.dispatchEvent(new Event('input', { bubbles: true })); });
  await expect(page.locator('#tlNow')).toContainText('預報 +48h');
  await expect(page.locator('#tlOut')).toContainText('七級風暴風圈內（推估）');
  await expect(page.locator('#tlOut')).toContainText('苗栗縣');
  expect(errors).toEqual([]);
});

test('總覽名牌：第幾號颱風、英文名、警報只寫最高一級（陸上），陸上與海上警戒分開列', async ({ page }) => {
  const warn = { info: [{ headline: '海上陸上颱風警報', effective: iso(Date.now() - 3600000), expires: iso(Date.now() + 2 * 3600000),
    area: [{ areaDesc: '巴士海峽東部' }, { areaDesc: '臺灣東南部海面' }, { areaDesc: '臺東縣' }, { areaDesc: '屏東縣' }],
    description: { section: [{ title: '命名與位置', value: '中度颱風 測試颱風（國際命名 TESTA）22日15時的中心位置在北緯 20.5 度，東經 124.5 度，即在青石岬的東南方約 400 公里之處。' }],
      'typhoon-info': [{ section: [{ title: '警報報數', value: '6-1' }, { title: '颱風編號', value: '29' }] }] } }] };
  const errors = await bootWith(page, [NEAR], { ty: typhoonFile({ warn }) });
  await page.locator('.nav button[data-p="ty"]').click();
  const hero = page.locator('.tyh');
  await expect(hero).toContainText('第 29 號颱風');
  await expect(hero).toContainText('TESTA');
  // 警報單標題是「海上陸上颱風警報」（兩種都在發）：徽章只寫最高一級,海上的範圍在下面另列
  await expect(hero.locator('.wbadge')).toHaveText(/^\s*陸上颱風警報\s*發布中\s*$/);
  await expect(hero.locator('.tyh-r')).toHaveCount(0);           // 沒有強度表就不掛排名
  await expect(hero).toContainText('第 6-1 報');
  await expect(hero.locator('.wzone').first().locator('i')).toHaveText('陸上');
  await expect(hero.locator('.wzone').first().locator('span')).toHaveText('臺東縣、屏東縣');
  await expect(hero.locator('.wzone.sea span')).toHaveText('巴士海峽東部、臺灣東南部海面');
  // 位置描述引用警報單原文,並標來源
  await expect(page.locator('.lv')).toContainText('青石岬的東南方約 400 公里');
  await expect(page.locator('.lv .src')).toHaveText('警報單');
  await expect(page.locator('.lv .bfg span')).toHaveCount(17);
  expect(errors).toEqual([]);
});

test('名牌的警報：確定沒有才寫「沒有」；警報單沒拿到或是別顆颱風的，就不掛', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFile() });        // info: [] ＝拿到了、目前沒有警報
  await page.locator('.nav button[data-p="ty"]').click();
  await expect(page.locator('.tyh .wbadge')).toHaveText(/目前沒有颱風警報/);
  const r = await page.evaluate(() => {
    const ty = DB.ty.data, keep = DB.ty.warning;
    DB.ty.warning = null;                                     // 沒拿到警報單
    const a = heroCard(ty);
    DB.ty.warning = { active: true, headline: '海上颱風警報', tyNo: '30', areas: ['巴士海峽'], sec: {} };   // 別顆的
    const b = heroCard(ty);
    DB.ty.warning = keep;
    return { a: /wbadge/.test(a), b: /wbadge/.test(b) };
  });
  expect(r).toEqual({ a: false, b: false });
  expect(errors).toEqual([]);
});

test('颱風時間軸：過去（官方）、現在（紅點）、預報與推估分開標', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull() });
  await page.locator('.nav button[data-p="ty"]').click();
  const evt = page.locator('#tyEvt');
  await expect(evt).toContainText('颱風時間軸');
  await expect(evt.locator('li.cur')).toContainText('現在');
  await expect(evt).toContainText('開始定位');
  await expect(evt).toContainText('增強為中度颱風');
  await expect(evt).toContainText('預報減弱為輕度颱風');
  await expect(evt).toContainText('預報變性為溫帶氣旋');
  await expect(evt.locator('.esrc.off').first()).toHaveText('官方');
  await expect(evt.locator('.esrc.fc').first()).toHaveText('預報');
  // 「現在」那格不能被頂欄時鐘的 .now 樣式（不換行）撐破版面
  const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(over).toBeLessThanOrEqual(0);
  expect(errors).toEqual([]);
});

test('落雷圖層畫實測落雷；沒有資料時圖例寫「無資料」，不寫 0 筆', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull(), ltn: lightningWithStrikes() });
  await goMap(page);
  await page.waitForFunction(() => LTNLAYER && LTNLAYER.getLayers().length === 2);
  await expect(page.locator('.map-lg')).toContainText('落雷 2 筆');
  expect(errors).toEqual([]);
});

test('落雷沒有資料：圖例寫「無資料」', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull() });   // 後端還沒設金鑰
  await goMap(page);
  await expect(page.locator('.map-lg')).toContainText('落雷 無資料');
  await expect(page.locator('.map-lg')).not.toContainText('0 筆');
  expect(errors).toEqual([]);
});

test('風場流線（推估）有畫出來；系統要求減少動態時畫靜態的一張', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull() });
  await goMap(page);
  await page.waitForFunction(() => {
    const c = document.querySelector('.leaflet-flow-pane canvas');
    if (!c || !c.width) return false;
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    for (let i = 3; i < d.length; i += 4 * 31) if (d[i] > 0) return true;
    return false;
  }, null, { timeout: 8000 });
  expect(errors).toEqual([]);
});

/* ── 侵襲機率（停班課）、全球地圖、分頁整併 ── */
// 官方侵襲機率（後端把 W-C0034-003 的 KMZ 解析成這個形狀）：北臺灣一塊 100%,外面一圈 60%（挖掉內圈）
const box = (s, w, n, e) => [[s, w], [s, e], [n, e], [n, w]];
function withProb(f) {
  const inner = box(24.5, 120.8, 25.5, 122.2), outer = box(23.0, 119.5, 26.5, 123.0);
  f.raw['W-C0034-003'] = { time: '2026-10-22T14:00+08:00', items: [
    { name: '100%', p: 100, polys: [{ o: inner, h: [] }] },
    { name: '60%', p: 60, polys: [{ o: outer, h: [inner] }] },
  ] };
  return f;
}

test('侵襲機率（官方）：縣市政府所在地落在哪一圈就寫那個區間；不在任何一圈寫「<最低級」', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: withProb(typhoonFileFull()) });
  await page.locator('.nav button[data-p="dg"]').click();
  const row = n => page.locator(`#tyDgpa .cty[data-cty="${n}"] .cp em`);
  await expect(row('臺北市')).toHaveText('100%');
  await expect(row('苗栗縣')).toHaveText('100%');
  await expect(row('臺中市')).toHaveText('60–100%');       // 外圈,不在被挖掉的內圈
  await expect(row('連江縣')).toHaveText('60–100%');
  await expect(row('臺南市')).toHaveText('<60%');          // 有資料、只是低——不是「沒有資料」
  await expect(row('金門縣')).toHaveText('<60%');
  await expect(page.locator('#tyDgpa .dg-src')).toContainText('中央氣象署 10/22(四) 14:00');
  await expect(page.locator('#tyDgpa .stat .v-hi b')).toHaveText('17');
  // 總覽名牌上一格：最高的機率與縣市,點了到停班課頁
  await page.locator('.nav button[data-p="ty"]').click();
  const tile = page.locator('.tyh .tile-btn');
  await expect(tile).toContainText('侵襲機率最高（官方）');
  await expect(tile).toContainText('100%');
  await expect(tile).toContainText('等 8 縣市');
  await tile.click();
  await expect(page.locator('#p-dg')).toBeVisible();
  expect(errors).toEqual([]);
});

test('侵襲機率：官方還沒發布時用本機模擬推估，標「推估」；同一份資料每次算出一樣', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull() });
  await page.locator('.nav button[data-p="dg"]').click();
  await expect(page.locator('#tyDgpa .dg-src')).toContainText('推估（官方未發布）');
  await expect(page.locator('#tyDgpa .cty[data-cty="臺北市"] .cp em')).toHaveText(/^\d{1,3}%$/);
  const same = await page.evaluate(() => {
    const a = JSON.stringify(probEstimate());
    _probEstKey = '';                                       // 清掉快取重算一次
    return a === JSON.stringify(probEstimate());
  });
  expect(same).toBe(true);
  expect(errors).toEqual([]);
});

test('點一個縣市：底部面板列出侵襲機率、官方公告、推估三段', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: withProb(typhoonFileFull()) });
  await page.locator('.nav button[data-p="dg"]').click();
  await page.locator('#tyDgpa .cty[data-cty="臺北市"]').click();
  await expect(page.locator('#shTitle')).toHaveText('臺北市');
  const body = page.locator('#shBody');
  for (const t of ['暴風圈侵襲機率', '官方公告', '推估', '不是放假機率']) await expect(body).toContainText(t);
  await expect(body.locator('.cd-p b')).toHaveText('100%');
  expect(errors).toEqual([]);
});

test('地圖圖層鈕有「侵襲機率（官方）」，預設關閉；打開後畫出等值區色帶', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: withProb(typhoonFileFull()) });
  await goMap(page);
  await expect(page.locator('.leaflet-control-layers')).toContainText('侵襲機率（官方）');
  expect(await page.evaluate(() => MAP.hasLayer(PROBLAYER))).toBe(false);
  await page.evaluate(() => MAP.addLayer(PROBLAYER));
  expect(await page.evaluate(() => PROBLAYER.getLayers().length)).toBe(2);
  await expect(page.locator('.map-lg')).toContainText('侵襲機率');
  expect(await page.evaluate(() => localStorage.getItem('galepost.ui.probmap'))).toBe('1');
  expect(errors).toEqual([]);
});

test('全球地圖：太平洋置中，美洲（西經）的海岸線接到右邊，不再擠到最左邊', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull() });
  await goMap(page);
  await page.waitForFunction(() => document.querySelectorAll('.leaflet-land-pane path').length >= 3);
  const r = await page.evaluate(async () => {
    const j = await (await fetch('assets/coast.json')).json();
    const lng = ringsLL(j.land).flat().map(p => p[1]);
    const gl = ringsLL([[-72, 60, -11, 60, -11, 80]]);      // 跨在接縫（西經 25 度）上,像格陵蘭
    return {
      us: ringsLL([[-100, 40, -90, 40, -90, 45]]).map(x => x[0]),
      tw: ringsLL([[120, 23, 121, 23, 121, 24]]).map(x => x[0]),
      gl: gl.map(x => x[0][1]),
      max: Math.max(...lng), n: j.land.length,
    };
  });
  expect(r.us).toEqual([[40, 260]]);
  expect(r.tw).toEqual([[23, 120]]);
  expect(r.gl).toEqual([-72, 288]);                         // 左右各一份,接縫兩邊都完整
  expect(r.max).toBeGreaterThan(280);                       // 美洲在右邊
  expect(r.n).toBeGreaterThan(200);                         // 全球,不只東亞
  expect(errors).toEqual([]);
});

test('陸上警戒大半個臺灣時改寫「N 縣市（○○以外）」，一行講完', async ({ page }) => {
  const all = ['基隆市', '臺北市', '新北市', '桃園市', '新竹縣', '新竹市', '苗栗縣', '臺中市', '彰化縣', '南投縣', '雲林縣',
    '嘉義市', '嘉義縣', '臺南市', '高雄市', '屏東縣', '宜蘭縣', '花蓮縣', '臺東縣'];
  const warn = { info: [{ headline: '海上陸上颱風警報', effective: iso(Date.now() - 3600000), expires: iso(Date.now() + 2 * 3600000),
    area: [...all, '臺灣北部海面'].map(a => ({ areaDesc: a })),
    description: { 'typhoon-info': [{ section: [{ title: '警報報數', value: '7' }, { title: '颱風編號', value: '29' }] }] } }] };
  const errors = await bootWith(page, [NEAR], { ty: typhoonFile({ warn }) });
  await page.locator('.nav button[data-p="ty"]').click();
  await expect(page.locator('.tyh .wzone').first().locator('span')).toHaveText('19 縣市（澎湖縣、金門縣、連江縣以外）');
  await expect(page.locator('.tyh .wzone.sea span')).toHaveText('臺灣北部海面');
  // 名牌已經有警報徽章,下面的摘要不再講一次「警報」
  await expect(page.locator('#tyBody .gl-grid')).not.toContainText('海上陸上颱風警報');
  expect(errors).toEqual([]);
});

test('颱風時間軸：一天以前的事件收進「更早 N 則」', async ({ page }) => {
  const f = typhoonFileFull();
  f.raw['W-C0034-005'].tropicalCyclones.TropicalCyclone.AnalysisData.Fix.unshift({ DateTime: iso(Date.now() - 30 * 3600000),
    CoordinateLatitude: '18.5', CoordinateLongitude: '128.0', MaxWindSpeed: '15' });
  const errors = await bootWith(page, [NEAR], { ty: f });
  await page.locator('.nav button[data-p="ty"]').click();
  const old = page.locator('#tyEvt details.evt-old');
  await expect(old.locator('summary')).toContainText('更早 1 則');
  await expect(old).toContainText('開始定位');
  await expect(page.locator('#tyEvt > ol.evt li.cur')).toContainText('現在');
  expect(errors).toEqual([]);
});

test('設定分三頁（站臺／資料來源／系統），字級在最上面；記得上次看的分頁', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await page.locator('.nav button[data-p="cfg"]').click();
  await expect(page.locator('#cfgSeg button')).toHaveText(['站臺', '資料來源', '系統']);
  const ts = await page.locator('#tsSel').boundingBox(), seg = await page.locator('#cfgSeg').boundingBox();
  expect(ts.y).toBeLessThan(seg.y);
  await expect(page.locator('#c_bfShut')).toBeVisible();
  await expect(page.locator('#c_key')).toBeHidden();
  await page.locator('#cfgSeg [data-sub="cfg:src"]').click();
  await expect(page.locator('#c_key')).toBeVisible();
  await expect(page.locator('#cwaProxyIn')).toBeVisible();       // CWA 代理併進「中央氣象署」那張卡
  await page.locator('#tsSel').selectOption('l');
  await expect(page.locator('html')).toHaveAttribute('data-ts', 'l');
  await page.reload();
  await page.locator('.nav button[data-p="cfg"]').click();
  await expect(page.locator('#c_key')).toBeVisible();
  await expect(page.locator('#cfgSeg [data-sub="cfg:src"]')).toHaveAttribute('aria-pressed', 'true');
  await page.locator('#cfgSeg [data-sub="cfg:sys"]').click();
  await expect(page.locator('#p-cfg')).toContainText('風哨 v');
  expect(errors).toEqual([]);
});

test('站臺頁三個子頁：管制列上直接「開始管制」，管制中點那一列跳到時序', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await toEq(page);
  await expect(page.locator('#eqSeg button')).toHaveText(['管制', '預判', '時序']);
  const bar = page.locator('#eqBody .ctlbar');
  await expect(bar).toContainText('尚未開始管制');
  await bar.getByRole('button', { name: '開始管制' }).click();
  await expect(page.locator('#eqBody .ctlbar.on')).toContainText('管制中');
  await page.locator('#eqBody .ctlbar.on').click();
  await expect(page.locator('#eqLog')).toBeVisible();
  await expect(page.locator('#eqLog')).toContainText('結束管制');
  // 沒開始管制時,時序頁只講一次「按開始管制後才記」,不再上下各講一遍
  await page.evaluate(() => { DB.ctl = { start: 0, end: 0, ty: '' }; save(); renderStations(); });
  await expect(page.locator('#eqLog')).not.toContainText('狀態變更時序');
  expect(errors).toEqual([]);
});

test('沒有颱風路徑、也沒抓到豪大雨特報：推估欄寫「—」，不寫成「未達」', async ({ page }) => {
  const errors = await boot(page, PLACEHOLDER);              // 沒颱風、前端沒授權碼
  await page.locator('.nav button[data-p="dg"]').click();
  const tags = await page.locator('#tyDgpa .cty .tg.e').allTextContents();
  expect(tags.length).toBe(22);
  expect(new Set(tags)).toEqual(new Set(['—']));
  await expect(page.locator('#tyDgpa .dg-src')).toContainText('目前沒有颱風');
  expect(errors).toEqual([]);
});

/* ── 應變兩段解除（縮編 → 撤除）與撤除檢核 ── */
// 海上（1）→ 海上陸上（2）→ 改回海上（3）：第 3 報就是解除陸上警報,報數相連＝確定時間
const landLifted = () => typhoonFile({ warnLog: [W(1, 'SEA', '海上颱風警報', 20), W(2, 'SEA_LAND', '海上陸上颱風警報', 10),
  W(3, 'SEA', '海上颱風警報', 2)] });

test('官方解除陸上警報：時間軸分開標「解除陸上颱風警報（官方）」與「應變縮編（建議）」，摘要寫建議縮編', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: landLifted() });
  await page.locator('.nav button[data-p="ty"]').click();
  const r = await page.evaluate(() => { const m = meetPlan(), w = warnTimes(DB.ty.data);
    return { phase: m.phase, src: m.shrinkSrc, same: m.shrink === w.landOff.t, exact: w.landOff.exact, no: w.landOff.no }; });
  expect(r).toEqual({ phase: 'shrink', src: 'official', same: true, exact: true, no: '3' });
  const evt = page.locator('#tyEvt');
  await expect(evt.locator('li', { hasText: '解除陸上颱風警報' }).locator('.esrc')).toHaveText('官方');
  await expect(evt.locator('li', { hasText: '應變縮編' }).locator('.esrc')).toHaveText('建議');
  await expect(page.locator('#tyBody')).toContainText('建議縮編');
  expect(errors).toEqual([]);
});

test('推估說已離開海警範圍、官方警報還在：撤除等官方解除，不照推估先建議', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await page.locator('.nav button[data-p="ty"]').click();
  const r = await page.evaluate(() => {
    WARN = Object.assign({}, WARN, { sea: Object.assign({}, WARN.sea, { clear: Date.now() - 3600000 }) });
    DB.ty.warning = { active: true, isLand: false, headline: '海上颱風警報', tyNo: '29', areas: ['巴士海峽'], sec: {} };
    const m = meetPlan();
    return { close: m.close, src: m.closeSrc, brief: briefText().includes('建議撤除：待中央氣象署解除颱風警報') };
  });
  expect(r).toEqual({ close: null, src: 'wait', brief: true });
  expect(errors).toEqual([]);
});

test('撤除檢核：官方兩項自動帶入且鎖住；手動勾選記時間、照時間排成通報；清除只清手動的', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: landLifted(), cfg: { unit: '測試單位' } });
  await page.locator('.nav button[data-p="rp"]').click();
  await page.locator('#rpSeg [data-sub="rp:end"]').click();
  const land = page.locator('[data-chk="landOff"]');
  await expect(land).toHaveClass(/on/);
  await expect(land).toBeDisabled();
  await expect(land.locator('em')).toContainText('官方');
  await expect(page.locator('[data-chk="tyOff"]')).not.toHaveClass(/on/);
  await page.locator('[data-chk="shrinkNote"]').click();
  await page.locator('[data-eago="10"]').click();
  await page.locator('#ec_ok').click();
  await expect(page.locator('[data-chk="shrinkNote"]')).toHaveClass(/on/);
  const txt = await page.locator('#endOut').inputValue();
  expect(txt).toMatch(/^測試單位報告：\n測試颱風應變撤除檢核如下：\n一、\d+月\d+日\d+時中央氣象署解除陸上颱風警報。\n二、\d+月\d+日\d+時接獲縣市災害應變中心縮編通知。\n待辦：/);
  expect(txt).not.toMatch(/待辦：[^\n]*接獲縣市災害應變中心縮編通知/);
  await page.locator('#endCopy').click();
  await expect(page.locator('#toast')).toHaveText('撤除檢核已複製');
  page.once('dialog', d => d.accept());
  await page.locator('#endReset').click();
  await expect(page.locator('[data-chk="shrinkNote"]')).not.toHaveClass(/on/);
  await expect(page.locator('[data-chk="landOff"]')).toHaveClass(/on/);
  expect(errors).toEqual([]);
});

test('撤除時站臺縣市還有豪雨特報：提醒可能轉水災應變；只發海上警報的颱風「解除陸上警報」寫本次無', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFile({ warnLog: [W(1, 'SEA', '海上颱風警報', 9), W(2, 'SEA', '海上颱風警報', 6)] }) });
  await page.locator('.nav button[data-p="rp"]').click();
  await page.locator('#rpSeg [data-sub="rp:end"]').click();
  await expect(page.locator('[data-chk="landOff"]')).toHaveClass(/na/);
  await expect(page.locator('[data-chk="landOff"] em')).toHaveText('本次無');
  const r = await page.evaluate(() => {
    ADV = [{ kind: '豪雨特報', areas: ['臺中市'] }, { kind: '大雨特報', areas: ['苗栗縣'] }];
    renderEnd();
    return meetPlan().rainHold;
  });
  expect(r).toEqual(['臺中市']);                              // 大雨特報不算
  await expect(page.locator('#endBody .msg.w')).toContainText('臺中市仍有豪雨以上特報');
  expect(errors).toEqual([]);
});

/* ── 第 44 輪：強度排名、時序手動修改、貼上比對、停班課後端備援、動態研判、更新時間 ── */
// 颱風強度表（後端每週從 IBTrACS 建）：TESTA 是 960 hPa，同年同名那一筆是暫定資料裡的自己；
// 今年的日本氣象廳還沒出最終分析，用 JTWC 補（來源 U）
const tyHist = y => ({ v: 2, src: 'IBTrACS v04r01，今年的是暫定資料', built: `${y}-10-19T03:17:00+08:00`,
  through: `${y}-10-15 18:00:00`, from: 1951, storms: [
    [1979, 'TIP', 870, 140, 'J'], [2016, 'MERANTI', 890, 120, 'J'], [y, 'AAA', 930, 100, 'U'], [y, 'BBB', 950, 80, 'U'],
    [y, 'TESTA', 955, 75, 'U'], [1958, 'IDA', 960, 0, 'J'], [y - 1, 'DDD', 965, 60, 'J'], [y, 'CCC', 980, 50, 'U']] });

test('強度排名：今年第幾強、1951 年以來第幾強（暫定資料裡的自己不算）；點開列前五名與收錄落後提醒', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFile(), tyhist: tyHist(new Date().getFullYear()) });
  await page.locator('.nav button[data-p="ty"]').click();
  const r = page.locator('.tyh .tyh-r');
  // 960 hPa：今年比它低的只有 AAA、BBB → 第 3；歷史再加 TIP、MERANTI → 第 5
  await expect(r).toHaveAttribute('aria-label', '強度排名：今年第 3 強，1951 年以來第 5 強，巔峰 960 百帕');
  await expect(r).toContainText('1951 年以來');
  await r.click();
  const body = page.locator('#shBody');
  await expect(page.locator('#shTitle')).toHaveText(/強度排名/);
  await expect(body).toContainText('今年第 3 強');
  await expect(body).toContainText('共 4 個');
  await expect(body).toContainText('今年表裡只收到 4 個（氣象署已編到第 29 號）');
  const yr = body.locator('table.rk').first();
  await expect(yr.locator('tr.me td').first()).toHaveText('3');
  await expect(yr.locator('tr.me')).toContainText('35 m/s');          // 自己那列用氣象署的巔峰風速
  await expect(yr.locator('tr', { hasText: 'TESTA' })).toHaveCount(1);  // 不會自己跟自己並列
  await expect(yr.locator('tr', { hasText: 'AAA' })).toContainText('JTWC');
  await expect(yr.locator('tr', { hasText: 'AAA' }).locator('td').last()).toHaveText('—');   // JTWC 是 1 分鐘風，不放同一欄
  await expect(body).toContainText('今年有 3 個日本氣象廳還沒出最終分析');
  const hist = body.locator('table.rk').nth(1);
  await expect(hist.locator('tr')).toHaveCount(6);                       // 表頭＋前五
  // 1958 年的 IDA 跟自己同是 960 hPa：同氣壓自己排前面，跟「第 5 強」對得起來；1977 年前沒有風速寫「—」
  await expect(hist.locator('tr.me td').first()).toHaveText('5');
  expect(errors).toEqual([]);
});

test('時序可以手動改：改最新那筆，站臺狀態、時間、濕度跟著改並標「已手動修改」；改舊的只改時序', async ({ page }) => {
  const t1 = Date.now() - 40 * 60000, t2 = Date.now() - 20 * 60000;
  const errors = await bootWith(page, [{ ...NEAR, status: 'on', at: t2 }], { db: {
    ctl: { start: Date.now() - 3600000, end: 0, ty: '' }, since: Date.now() - 3600000,
    log: [{ id: 'a', t: t1, sid: 'n1', name: NEAR.name, from: 'on', to: 'standby', at: t1, hum: null },
          { id: 'b', t: t2, sid: 'n1', name: NEAR.name, from: 'standby', to: 'on', at: t2, hum: null }] } });
  await toEq(page);
  await page.locator('#eqSeg [data-sub="eq:log"]').click();
  await expect(page.locator('.ev')).toHaveCount(2);
  // 最新那筆（完成開機）改成關機、濕度 88、時間改晚 5 分鐘
  await page.locator('[data-evedit="b"]').click();
  await expect(page.locator('#shBody')).toContainText('最新的一筆');
  await page.locator('#le_to').selectOption('off');
  await page.locator('#le_hum').fill('88');
  await page.locator('#le_at').fill(await page.evaluate(t => toInput(t), t2 + 5 * 60000));
  await page.locator('#le_ok').click();
  await expect(page.locator('#toast')).toContainText('已修改');
  let s = await page.evaluate(() => ({ st: DB.stations[0], b: DB.log.find(e => e.id === 'b') }));
  expect([s.st.status, s.st.hum, s.b.to, s.b.hum, !!s.b.edited]).toEqual(['off', 88, 'off', 88, true]);
  expect(Math.abs(s.st.at - (t2 + 5 * 60000))).toBeLessThan(60000);
  expect(s.b.at).toBe(s.st.at);
  await expect(page.locator('[data-evedit="b"]')).toContainText('已手動修改');
  await expect(page.locator('#out')).toHaveValue(/中心旁測試站於.*完成關機/);
  // 舊的那筆（實施待機）改濕度：站臺現況不動
  await page.locator('[data-evedit="a"]').click();
  await expect(page.locator('#shBody')).toContainText('只改時序');
  await page.locator('#le_hum').fill('70');
  await page.locator('#le_ok').click();
  s = await page.evaluate(() => ({ st: DB.stations[0], a: DB.log.find(e => e.id === 'a') }));
  expect([s.st.status, s.st.hum, s.a.hum]).toEqual(['off', 88, 70]);
  expect(errors).toEqual([]);
});

// 群組回報判讀用的站臺：全名、可以只寫地名的、名字很像的兩站
const GSTS = [
  { ...NEAR, id: 'g1', name: '青石岬雷達站', lat: 21.9, lng: 120.85, at: Date.now() - 86400000 },
  { ...NEAR, id: 'g2', name: '松岡站', lat: 25.1, lng: 121.3, at: Date.now() - 86400000 },
  { ...NEAR, id: 'g3', name: '松岡二站', lat: 25.0, lng: 121.3, at: Date.now() - 86400000 },
  { ...NEAR, id: 'g4', name: '白浦港雷達站', status: 'standby', hum: 80, lat: 24.6, lng: 121.9, at: Date.now() - 7200000 },
];
const goPaste = async page => {
  await page.locator('.nav button[data-p="rp"]').click();
  await page.locator('#rpSeg [data-sub="rp:rep"]').click();         // 貼上比對併在回報稿這一頁的下方
};

test('萬用判讀：各種寫法都讀得出站名、動作、時間；閒聊、問句、指示不算回報', async ({ page }) => {
  const errors = await bootWith(page, GSTS);
  const r = await page.evaluate(() => {
    const R = readGroup([
      '青石岬雷達站0830完成關機',                       // 全名＋裸寫時間
      '松岡 08:45 關機',                                 // 只寫地名、時間有冒號
      '報告：松岡二站已於8點30分實施待機，濕度85%',      // 「松岡二站」不能被「松岡」吃掉；濕度
      '青石岬、松岡二站 0715 恢復開機',                  // 一句兩站
      '白浦港由待機恢復開機 0600',                       // 兩個動作取最後一個
      '青石岬０７００關機，松岡0705待機',                // 一句兩組；全形數字
      '青石岬站0640待機',                                // 簡稱後面的「站」一起算，不會多出一個新站「站」
      '松岡站預計0900關機',                              // 預計 → 問
      '松岡站尚未開機',                                  // 否定、沒時間 → 問
      '新設站0830關機',                                  // 名單裡沒有 → 問
      '收到，辛苦了', '記得關機喔', '松岡站關機了嗎？', '請各站0900前完成待機',
    ].join('\n'));
    const nm = id => (DB.stations.find(s => s.id === id) || {}).name || '?' + '';
    const hm = t => t ? `${String(new Date(t).getHours()).padStart(2, '0')}${String(new Date(t).getMinutes()).padStart(2, '0')}` : '-';
    return { items: R.items.map(x => [x.sid ? nm(x.sid) : '?' + x.cand, x.status, hm(x.at), x.hum, x.why.join(',')].join('|')),
      other: R.other };
  });
  expect(r.items).toEqual([
    '青石岬雷達站|off|0830||', '松岡站|off|0845||', '松岡二站|standby|0830|85|',
    '青石岬雷達站|on|0715||', '松岡二站|on|0715||', '白浦港雷達站|on|0600||',
    '青石岬雷達站|off|0700||', '松岡站|standby|0705||', '青石岬雷達站|standby|0640||',
    '松岡站|off|0900||plan', '松岡站|on|-||neg', '?新設站|off|0830||new',
  ]);
  expect(r.other).toEqual(['收到，辛苦了', '記得關機喔', '松岡站關機了嗎？', '請各站0900前完成待機']);
  // 「王小明：…」只算那一行是他說的；下一行沒有發話人、沒站名沒時間，是閒聊
  const s2 = await page.evaluate(() => readGroup('王小明：已完成關機\n已完成待機'));
  expect(s2.items.map(x => [x.sender, x.why.join(',')])).toEqual([['王小明', 'new,notime']]);
  expect(s2.other).toEqual(['已完成待機']);
  expect(errors).toEqual([]);
});

test('事件句（停電案）：發電機的停機、啟動不算站臺狀態；雷達開機後切待機＝待機、紅外線正常運作＝開機；辦公室的不算', async ({ page }) => {
  // 使用者實際貼來、舊版看不懂的三句（站名換掉）
  const errors = await bootWith(page, [
    { ...NEAR, id: 'r1', name: '霞雲嶺雷達站', status: 'off', at: Date.now() - 3 * 3600000 },
    { ...NEAR, id: 'r2', name: '霞雲嶺紅外線站', at: Date.now() - 86400000 }]);
  const r = await page.evaluate(() => {
    const R = readGroup([
      '三、霞雲嶺雷達停電案，於0540時發電機已停機休息滿2小時，0610時駐守人員已啟動發電機完成雷達開機並切換為待機模式。',
      '四、霞雲嶺紅外線停電案，於0532時廳舍無預警停電，發電機自行啟動，紅外線正常運作，賡續管制於1532時實施發電機供電達10小時後停機修整，以上報告。',
      '三、0545時辦公室因台電電壓不穩原因，於0547時啟用發電機運轉供電。',
      '五、甲地雷達停電案，於0600時完成雷達開機。',                 // 名單裡沒有：站名要抓「甲地雷達」，不是整段
    ].join('\n'));
    const nm = id => (DB.stations.find(s => s.id === id) || {}).name;
    const hm = t => `${String(new Date(t).getHours()).padStart(2, '0')}${String(new Date(t).getMinutes()).padStart(2, '0')}`;
    return { items: R.items.map(x => [x.sid ? nm(x.sid) : '?' + x.cand, x.status, hm(x.at), x.why.join(',')].join('|')),
      other: R.other, ev: R.events.map(e => e.text.slice(0, 8)) };
  });
  expect(r.items).toEqual(['霞雲嶺雷達站|standby|0610|', '霞雲嶺紅外線站|on|0532|', '?甲地雷達|on|0600|new']);
  // 辦公室那句不是站臺回報，但它是事件（可以列入回報稿的「事件」段）
  expect(r.other).toEqual([]);
  expect(r.ev).toEqual(['霞雲嶺雷達停電案', '霞雲嶺紅外線停電', '0545時辦公室', '甲地雷達停電案，']);
  // 整行是「一、岸際雷達3處：」才是區塊標題；句子裡有「處理」不能被當成標題跳過
  const h = await page.evaluate(() => [readGroup('一、岸際雷達3處：'), readGroup('三、霞雲嶺雷達停電處理中，0620時完成開機。')]
    .map(R => R.items.length + '/' + R.other.length));
  expect(h).toEqual(['0/0', '1/0']);
  expect(errors).toEqual([]);
});

test('事件段：停電案原文列入回報稿（可勾選、改字），結語前多一段「事件」；短的狀態句不算事件；清除紀錄一起清', async ({ page }) => {
  const errors = await bootWith(page, [
    { ...NEAR, id: 'r1', name: '霞雲嶺雷達站', status: 'off', at: Date.now() - 3 * 3600000 },
    { ...NEAR, id: 'r2', name: '霞雲嶺紅外線站', at: Date.now() - 86400000 }],
    { db: { ctl: { start: Date.now() - 4 * 3600000, end: 0, ty: '' }, since: Date.now() - 4 * 3600000 } });
  await goPaste(page);
  await page.locator('#impText').fill([
    '三、霞雲嶺雷達停電案，於0540時發電機已停機休息滿2小時，0610時駐守人員已啟動發電機完成雷達開機並切換為待機模式。',
    '四、霞雲嶺紅外線停電案，於0532時廳舍無預警停電，發電機自行啟動，紅外線正常運作，以上報告。',
    '三、0545時辦公室因台電電壓不穩原因，於0547時啟用發電機運轉供電。',
    '霞雲嶺雷達0612斷電',
  ].join('\n'));
  await page.locator('#impGo').click();
  const card = page.locator('#impRes .ev-card');
  await expect(card.locator('.ev-row')).toHaveCount(3);                    // 最後那句短的只是狀態
  await expect(card.locator('.ev-row').nth(1)).toContainText('紅外線正常運作。');
  await expect(card.locator('.ev-row').nth(1)).not.toContainText('以上報告');
  // 辦公室那句不要；第一句改字
  await card.locator('.ev-row').nth(2).locator('input').uncheck();
  await card.locator('[data-evtx="0"]').click();
  await page.locator('#ev_tx').fill('霞雲嶺雷達停電，0610時以發電機供電完成開機並實施待機');
  await page.locator('#ev_ok').click();
  await page.locator('#impEv').click();
  await expect(page.locator('#toast')).toHaveText('已列入回報稿 2 句事件');
  await expect(card.locator('.dif-k')).toHaveCount(2);                     // 已列入
  const out = await page.locator('#out').inputValue();
  expect(out).toMatch(/、事件：\n1\.霞雲嶺雷達停電，0610時以發電機供電完成開機並實施待機。\n2\.霞雲嶺紅外線停電案，於0532時廳舍無預警停電，發電機自行啟動，紅外線正常運作。\n.、本單位賡續管制復機情形。$/);
  expect(out).not.toContain('辦公室');
  // 回報稿頁可以刪
  await page.locator('#rpSeg [data-sub="rp:rep"]').click();
  await expect(page.locator('#rpEvents .ev-it')).toHaveCount(2);
  await page.locator('#rpEvents [data-rdel]').first().click();
  await expect(page.locator('#out')).not.toHaveValue(/以發電機供電完成開機/);
  // 清除本次紀錄：事件一起清
  await page.locator('.nav button[data-p="eq"]').click();
  await page.locator('#eqSeg [data-sub="eq:log"]').click();
  page.once('dialog', d => { expect(d.message()).toContain('1 句事件'); d.accept(); });
  await page.locator('[data-clearlog]').click();
  expect(await page.evaluate(() => DB.events.length)).toBe(0);
  expect(errors).toEqual([]);
});

test('貼上比對：看不懂、名單裡沒有的跳出來問；對到既有站並記住寫法，下次自動認；略過的同一句不再問', async ({ page }) => {
  const errors = await bootWith(page, GSTS);
  await goPaste(page);
  const text = ['青石岬0630完成關機', '青石岬站預計0900關機', '丹楓0645關機', '收到'].join('\n');
  await page.locator('#impText').fill(text);
  await page.locator('#impGo').click();
  // 自動跳出：第 1 筆是「預計」
  await expect(page.locator('#shTitle')).toHaveText('要你確認　1 / 2');
  await expect(page.locator('#shBody')).toContainText('像是預計或要求');
  await page.locator('#ak_skip').click();
  // 第 2 筆：名單裡沒有「丹楓」→ 對到松岡站，勾著「記住」
  await expect(page.locator('#shTitle')).toHaveText('要你確認　2 / 2');
  await expect(page.locator('#shBody')).toContainText('名單裡沒有這一站：「丹楓」');
  await expect(page.locator('#ak_sid')).toHaveValue('__new');
  await page.locator('#ak_sid').selectOption('g2');
  await expect(page.locator('#ak_lt')).toHaveText('記住：以後看到「丹楓」就是 松岡站');
  await page.locator('#ak_ok').click();
  await expect(page.locator('#sheet')).not.toHaveClass(/on/);
  const res = page.locator('#impRes');
  await expect(res.locator('.stat > div b')).toHaveText(['2', '0', '2', '2']);      // 判讀成功、要確認、要套用、沒回報
  await expect(res.locator('.ok-it')).toHaveCount(2);
  await expect(res.locator('.ok-it', { hasText: '丹楓0645關機' })).toContainText('你確認過');
  expect(await page.evaluate(() => DB.alias)).toEqual({ '丹楓': 'g2' });
  // 整段再貼一次（多一句）：問過的不再問、略過的還是略過，「丹楓」直接認成松岡站
  await page.locator('#impText').fill(text + '\n丹楓0700恢復開機');
  await page.locator('#impGo').click();
  await expect(page.locator('#sheet')).not.toHaveClass(/on/);
  await expect(res.locator('.ok-it', { hasText: '丹楓0700恢復開機' })).toContainText('松岡站');
  await expect(res).toContainText('沒用到的 2 句');
  // 教過的寫法可以刪
  await page.locator('#impBody summary', { hasText: '認得的寫法' }).click();     // 裡面還有一層「範例」
  await page.locator('[data-unalias="丹楓"]').click();
  expect(await page.evaluate(() => DB.alias)).toEqual({});
  expect(errors).toEqual([]);
});

test('貼上比對：列出判讀成功的資料（附原句、點了可改）；套用後回報稿更新；下一次貼上列出新增、有變、這次沒出現', async ({ page }) => {
  const errors = await bootWith(page, GSTS);
  await goPaste(page);
  // 同一頁：上面是回報稿（目前站臺匯出），下面貼最新訊息；沒有另外的「貼上比對」分頁
  await expect(page.locator('#rpSeg [data-sub="rp:imp"]')).toHaveCount(0);
  await expect(page.locator('[data-sp="rp:rep"] #out')).toBeVisible();
  await expect(page.locator('[data-sp="rp:rep"] #impText')).toBeVisible();
  await page.locator('#impText').fill(['青石岬0630完成關機', '報告：松岡二站已於6點40分實施待機，濕度85%'].join('\n'));
  await page.locator('#impGo').click();
  const res = page.locator('#impRes');
  await expect(res.locator('.ok-it')).toHaveCount(2);
  await expect(res.locator('.ok-it').nth(1)).toContainText('松岡二站');
  await expect(res.locator('.ok-it').nth(1)).toContainText('濕度 85%');
  await expect(res.locator('.ok-it').nth(1).locator('small')).toHaveText('報告：松岡二站已於6點40分實施待機，濕度85%');
  await expect(res).toContainText('這是第一份貼上');
  // 點一筆改：松岡二站其實是關機
  await res.locator('.ok-it').nth(1).click();
  await expect(page.locator('#shTitle')).toHaveText('修改這筆判讀');
  await page.locator('#ak_st').selectOption('off');
  await page.locator('#ak_ok').click();
  await expect(res.locator('.ok-it').nth(1)).toContainText('關機');
  await page.locator('#impApply').click();
  await expect(page.locator('#toast')).toHaveText('已套用 2 項');
  const st = await page.evaluate(() => DB.stations.map(s => [s.name, s.status]));
  expect(st).toEqual([['青石岬雷達站', 'off'], ['松岡站', 'on'], ['松岡二站', 'off'], ['白浦港雷達站', 'standby']]);
  await expect(page.locator('#out')).toHaveValue(/青石岬雷達站於\d+月\d+日0630時完成關機/);   // 上方回報稿跟著更新
  await expect(page.locator('#out')).toHaveValue(/松岡二站於\d+月\d+日0640時完成關機/);
  // 下一次：青石岬改成待機、多了白浦港、松岡二站沒出現
  await page.locator('#impText').fill(['青石岬0700待機', '白浦港0705恢復開機'].join('\n'));
  await page.locator('#impGo').click();
  const cmp = res.locator('details.cmp');
  await expect(cmp.locator('summary')).toContainText('新增 1・有變 1・沒出現 1');
  await expect(cmp.locator('.dif-s', { hasText: '白浦港雷達站' })).toContainText('新增');
  await expect(cmp.locator('.dif-s', { hasText: '青石岬雷達站' })).toContainText('有變');
  await expect(cmp.locator('.dif-s', { hasText: '松岡二站' })).toContainText('這次沒出現');
  await expect(res.locator('.dif', { hasText: '白浦港雷達站' }).locator('.dif-nw')).toHaveText('新');
  expect(errors).toEqual([]);
});

test('貼上 LINE 聊天紀錄：沒寫時間的用訊息時間；沒寫站名的可以記住「某人回報的就是某站」', async ({ page }) => {
  const errors = await bootWith(page, GSTS);
  await goPaste(page);
  // 訊息 06:30 起：凌晨跑的時候今天的 06:30 還沒到（未來的時間會被拿出來問），改用昨天的日期
  const d = await page.evaluate(() => { const t = new Date(); if (t.getHours() < 7) t.setDate(t.getDate() - 1);
    return `${t.getFullYear()}/${t.getMonth() + 1}/${t.getDate()}（五）`; });
  const chat = ['[LINE] 颱風回報群組的聊天記錄', '', d, '06:30\t王小明\t青石岬站完成關機', '06:40\t李大華\t已完成待機',
    '06:41\t李大華\t（濕度88%）', '06:50\t王小明\t收到'].join('\n');
  await page.locator('#impText').fill(chat);
  await page.locator('#impGo').click();
  await expect(page.locator('#shTitle')).toHaveText('要你確認　1 / 1');
  await expect(page.locator('#shBody .ask-q small')).toContainText('李大華');
  await expect(page.locator('#ak_at')).toHaveValue(/T06:40$/);              // 句子沒寫時間：先用訊息時間
  await page.locator('#ak_sid').selectOption('g4');
  await expect(page.locator('#ak_lt')).toHaveText('記住：以後 李大華 沒寫站名的回報就是 白浦港雷達站');
  await page.locator('#ak_ok').click();
  const res = page.locator('#impRes');
  await expect(res.locator('.ok-it', { hasText: '青石岬雷達站' })).toContainText('（訊息時間）');
  // 下一則李大華沒寫站名的訊息自動算白浦港
  await page.locator('#impText').fill(chat + '\n07:10\t李大華\t已恢復開機');
  await page.locator('#impGo').click();
  await expect(page.locator('#sheet')).not.toHaveClass(/on/);
  await expect(res.locator('.ok-it', { hasText: '已恢復開機' })).toContainText('白浦港雷達站');
  expect(errors).toEqual([]);
});

test('沒寫時間的：沿用同一段更早寫過的時間，再沒有就用該站最後紀錄的時間，標「沿用最後時間」不跳出來問', async ({ page }) => {
  const at4 = Date.now() - 7200000;
  const errors = await bootWith(page, GSTS.map(x => x.id === 'g4' ? { ...x, at: at4 } : x));
  await goPaste(page);
  await page.locator('#impText').fill(['青石岬0630完成關機', '青石岬已關機', '白浦港待機中'].join('\n'));
  await page.locator('#impGo').click();
  await expect(page.locator('#sheet')).not.toHaveClass(/on/);               // 不再問「沒寫時間」
  const r = await page.evaluate(() => IMP.ok.map(x => [x.sid, x.atFrom, x.at]));
  const t630 = r[0][2];
  expect(await page.evaluate(t => fmtHM(t), t630)).toBe('0630時');           // 用臺灣時間看（CI 機器是 UTC）
  expect(r[1]).toEqual(['g1', 'last', t630]);                                // 同一段前面寫過 0630
  expect(r[2]).toEqual(['g4', 'last', at4]);                                 // 白浦港最後紀錄的時間
  await expect(page.locator('#impRes .ok-it', { hasText: '白浦港待機中' })).toContainText('（沿用最後時間）');
  expect(errors).toEqual([]);
});

test('分項報告格式：「截止至0845時」＋「（一）正常運作（2）：○○及△△雷達站」，共用尾綴、狀態沒變的不動時間', async ({ page }) => {
  const H = 3600000, now = Date.now();
  const S = (id, name, status, at) => ({ ...NEAR, id, name, status, at });
  const errors = await bootWith(page, [S('a', '松岡雷達站', 'on', now - 24 * H), S('b', '白浦雷達站', 'standby', now - 5 * H),
    S('c', '青石岬雷達站', 'on', now - 24 * H), S('d', '丹楓雷達站', 'on', now - 24 * H), S('e', '翠谷雷達站', 'off', now - 6 * H),
    S('f', '安平港區雷達站', 'on', now - 24 * H), S('g', '松岡紅外線站', 'on', now - 24 * H)]);
  const r = await page.evaluate(() => {
    const D = pasteDiff(['1.x組報告：', '截止至0545時，受薔琵颱風影響，相關設備情形說明如下：', '一、雷達：',
      '（一）正常運作（2）：松岡及白浦雷達站。', '（二）待機（1）：青石岬雷達站。', '（三）關機（3）：丹楓、翠谷雷達站及安平港區雷達站。',
      '二、紅外線：', '松岡紅外線正常運作。', '三、0545時辦公室因台電電壓不穩原因，於0547時啟用發電機運轉供電。',
      '（三）關機（1）：新港雷達站。'].join('\n'));
    const st = id => DB.stations.find(x => x.id === id);
    return { ok: D.ok.map(x => [x.sid, x.status, x.atFrom, x.atFrom === 'asof' ? fmtHM(x.at) : x.at === st(x.sid).at]),
      ask: D.ask.map(x => [x.cand, x.status, x.why.join(',')]), ev: D.events.length };
  });
  expect(r.ok).toEqual([
    ['a', 'on', 'last', true], ['b', 'on', 'asof', '0545時'],                // 狀態沒變：時間不動；變了：用截止時間
    ['c', 'standby', 'asof', '0545時'], ['d', 'off', 'asof', '0545時'],       // 「丹楓」借後面的「雷達站」
    ['e', 'off', 'last', true], ['f', 'off', 'asof', '0545時'], ['g', 'on', 'last', true]]);
  expect(r.ask).toEqual([['新港雷達站', 'off', 'new']]);                     // 名單裡沒有的照樣問
  expect(r.ev).toBe(1);                                                       // 辦公室發電機那句是事件
  expect(errors).toEqual([]);
});

// 人事行政總處那一頁（後端存的是切出來的三塊）；day＝公告頁是哪一天（0 今天、-1 昨天），用臺灣日期
function dgpaFile({ day = 0, fetched = Date.now() - 10 * 60000, html = true, error = '' } = {}) {
  const tw = new Date(Date.now() + 8 * 3600000 + day * 86400000);              // CI 機器是 UTC
  const Y = tw.getUTCFullYear(), M = tw.getUTCMonth() + 1, D = tw.getUTCDate(), p2 = n => String(n).padStart(2, '0');
  // 照官方真表：每一區第一列前面多一格跨列的「北部地區」，縣市不在第一格
  const row = (c, txt, red, region) => `<tr>${region ? `<td rowspan=3><P align=center><FONT>${region}</FONT></P></td>` : ''}`
    + `<td headers="city_Name Some County"><FONT>${c}</FONT></td>`
    + `<td vAlign=center align=left width="70%"><FONT${red ? ' color=#FF0000' : ''}>${txt}</FONT></td></tr>`;
  const snippet = `<div class="Header_YMD">${Y - 1911}年${M}月${D}日 天然災害停止上班及上課情形</div>`
    + `<div class="Content_Updata">更新時間：${Y}/${p2(M)}/${p2(D)} 08:20:16</div><table id="Table">`
    + row('基隆市', '今天停止上班、停止上課。', 1, '北部地區') + row('臺北市', '明天停止上班、停止上課。', 1)
    + row('臺中市', '今天照常上班、照常上課。', 0)
    // 真表常見：「明天未達停止上班及上課標準」含「停止上班」四字，但不是停班
    + row('金門縣', '今天照常上班、照常上課。</FONT><br><FONT color=#000080>明天未達停止上班及上課標準。', 0) + '</table>';
  return { v: 1, fetched: iso(fetched), tried: iso(Date.now()), src: 'https://www.dgpa.gov.tw/typh/daily/nds.html',
    html: html ? snippet : '', error };
}

test('停班停課：即時轉發抓不到改讀後端檔，標「後端」與抓取時間；按更新有回音', async ({ page }) => {
  const fetched = Date.now() - 10 * 60000;
  const errors = await bootWith(page, [NEAR], { dgpa: dgpaFile({ fetched }) });
  await page.locator('.nav button[data-p="dg"]').click();
  const box = page.locator('#tyDgpa');
  await expect(box.locator('.msg.w')).toContainText('即時抓不到（連不上轉發服務），改用後端');
  await expect(box).toContainText('（後端）');
  await expect(box).not.toContainText('Failed to fetch');
  // 更新鈕旁邊是這份資料的抓取時間，不是現在
  await expect(box.locator('.sec .upd')).toHaveAttribute('data-age', String(fetched));
  await expect(box.locator('.sec .upd em')).toHaveText('10 分前');
  await expect(box.locator('.cty[data-cty="基隆市"] .tg.o')).toHaveText('今日停');
  await expect(box.locator('.cty[data-cty="臺北市"] .tg.o')).toHaveText('明日停');
  await expect(box.locator('.cty[data-cty="臺中市"] .tg.o')).toHaveText('照常');
  await expect(box.locator('.cty[data-cty="金門縣"] .tg.o')).toHaveText('照常');
  await expect(box.locator('.stat > div').first().locator('b')).toHaveText('2');
  await box.locator('.sec .act').click();
  await expect(page.locator('#toast')).toContainText('改用後端');
  // 後端也從來沒抓到過：兩邊的原因都講，不交出空表
  await page.route('**/data/dgpa.json', r => r.fulfill({ json: dgpaFile({ html: false, error: 'HTTP 503' }) }));
  await box.locator('.sec .act').click();
  await expect(box).toContainText('後端也抓不到（HTTP 503）');
  await expect(box).toContainText('連不上轉發服務');
  expect(errors).toEqual([]);
});

test('停班停課：後端那份是昨天的公告——「明天停」算今天，「今天停／照常」講的是昨天，今天寫「—」', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { dgpa: dgpaFile({ day: -1, fetched: Date.now() - 11 * 3600000 }) });
  await page.locator('.nav button[data-p="dg"]').click();
  const box = page.locator('#tyDgpa');
  await expect(box.locator('.cty[data-cty="臺北市"] .tg.o')).toHaveText('今日停');
  await expect(box.locator('.cty[data-cty="基隆市"] .tg.o')).toHaveText('—');
  await expect(box.locator('.cty[data-cty="臺中市"] .tg.o')).toHaveText('—');
  // 昨天說「明天未達」＝今天照常
  await expect(box.locator('.cty[data-cty="金門縣"] .tg.o')).toHaveText('照常');
  await expect(box.locator('.stat > div').first().locator('b')).toHaveText('1');
  // 自備 JSON 來源也一樣：「未達停止上課標準」不是停；逐欄看，上課照常不會蓋掉上班的停
  const js = await page.evaluate(() => parseDgpaJson([{ county: '金門縣', work: '今天照常上班', school: '明天未達停止上課標準' },
    { county: '臺北市', work: '今天停止上班', school: '今天照常上課' }]).map(r => r.full));
  expect(js).toEqual([false, true]);
  expect(errors).toEqual([]);
});

test('颱風「更新」旁邊有更新時間，按了有回音；沒有颱風時也記查詢時間', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFile() });
  await page.locator('.nav button[data-p="ty"]').click();
  const bar = page.locator('#tyMsg .tybar');
  await expect(bar.locator('.upd')).toHaveText(/^\d{2}:\d{2}剛剛$/);
  await page.evaluate(() => { _last = 0; });                 // 跳過「60 秒內不能再按」
  await bar.locator('.act').click();
  await expect(page.locator('#toast')).toHaveText(/^已更新：氣象署 \d+\/\d+ \d{2}:\d{2} 分析$/);
  // 颱風消散後再按：時間照樣更新（以前停在上一顆颱風的時間，看起來像按了沒反應）
  await page.route('**/data/typhoon.json', r => r.fulfill({ json: typhoonFile({ cyclone: false }) }));
  await page.evaluate(() => { _last = 0; DB.ty.at = Date.now() - 3 * 3600000; });
  await page.locator('#tyMsg .tybar .act').click();
  await expect(page.locator('#toast')).toHaveText('已更新：目前沒有颱風');
  await expect(page.locator('#tyMsg .tybar .upd')).toHaveText(/^\d{2}:\d{2}剛剛$/);
  expect(errors).toEqual([]);
});

test('播報稿「動態研判」：加速、右偏、轉向路徑、滯留都講得出來；與臺灣的距離標本機推算', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull() });
  await page.locator('.nav button[data-p="ty"]').click();
  await page.waitForFunction(() => DB.ty.data && SERIES);
  const r = await page.evaluate(() => {
    const H = 3600000, t0 = Date.now() - H, keep = [TRACK, EXTRAP, WARN, SERIES];
    // 12→6 小時前往西每小時約 10 公里；6 小時前→現在往北每小時約 25 公里
    const base = { name: 'X', t0, vmax: 40, pos: { lat: 21.35, lng: 124.42 },
      past: [{ t: t0 - 12 * H, lat: 20, lng: 125, vmax: 33 }, { t: t0 - 6 * H, lat: 20, lng: 124.424, vmax: 36 }] };
    TRACK = []; EXTRAP = false; WARN = null; SERIES = null;          // 只看這顆假颱風自己的路徑
    const fast = motionBrief(base).lines;
    // 預報：先北北西、再轉東北加速（典型轉向颱風）
    TRACK = [{ tau: 24, pos: { lat: 23.3, lng: 123.9 }, vmax: 45 }, { tau: 48, pos: { lat: 26.5, lng: 127.2 }, vmax: 38 }];
    const recurve = motionBrief({ ...base, past: [{ t: t0 - 12 * H, lat: 20, lng: 126.2, vmax: 40 }, { t: t0 - 6 * H, lat: 20.6, lng: 125.3, vmax: 40 }] }).lines;
    TRACK = [{ tau: 24, pos: { lat: 21.45, lng: 124.42 }, vmax: 40 }, { tau: 48, pos: { lat: 21.5, lng: 124.45 }, vmax: 40 }];
    const stall = motionBrief({ ...base, mvSpd: 3, mvDir: 'N', past: [] }).lines;
    [TRACK, EXTRAP, WARN, SERIES] = keep;
    return { fast, recurve, stall, brief: briefText() };
  });
  expect(r.fast[0]).toMatch(/^颱風中心以每小時 2\d 公里向北移動；過去 12 小時移速由每小時 1\d 公里加快到 2\d 公里，路徑由西向右偏轉為北。$/);
  expect(r.fast.join('')).toMatch(/近中心風速由每秒 33 公尺增強為 40 公尺/);
  expect(r.recurve.join('')).toContain('屬北轉後往東北移動的轉向路徑');
  expect(r.stall[0]).toContain('幾乎滯留');
  expect(r.stall[1]).toBe('官方預報未來 24 小時幾乎停留在原地；24 至 48 小時仍近乎滯留。');
  expect(r.stall[r.stall.length - 1]).toMatch(/^研判：颱風移動緩慢/);
  // 真的播報稿裡有這一段，而且標明依據
  expect(r.brief).toContain('動態研判（依中央氣象署分析與預報推算）');
  expect(r.brief).toMatch(/\n　颱風中心以每小時 \d+ 公里/);
  expect(r.brief).toMatch(/最接近臺灣，距離約 \d+ 公里[^\n]*（本機推算）。/);
  expect(errors).toEqual([]);
});

/* ── 出門評估、站臺天氣紀錄、後端 data/wx.json（第 46 輪）── */
// 臺灣時間今天 h:20（CI 機器是 UTC）：固定時鐘用，讓結果不看測試在幾點跑
const twToday = h => { const tw = new Date(Date.now() + 8 * 3600000);
  return Date.UTC(tw.getUTCFullYear(), tw.getUTCMonth(), tw.getUTCDate(), h - 8, 20); };
// 基隆附近的站臺；後端雨量與陣風站就放在它旁邊
const KEEL = { id: 'k1', name: '基隆測試站', type: 't1', status: 'on', hum: null, note: '', lat: 25.13, lng: 121.74 };
// scripts/fetch_wx.py 的輸出格式：[站名, 緯, 經, 縣市序號, 10 分鐘, 時雨量, 3 小時, 24 小時]，尾巴是 0 的欄位省略，null＝測站沒給值
function wxFile({ at = Date.now() - 20 * 60000, rain = [], wind = [], adv = null } = {}) {
  const t = new Date(at).toISOString(), counties = ['基隆市', '新北市'];
  return { v: 1, hasKey: true, updated: t, rain: { t, counties, rows: rain }, wind: { t, counties, rows: wind }, adv, errors: {} };
}
// 5 分鐘前：2 公里外對地、8 公里外雲間、40 公里外對地（超出 15 公里不算）。同一個時間，落在同一個整點分組
function ltnNearKeel() {
  const t = new Date(Date.now() - 5 * 60000).toISOString();
  return { hasKey: true, updated: new Date().toISOString(), window: '過去 1 小時', source: 'fileapi KMZ', n: 3, cg: 2, ic: 1,
    strikes: [{ lat: 25.148, lng: 121.74, t, cg: true }, { lat: 25.2, lng: 121.74, t, cg: false }, { lat: 25.49, lng: 121.74, t, cg: true }] };
}

test('出門建議：現在位置——附近對地落雷、豪雨、陣風 8 級 → 現在不要出門；依據標實測與來源站；後端 wx.json 解得對', async ({ page }) => {
  await page.context().grantPermissions(['geolocation']);
  await page.context().setGeolocation({ latitude: 25.13, longitude: 121.74 });
  const errors = await bootWith(page, [NEAR], { ty: PLACEHOLDER, ltn: ltnNearKeel(), wx: wxFile({
    rain: [['基隆', 25.135, 121.74, 0, 1.5, 45, 98, 210.5], ['乾站', 25.0, 121.6, 1], ['壞站', 25.2, 121.9, 1, null, null]],
    wind: [['基隆', 25.135, 121.74, 0, 8.1, 17.5]] }) });
  await page.waitForFunction(() => WX_SRC === 'backend');
  const dec = await page.evaluate(() => ({ dry: RAIN.find(o => o.name === '乾站'), bad: RAIN.find(o => o.name === '壞站'),
    county: RAIN[0].county, gust: OBS[0].gust, fresh: Date.now() - RAIN_AT }));
  // 省略的尾巴是 0（乾的站）；null 是沒給值，不能當 0
  expect([dec.dry.now, dec.dry.h1, dec.dry.h3, dec.dry.h24, dec.dry.county]).toEqual([0, 0, 0, 0, '新北市']);
  expect([dec.bad.now, dec.bad.h1, dec.bad.h3]).toEqual([null, null, 0]);
  expect([dec.county, dec.gust]).toEqual(['基隆市', 17.5]);
  expect(dec.fresh).toBeGreaterThan(15 * 60000);          // 用檔案裡的觀測時間，不是現在
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  await page.locator('#tyWx .seg button', { hasText: '現在位置' }).click();
  const card = page.locator('#tyWx .out-card');
  await expect(card).toHaveClass(/out-red/);
  await expect(card.locator('.out-v b')).toHaveText('現在不要出門');
  await expect(card.locator('.out-h').first()).toContainText('基隆市');
  await expect(card.locator('.out-do dd').first()).toHaveText(/^實測(5 公里內 30 分鐘內有對地落雷 1 次（最近 2\.0 公里）|豪雨：時雨量 45 毫米)/);
  await expect(card.locator('.out-do')).toContainText('山區、河堤、海邊、地下道');
  await expect(card.locator('.out-sub').first()).toContainText('接下來沒有預報資料');
  // 依據收在摺疊裡：三條都是實測，標來源站與距離
  await card.locator('.out-why summary').click();
  const li = card.locator('.out-why .out-rs li');
  await expect(li).toHaveCount(3);
  await expect(card.locator('li.lv3', { hasText: '對地落雷' })).toContainText('5 公里內 30 分鐘內有對地落雷 1 次（最近 2.0 公里）');
  await expect(card.locator('li.lv3', { hasText: '豪雨' })).toContainText('豪雨：時雨量 45 毫米、24 小時 211 毫米（基隆站 1 公里）');
  await expect(card.locator('li.lv2')).toContainText('陣風 8 級（17.5 m/s，基隆站 1 公里）');
  for (const e of await li.locator('em').allTextContents()) expect(e).toBe('實測');
  // 查了什麼、哪些沒有資料都講；不寫「安全」
  await expect(card.locator('.out-why .hint').first()).toContainText('已查：豪大雨與雷雨特報、落雷');
  await expect(card.locator('.out-why .hint').first()).toContainText('沒有資料：停班停課');
  await expect(card).not.toContainText('安全');
  expect(errors).toEqual([]);
});

test('出門建議：今天停班停課只當提醒（外出以必要為限），不擋採買；綠燈要有實測撐著，什麼都沒有是「資料不足」', async ({ page }) => {
  // 固定在今天下午：凌晨 0～5 點「明天」那一段會改講今天，結果跟著時段變
  const fix = twToday(14);
  await page.clock.setFixedTime(fix);
  const errors = await bootWith(page, [KEEL], { ty: PLACEHOLDER, dgpa: dgpaFile({ fetched: fix - 10 * 60000 }),
    wx: wxFile({ at: fix - 20 * 60000, rain: [['基隆', 25.135, 121.74, 0]], wind: [['基隆', 25.135, 121.74, 0, 2, 4]] }) });
  await page.waitForFunction(() => WX_SRC === 'backend' && DGPA_LIVE);
  // 指定站臺也有這張卡；公告比雨量晚回來也要補上
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  await page.locator('#tyWx .seg button', { hasText: '指定站臺' }).click();
  const card = page.locator('#tyWx .out-card');
  await expect(card).toHaveClass(/out-yel/);
  await expect(card.locator('.out-v b')).toHaveText('必要的話可以出門');
  await card.locator('.out-why summary').click();
  await expect(card.locator('li.lv1')).toHaveText('官方基隆市今天停止上班上課——外出以採買、就醫等必要為限');
  // 明天：公告只講今天 → 還沒公布；沒有預報 → 四個時段都寫沒資料，不畫成沒事
  await expect(card.locator('.out-off')).toHaveText('明天的停班停課還沒公布 ↗');
  await expect(card.locator('.out-pd div span')).toHaveText(['沒資料', '沒資料', '沒資料', '沒資料']);
  await expect(card.locator('.out-tm .out-sub')).toHaveText('明天白天的資料不足。');
  const r = await page.evaluate(() => {
    const site = { lat: 25.13, lng: 121.74 }, keep = DGPA_LIVE;
    DGPA_LIVE = null;
    // 同一縣市山區大豪雨、平地豪雨各一張特報：雨只列最重的一條，雷另列
    const keepAdv = ADV;
    ADV = [{ kind: '大雷雨', cat: 'thunder', areas: ['基隆市'] }, { kind: '大豪雨', cat: 'rain', areas: ['基隆市山區'] },
      { kind: '豪雨', cat: 'rain', areas: ['基隆市'] }, { kind: '豪雨', cat: 'rain', areas: ['臺南市'] }];
    const adv = outlook(site).rs.filter(x => x.src === '官方').map(x => `${x.lv}/${x.txt}`);
    ADV = keepAdv;
    const grn = outPlan(site);                       // 雨、風都測得到，都沒事
    const sv = [RAIN, OBS, LTN_AT, WX_SRC];
    RAIN = []; OBS = []; LTN_AT = 0; WX_SRC = '';
    const unk = [outPlan(site).v, outlook(site).no.join('、'), outCard(site)];   // 什麼實測都沒有
    [RAIN, OBS, LTN_AT, WX_SRC] = sv; DGPA_LIVE = keep;
    return { adv, grn: [grn.v, grn.head], unk };
  });
  // 雷雨特報（一兩小時、範圍小）才是「盡量不要」；豪大雨特報整個縣市一兩天，只當注意
  expect(r.adv).toEqual(['2/大雷雨特報（基隆市）', '1/大豪雨特報（基隆市）']);
  expect(r.grn).toEqual(['grn', '現在可以出門']);
  expect(r.unk[0]).toBe('unk');
  expect(r.unk[1]).toBe('停班停課（不知道在哪個縣市）、豪大雨與雷雨特報、落雷、雨量、陣風');
  expect(r.unk[2]).toContain('資料不足，無法判斷');
  expect(r.unk[2]).toContain('路況、坍方、淹水這裡看不到');
  expect(errors).toEqual([]);
});

test('站臺天氣紀錄：同一批落雷重抓不重算；同一場雨合併成一筆留最大值、豪雨標致災性；站臺卡與編輯面板看得到', async ({ page }) => {
  const errors = await bootWith(page, [KEEL], { ty: PLACEHOLDER, ltn: ltnNearKeel(),
    wx: wxFile({ rain: [['基隆', 25.135, 121.74, 0, 1, 45, 98, 120]] }) });
  await toEq(page);
  await page.waitForFunction(() => (DB.wxlog || []).length >= 2);
  const r = await page.evaluate(() => {
    const ltn = () => JSON.stringify(DB.wxlog.filter(e => e.k === 'ltn'));
    const before = ltn();
    recordWxLog(); recordWxLog();                    // 十分鐘後又抓到「過去 1 小時」的同一批
    const same = before === ltn();
    // 十分鐘後同一站雨變大：同一場，不另開一筆
    RAIN = RAIN.map(o => o.name === '基隆' ? { ...o, at: o.at + 10 * 60000, h1: 20, h24: 230 } : o);
    recordWxLog();
    return { same, ltn: DB.wxlog.filter(e => e.k === 'ltn'), rain: DB.wxlog.filter(e => e.k === 'rain') };
  });
  expect(r.same).toBe(true);
  expect(r.ltn).toHaveLength(1);
  expect([r.ltn[0].cg, r.ltn[0].ic, +r.ltn[0].near.toFixed(1)]).toEqual([1, 1, 2]);
  expect(r.rain).toHaveLength(1);
  expect([r.rain[0].lv, r.rain[0].h1, r.rain[0].h24, r.rain[0].g]).toEqual(['豪雨', 45, 230, '基隆']);
  // 有存進本機（save 會延遲一下才寫）
  await page.waitForFunction(() => (JSON.parse(localStorage.getItem('galepost.v2')).wxlog || []).length === 2);
  await page.evaluate(() => renderStations());
  // 沒有待機、關機站：導覽上不掛紅色「0」
  await expect(page.locator('#navDot')).toBeHidden();
  const card = page.locator('.st[data-id="k1"]');
  await expect(card).toContainText('24 小時落雷 對地 1 次、雲間 1 次（最近 2.0 km）');
  await expect(card).toContainText('24 小時豪雨（致災性）');
  await card.locator('[data-edit]').first().click();
  const log = page.locator('details.wxl');
  await expect(log.locator('summary')).toContainText('落雷 1 筆・大雨以上 1 筆');
  await expect(log.locator('.wxl-it b', { hasText: '豪雨' })).toHaveText('豪雨・致災性');
  await expect(log).toContainText('時雨量最大 45、3 小時 98、24 小時 230 毫米（基隆站 1 km）');
  await expect(log).toContainText('沒紀錄不等於沒發生');
  // 超過 30 天的自動清掉
  const left = await page.evaluate(() => { DB.wxlog.forEach(e => { e.t -= 31 * 86400000; if (e.t2) e.t2 -= 31 * 86400000; if (e.last) e.last -= 31 * 86400000; });
    RAIN = []; LTN = []; recordWxLog(); return DB.wxlog.length; });
  expect(left).toBe(0);
  expect(errors).toEqual([]);
});

test('播報稿與天氣總表：沒有雨量資料時寫「無法判斷」，不寫成均無降雨', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await page.waitForFunction(() => DB.ty.data && SERIES);
  const r = await page.evaluate(() => {
    const keep = RAIN;
    RAIN = [];
    const none = briefText();
    RAIN = [{ name: '旁邊', id: '', pos: { lat: 20.61, lng: 124.6 }, at: Date.now(), county: '', now: 0, h1: 0, h3: 0, h24: 0 }];
    const dry = briefText();
    RAIN = keep;
    return { none, dry };
  });
  expect(r.none).toContain('目前沒有雨量資料，無法判斷。');
  expect(r.none).not.toContain('均無降雨');
  expect(r.none).not.toContain('無鄰近雨量站可判斷');
  expect(r.dry).toContain('各站臺鄰近雨量站 24 小時內均無降雨。');
  // 天氣總表也一樣：沒有雨量資料不寫「全部站臺目前都沒有下雨」
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  await expect(page.locator('#tyWx')).toContainText('沒有雨量資料，無法判斷有沒有下雨');
  await expect(page.locator('#tyWx')).not.toContainText('全部站臺目前都沒有下雨');
  expect(errors).toEqual([]);
});

test('出門建議：附近有實測陣風時「現在」以實測為準，颱風推估只排後面、依實測校正；大雨講 24 小時累積', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  await page.waitForFunction(() => DB.ty.data && SERIES && SERIES.length > 1);
  const r = await page.evaluate(([lat, lng]) => {
    const site = { lat, lng }, now = Date.now(), keep = [OBS, RAIN];
    const est = (p, i) => p.S[i].why.filter(x => x.src === '推估').map(x => x.txt);
    OBS = []; const noObs = est(outPlan(site), 0);
    OBS = [{ name: '旁邊', id: '', pos: { ...site }, at: now, county: '', wind: 3, gust: 5 }];
    const p = outPlan(site), withObs = est(p, 0), later = est(p, 2);
    RAIN = [{ name: '旁邊', id: '', pos: { ...site }, at: now, county: '', now: 0, h1: 0.5, h3: 2, h24: 136 }];
    const rain = outlook(site).rs.filter(x => x.src === '實測' && /毫米/.test(x.txt)).map(x => `${x.lv}/${x.txt}`);
    [OBS, RAIN] = keep;
    return { noObs, withObs, later, rain };
  }, [NEAR.lat, NEAR.lng]);
  expect(r.noObs[0]).toMatch(/^颱風陣風約 \d+ 級$/);
  expect(r.withObs).toEqual([]);
  expect(r.later[0]).toMatch(/^颱風陣風約 \d+ 級（依附近實測校正）$/);
  expect(r.rain).toEqual(['1/大雨：24 小時 136 毫米、時雨量 0.5 毫米（旁邊站 0 公里）']);
  expect(errors).toEqual([]);
});

/* 時間固定在 10/23（五）14:20，預報排在絕對時間上：結果不看測試在幾點跑 */
const FIX = Date.parse('2026-10-23T14:20:00+08:00'), HRS = 3600000;
const TW = (d, h) => Date.parse(`2026-10-${d}T${String(h).padStart(2, '0')}:00:00+08:00`);
// 後端預報格式：[距 t 幾小時, 長幾小時, 天氣序號, 降雨機率, 溫度, 平均風級]
function fcFile(segs, name = '基隆市', lat = 25.13, lng = 121.74) {
  const t0 = segs[0][0], W = [...new Set(segs.map(x => x[2]))];
  return { t: new Date(t0).toISOString(), wx: W, towns: [[name, lat, lng,
    segs.map(([a, b, w, pop, bf]) => [(a - t0) / HRS, (b - a) / HRS, W.indexOf(w), pop, 25, bf])]] };
}
const calmLtn = () => ({ hasKey: true, updated: new Date(FIX - 3 * 60000).toISOString(), window: '過去 1 小時', n: 0, cg: 0, ic: 0, strikes: [] });

test('出門建議：照預報排逐時時間軸——幾點前回家、活動範圍、提早採買；明天四個時段、最適合出門的時段', async ({ page }) => {
  await page.clock.setFixedTime(FIX);
  const wx = { ...wxFile({ at: FIX - 20 * 60000, rain: [['基隆', 25.135, 121.74, 0]], wind: [['基隆', 25.135, 121.74, 0, 2, 4]] }),
    fc: fcFile([[TW(23, 12), TW(23, 18), '多雲', 20, 3], [TW(23, 18), TW(24, 0), '短暫陣雨或雷雨', 70, 4],
      [TW(24, 0), TW(24, 6), '多雲', 10, 3], [TW(24, 6), TW(24, 12), '陰', 30, 7],
      [TW(24, 12), TW(24, 18), '晴', 0, 2], [TW(24, 18), TW(25, 0), '短暫陣雨', 60, 3]]) };
  const errors = await bootWith(page, [KEEL], { ty: PLACEHOLDER, ltn: calmLtn(), wx });
  await page.waitForFunction(() => WX_SRC === 'backend' && WX && WX.length === 1);
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  await page.locator('#tyWx .seg button', { hasText: '指定站臺' }).click();
  const card = page.locator('#tyWx .out-card');
  await expect(card).toHaveClass(/out-grn/);
  // 18:00 起有雷 → 17:30 前回到家；離回家 3 小時多 → 5 公里內；雷雨一連 6 小時 → 提醒趁現在採買
  await expect(card.locator('.out-v b')).toHaveText('現在可以出門，17:30 前回到家');
  const dd = t => card.locator('.out-do dt', { hasText: t }).locator('xpath=following-sibling::dd[1]');
  await expect(dd('回家')).toHaveText('17:30 前，18:00 起官方預報有雷（短暫陣雨或雷雨）');
  await expect(dd('範圍')).toHaveText('5 公里內，離回家只有 3.2 小時');
  await expect(dd('提早')).toHaveText('18:00 起到 明天 00:00 風雨較大，要採買、加油趁現在');
  await expect(card.locator('.out-do')).not.toContainText('沒有落雷資料');
  // 時間軸：14:00 到後天 00:00 共 34 小時；有雷的 6 小時與明天上午平均風 7 級（陣風 8 級）的 6 小時是橘色
  await expect(card.locator('.tl-bar i')).toHaveCount(34);
  await expect(card.locator('.tl-bar i.l2')).toHaveCount(12);
  await expect(card.locator('.tl-bar i').first()).toHaveClass('l0');
  // 明天
  const pd = card.locator('.out-pd div');
  await expect(pd.locator('span')).toHaveText(['沒發現危險', '盡量不要', '沒發現危險', '注意']);
  await expect(pd.nth(1).locator('small')).toHaveText('陣風 8 級・雨 30%');
  await expect(pd.nth(3).locator('small')).toHaveText('陣風 4 級・雨 60%');
  await expect(card.locator('.out-tm .out-sub')).toHaveText('明天 12:00–21:00 比較適合出門。');
  // 後端預報也接到原本的天氣預報區（沒填授權碼的同事原本完全沒有）
  await expect(page.locator('#tyWx')).not.toContainText('尚未取得鄉鎮天氣預報');
  expect(errors).toEqual([]);
});

test('出門建議：只因為附近剛打雷不能出門時，講雷停 30 分鐘的時間，不用等到下一個整點；來不及回來的講最早幾點', async ({ page }) => {
  await page.clock.setFixedTime(FIX);
  const t = new Date(FIX - 10 * 60000).toISOString();
  const ltn = { ...calmLtn(), n: 1, cg: 1, strikes: [{ lat: 25.157, lng: 121.74, t, cg: true }] };     // 3 公里、10 分鐘前
  const wx = { ...wxFile({ at: FIX - 20 * 60000, rain: [['基隆', 25.135, 121.74, 0]], wind: [['基隆', 25.135, 121.74, 0, 2, 4]] }),
    fc: fcFile([[TW(23, 12), TW(23, 15), '多雲', 20, 3], [TW(23, 15), TW(23, 18), '陰', 20, 3], [TW(23, 18), TW(24, 6), '短暫陣雨或雷雨', 70, 4],
      [TW(24, 6), TW(25, 0), '多雲', 10, 3]]) };
  const errors = await bootWith(page, [KEEL], { ty: PLACEHOLDER, ltn, wx });
  await page.waitForFunction(() => WX && WX.length === 1 && LTN.length === 1);
  const r = await page.evaluate(() => {
    const site = { lat: 25.13, lng: 121.74 }, p = outPlan(site);
    // 改成 17:45：離 18:00 有雷只剩 15 分鐘，出門來不及回來 → 最早明天 06:00
    const late = (() => { LTN = []; const real = Date.now; Date.now = () => real() + 205 * 60000;
      try { return outPlan(site); } finally { Date.now = real; } })();
    return { v: p.v, head: p.head, dos: p.dos, late: [late.head, late.dos.map(d => d.join('|'))] };
  });
  expect(r.v).toBe('red');
  expect(r.head).toBe('附近剛打雷，14:40 後再出門');
  expect(r.dos.find(d => d[0] === '最早')[1]).toBe('<b>14:40 後</b>可以出門（17:30 前回到家）');
  expect(r.late[0]).toBe('18:00 起轉差，現在出門來不及回來');
  expect(r.late[1].slice(0, 2)).toEqual(['轉差|18:00 起官方預報有雷（短暫陣雨或雷雨）', '最早|<b>明天 06:00 後</b>可以出門']);
  expect(errors).toEqual([]);
});

test('現在位置：App 重新載入（改版自動重載）後不會變回「尚未定位」；允許過定位會背景重定位', async ({ page }) => {
  await page.context().grantPermissions(['geolocation']);
  await page.context().setGeolocation({ latitude: 25.13, longitude: 121.74 });
  const errors = await bootWith(page, [KEEL], { ty: PLACEHOLDER, wx: wxFile({ rain: [['基隆', 25.135, 121.74, 0]], wind: [['基隆', 25.135, 121.74, 0, 2, 4]] }) });
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  await page.locator('#tyWx .seg button', { hasText: '現在位置' }).click();
  await expect(page.locator('#tyWx .out-card')).toBeVisible();
  await page.waitForFunction(() => JSON.parse(localStorage.getItem('galepost.v2')).cfg.wxLoc === '__geo__');   // save 會延遲一下才寫
  // 重新載入，而且定位這次不回應：先用上次存的位置
  await page.context().clearPermissions();
  await page.reload();
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  await expect(page.locator('#tyWx .out-card')).toBeVisible();
  await expect(page.locator('#tyWx')).not.toContainText('尚未定位');
  expect(await page.evaluate(() => GEO && GEO.lat)).toBe(25.13);
  expect(errors).toEqual([]);
});

test('沒開自動更新：App 打開也會抓一次資料，出門建議不會一進來就「資料不足」', async ({ page }) => {
  const errors = await bootWith(page, [KEEL], { ty: PLACEHOLDER, cfg: { auto: false },
    wx: wxFile({ rain: [['基隆', 25.135, 121.74, 0]], wind: [['基隆', 25.135, 121.74, 0, 2, 4]] }) });
  await page.waitForFunction(() => WX_SRC === 'backend');
  expect(await page.evaluate(() => outPlan({ lat: 25.13, lng: 121.74 }).v)).toBe('grn');
  // 沒開自動更新就不排每 10 分鐘的輪詢
  expect(await page.evaluate(() => _autoT)).toBe(null);
  expect(errors).toEqual([]);
});

test('停班停課：官方公告連結一直在（頁面上方、縣市細節、出門建議）；出門建議算不出來只壞那一張卡', async ({ page }) => {
  const errors = await bootWith(page, [KEEL], { ty: PLACEHOLDER, dgpa: dgpaFile(),
    wx: wxFile({ rain: [['基隆', 25.135, 121.74, 0]], wind: [['基隆', 25.135, 121.74, 0, 2, 4]] }) });
  await page.waitForFunction(() => DGPA_LIVE && WX_SRC === 'backend');
  await page.locator('.nav button[data-p="dg"]').click();
  const DG = 'https://www.dgpa.gov.tw/typh/daily/nds.html';
  await expect(page.locator('#tyDgpa a.dg-off')).toHaveAttribute('href', DG);
  await page.locator('#tyDgpa .cty[data-cty="基隆市"]').click();
  await expect(page.locator('#shBody a', { hasText: '官方公告' })).toHaveAttribute('href', DG);
  await page.locator('#shClose').click();
  const r = await page.evaluate(() => {
    const site = { lat: 25.13, lng: 121.74 }, ok = outCard(site);
    const real = outPlan; outPlan = () => { throw new Error('測試用的錯'); };
    try { return [ok, outCard(site)]; } finally { outPlan = real; }
  });
  expect(r[0]).toContain(`href="${DG}"`);
  expect(r[1]).toContain('算不出來（測試用的錯）');
  expect(errors).toEqual([]);
});

test('出門建議：凌晨 0～5 點的「明天」改講今天白天；後端落雷檔 0 筆也算有資料（太舊就標太舊）', async ({ page }) => {
  await page.clock.setFixedTime(Date.parse('2026-10-24T03:20:00+08:00'));
  const ltn = { hasKey: true, updated: '2026-10-24T01:10:00+08:00', window: '過去 1 小時', n: 0, cg: 0, ic: 0, strikes: [] };
  const errors = await bootWith(page, [KEEL], { ty: PLACEHOLDER, ltn,
    wx: wxFile({ at: Date.parse('2026-10-24T03:00:00+08:00'), rain: [['基隆', 25.135, 121.74, 0]], wind: [['基隆', 25.135, 121.74, 0, 2, 4]] }) });
  await page.waitForFunction(() => WX_SRC === 'backend' && LTN_AT > 0);
  const r = await page.evaluate(() => { const p = outPlan({ lat: 25.13, lng: 121.74 });
    return { w: p.nWord, d: new Date(p.dayN).getDate(), note: p.dos.map(d => d.join('|')).join('；'), err: LTN_ERR }; });
  expect([r.w, r.d, r.err]).toEqual(['今天', 24, '']);
  expect(r.note).toContain('落雷資料是 2 小時前的，太舊不能用');
  expect(errors).toEqual([]);
});

test('現在位置：定位被拒絕時按下去也會切過去，寫出 iPhone 要去哪裡開權限，可以改看指定站臺', async ({ page }) => {
  await page.addInitScript(() => { navigator.geolocation.getCurrentPosition = (ok, err) => setTimeout(() => err({ code: 1, message: 'denied' }), 50); });
  const errors = await bootWith(page, [KEEL], { ty: PLACEHOLDER });
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  await page.locator('#tyWx .seg button', { hasText: '現在位置' }).click();
  await expect(page.locator('#tyWx .seg button[aria-pressed="true"]')).toHaveText('現在位置');
  await expect(page.locator('#tyWx .msg.e')).toContainText('定位權限被拒絕。iPhone：設定 › 隱私權與安全性 › 定位服務');
  await page.locator('#tyWx .msg.e button', { hasText: '改看指定站臺' }).click();
  await expect(page.locator('#tyWx .seg button[aria-pressed="true"]')).toHaveText('指定站臺');
  await expect(page.locator('#tyWx .out-card')).toBeVisible();
  expect(errors).toEqual([]);
});

test('分項報告：各單位簡寫（連續 2 個字對得上就算）、「截止至23日2340時」、標題帶數量、標題跟內容同一行', async ({ page }) => {
  const S = (id, name, type) => ({ ...NEAR, id, name, type, at: Date.now() - 86400000 });
  const errors = await bootWith(page, [S('r1', '松岡雷達站', 't1'), S('r2', '丹楓雷達站', 't1'), S('r3', '青石岬雷達站', 't1'),
    S('r4', '白浦港區雷達站', 't2'), S('r5', '柳汀雷達站', 't1'), S('r6', '北嶼雷達站', 't1'), S('r7', '南灣港區雷達站', 't2'),
    S('i1', '松岡紅外線站', 't3'), S('i2', '丹楓紅外線站', 't3')]);
  const r = await page.evaluate(() => {
    const R = readGroup(['截止至23日2340時，受薔琵颱風影響，相關設備情形說明如下：', '一、雷達（10）：',
      '（一）正常運作（2）：松岡、丹楓', '（二）待機（3）：青石、白浦港、柳汀', '（三）關機（1）：北嶼、南灣港',
      '二、紅外線（2）：松岡及丹楓紅外線均正常運作。'].join('\n'));
    const nm = id => (DB.stations.find(s => s.id === id) || {}).name || '';
    const a = R.items[0].asOf ? new Date(R.items[0].asOf) : null;
    return { got: R.items.map(it => `${it.sid ? nm(it.sid) : '?' + it.cand}:${it.status}`), asOf: a && [a.getDate(), a.getHours(), a.getMinutes()],
      // 「港區」是通用字：「南灣港」不能因為「港區」兩個字對到白浦港區；單一個字（甲）對不到就交給人確認
      generic: gFuzzy('港區', 't2'), one: gFuzzy('甲', 't1') };
  });
  expect(r.got).toEqual(['松岡雷達站:on', '丹楓雷達站:on', '青石岬雷達站:standby', '白浦港區雷達站:standby', '柳汀雷達站:standby',
    '北嶼雷達站:off', '南灣港區雷達站:off', '松岡紅外線站:on', '丹楓紅外線站:on']);
  expect(r.asOf).toEqual([23, 23, 40]);
  expect([r.generic, r.one]).toEqual([null, null]);
  expect(errors).toEqual([]);
});

test('Windy 即時影像搬到路徑頁「對焦站臺」右邊的分頁；天氣頁不再有；切走就卸掉 iframe', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull() });
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  await expect(page.locator('#ty-wx #tyWindy')).toHaveCount(0);
  await expect(page.locator('#ty-wx')).not.toContainText('即時影像 Windy');
  await page.locator('#tySeg button[data-ty="map"]').click();
  await expect(page.locator('#mapFocus button')).toHaveText(['全域', '對焦站臺', '即時影像']);
  await page.locator('#mapFocus button[data-mf="wy"]').click();
  await expect(page.locator('#tyWindy')).toContainText('即時影像 Windy');
  await expect(page.locator('#tyWindy iframe')).toHaveCount(1);
  await expect(page.locator('#mapWrap')).toBeHidden();
  await page.locator('#mapFocus button[data-mf="tw"]').click();
  await expect(page.locator('#tyWindy iframe')).toHaveCount(0);
  await expect(page.locator('#mapWrap')).toBeVisible();
  expect(errors).toEqual([]);
});

test('名單裡有沒填座標的站臺：按「現在位置」照樣定位成功（以前判斷縣市無限遞迴，停在「定位中…」）', async ({ page }) => {
  await page.context().grantPermissions(['geolocation']);
  await page.context().setGeolocation({ latitude: 25.13, longitude: 121.74 });
  const errors = await bootWith(page, [KEEL, { ...KEEL, id: 'nx', name: '沒座標站', lat: null, lng: null }], { ty: PLACEHOLDER,
    wx: { ...wxFile({ rain: [['基隆', 25.135, 121.74, 0]], wind: [['基隆', 25.135, 121.74, 0, 2, 4]] }),
      fc: { t: new Date(Math.floor(Date.now() / 3600000) * 3600000).toISOString(), wx: ['多雲'], towns: [['基隆市', 25.15, 121.76, [[0, 48, 0, 10, 25, 3]]]] } } });
  await page.waitForFunction(() => WX && WX.length === 1);
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  await page.locator('#tyWx .seg button', { hasText: '現在位置' }).click();
  await expect(page.locator('#tyWx .out-card')).toBeVisible();
  await expect(page.locator('#tyWx')).not.toContainText('定位中');
  expect(await page.evaluate(() => countyOf({ lat: NaN, lng: 121 }))).toBe('');      // 沒座標不猜，也不當掉
  expect(errors).toEqual([]);
});

test('天氣頁：天氣卡在最上面、沒有「天氣」大字；現在位置分頁在最左；雷電收成一行摺疊，附近有對地落雷才自動打開', async ({ page }) => {
  const errors = await bootWith(page, [{ ...STATION, id: 'n1', name: '中心旁測試站' }], { ty: PLACEHOLDER, ltn: lightningWithStrikes() });
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  const kids = await page.locator('#ty-wx > div').evaluateAll(els => els.map(e => e.id));
  expect(kids).toEqual(['tyWx', 'tyThunder']);
  await expect(page.locator('#tyWx h2')).toHaveCount(0);
  await expect(page.locator('#tyWx .seg button').first()).toHaveText('現在位置');
  const th = page.locator('#tyThunder details.thz');
  await expect(th.locator('> summary')).toContainText('1 站 15 km 內有雲對地落雷');
  await expect(th).toHaveAttribute('open', '');
  // 沒有落雷：收起來，只剩一行
  await page.evaluate(() => { LTN = []; renderTyphoon({}); });
  await expect(page.locator('#tyThunder details.thz')).not.toHaveAttribute('open', '');
  await expect(page.locator('#tyThunder details.thz > summary')).toContainText('沒有落雷');
  expect(errors).toEqual([]);
});

test('現在位置寫出鄉鎮（最近雨量站的縣市鄉鎮，例如臺中市大里區）', async ({ page }) => {
  await page.context().grantPermissions(['geolocation']);
  await page.context().setGeolocation({ latitude: 24.10, longitude: 120.68 });
  const t = new Date(Date.now() - 20 * 60000).toISOString();
  const wx = { v: 1, hasKey: true, updated: t, rain: { t, counties: ['臺中市'], rows: [['大里', 24.099, 120.678, 0]], towns: ['大里區'], tw: [0] },
    wind: { t, counties: ['臺中市'], rows: [['大里', 24.099, 120.678, 0, 2, 4]], towns: ['大里區'], tw: [0] }, adv: null, errors: {} };
  const errors = await bootWith(page, [KEEL], { ty: PLACEHOLDER, wx });
  await page.waitForFunction(() => WX_SRC === 'backend');
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="wx"]').click();
  await page.locator('#tyWx .seg button', { hasText: '現在位置' }).click();
  await expect(page.locator('#tyWx')).toContainText('臺中市大里區');
  expect(await page.evaluate(() => placeOf({ lat: 23.0, lng: 121.9 }))).not.toContain('大里');   // 太遠就不寫鄉鎮
  expect(errors).toEqual([]);
});

test('路徑頁：「各站臺風級」正上方有第二條播放條，跟地圖下那條同步，拖了站臺風級跟著變', async ({ page }) => {
  const errors = await bootWith(page, [NEAR], { ty: typhoonFileFull() });
  await page.locator('.nav button[data-p="ty"]').click();
  await page.locator('#tySeg button[data-ty="map"]').click();
  const r2 = page.locator('#tlR2');
  await expect(r2).toBeVisible();
  // 在 DOM 裡排在各站臺風級前面
  const before = await page.evaluate(() => document.getElementById('tlR2').compareDocumentPosition(document.getElementById('tlSt')) & Node.DOCUMENT_POSITION_FOLLOWING);
  expect(before).toBeTruthy();
  const st0 = await page.locator('#tlSt').innerHTML();
  await r2.evaluate(el => { el.value = '24'; el.dispatchEvent(new Event('input', { bubbles: true })); });
  expect(await page.evaluate(() => TCUR)).toBe(24);
  await expect(page.locator('#tlR')).toHaveValue('24');                       // 地圖下那條跟著動
  await expect(page.locator('#tlNow2')).toContainText('+24h');
  expect(await page.locator('#tlSt').innerHTML()).not.toBe(st0);
  await expect(r2).toBeVisible();                                               // 拉桿本身沒被重畫掉
  await page.locator('#tlPlay2').click();
  await expect(page.locator('#tlPlay')).toHaveText('❚❚ 暫停');
  await page.locator('#tlPlay2').click();
  expect(errors).toEqual([]);
});

/* ── \u5206項報告的時間、單位、請查明（使用者的兩份實際格式；站名是代號，單位用字跳脫——repo 公開） ── */
const R3 = `三\u968a報告
截止至24日1010時，受薔琵颱風影響，相關設備情形說明如下：
一、雷達（10）：
（一）正常運作（3）：Aa、bb、cc
（二）待機（1）：dd
（三）關機（6）：ee、ff、gg、hh、ii、jj

二、紅外線（2）：kk及ll紅外線均正常運作。

三、jj雷達停電案，因自昨（23）日2324時jj雷達站因發電機供電達10小時，故將發電機停機2小時，雷達設備已於1000時完成關機，並由雷達組同仁完成油料補充，以上報告。`;
const RC = `中部\u5206\u7f72報告：
因應「薔琵」颱風，實施預警性關、待機作業，計(7)處：
一、\u5cb8際雷達關機情形如下：
（一）第四\u5de1\u9632\u5340(關機1處)：
1.xx雷達站（10月22日1715時關機）

（二）第五\u5de1\u9632\u5340(關機1處、待機(0)處）：
1.ww雷達站（10月22日1100時開機，由發電機供電）
2.ss雷達站（10月22日2000時關機）
二、彌補雷達站（待機1處）：
1.qq彌補雷達站（10月20日1600時待機）
三、雷達站由發電機供電計3處：
1.tt雷達
2.uu雷達
3.yy雷達
四、本\u5206\u7f72賡續管制雷達復機及運作情形。`;
const UA = '三\u968a', UB = '四\u968a';                 // 預設對照表：UA＝第四…、UB＝第五…
const TW8 = (d, hm) => new Date(`2026-10-${d}T${hm.slice(0, 2)}:${hm.slice(2)}:00+08:00`).getTime();
const ST3 = (prior, unit = '') => {
  const mk = (n, type, status, at) => ({ id: n, name: n + (type === 't3' ? '紅外線站' : '雷達站'), type, status, at, hum: null, note: '', unit });
  const on = TW8(23, '0800');
  if (!prior) return ['Aa', 'bb', 'cc', 'dd', 'ee', 'ff', 'gg', 'hh', 'ii', 'jj'].map(n => mk(n, 't1', 'on', on))
    .concat([mk('kk', 't3', 'on', on), mk('ll', 't3', 'on', on)]);
  return [mk('Aa', 't1', 'on', on), mk('bb', 't1', 'on', on), mk('cc', 't1', 'on', on), mk('dd', 't1', 'standby', TW8(23, '2200')),
    ...['ee', 'ff', 'gg', 'hh', 'ii'].map((n, i) => mk(n, 't1', 'off', TW8(23, '1500') + i * 600000)),
    mk('jj', 't1', 'on', on), mk('kk', 't3', 'on', on), mk('ll', 't3', 'on', on), mk('mm', 't1', 'on', on)];
};
async function pasteReport(page, text) {
  await page.locator('.nav button[data-p="rp"]').click();
  await page.locator('#impText').fill(text);
  await page.locator('#impGo').click();
  if (await page.locator('#ak_ok').isVisible().catch(() => false)) await page.locator('#shClose').click();
  return page.evaluate(() => {
    const f = ts => new Date(ts).toLocaleString('sv-SE', { timeZone: 'Asia/Taipei' }).slice(5, 16);
    const D = IMP;
    return { rows: Object.fromEntries(D.rows.map(r => [r.name, `${r.kind} ${r.status} ${f(r.at)} ${r.it.atFrom}`])),
      ask: D.ask.map(a => `${a.cand} ${a.status} ${f(a.at)} ${a.unit}`), missing: D.missing.map(s => s.name),
      asof: D.asof.map(r => r.name), warns: D.warns, units: D.units, roster: D.roster, ev: D.events.map(e => e.text), other: D.other };
  });
}

test('名冊式報告：截止時間不蓋掉沒變的站；jj 用第三點寫的 1000；名冊裡有、報告沒提到的列進「請查明」', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-10-24T10:30:00+08:00'));
  const errors = await bootWith(page, ST3(true, UA), { ty: PLACEHOLDER });
  const r = await pasteReport(page, R3);
  expect(r.units).toEqual([UA]);
  expect(r.rows['ee雷達站']).toBe('same off 10-23 15:00 last');             // 狀態沒變：不改成 1010
  expect(r.rows['dd雷達站']).toBe('same standby 10-23 22:00 last');
  expect(r.rows['Aa雷達站']).toBe('same on 10-23 08:00 last');
  expect(r.rows['jj雷達站']).toBe('status off 10-24 10:00 text');           // 「雷達設備已於1000時完成關機」勝過截止時間
  expect(r.missing).toEqual(['mm雷達站']);
  expect(r.roster).toBe(true);
  expect(r.warns).toEqual([]);
  expect(r.ev[0]).toContain('jj雷達停電案');
  const chk = page.locator('.chk-card');
  await expect(chk).toContainText('減少');
  await expect(chk).toContainText('mm雷達站');
  await expect(chk).not.toContainText('截止時間');
  expect(errors).toEqual([]);
});

test('名冊式報告：狀態變了又沒寫時間——先找之前報告明寫的時間，都沒有才用截止時間並列進請查明；標題數量對不上也列；名單沒\u5206單位的直接歸過去', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-10-24T10:30:00+08:00'));
  const errors = await bootWith(page, ST3(false), { ty: PLACEHOLDER });
  // 之前貼過一則明寫時間的訊息（沒有套用）
  await pasteReport(page, 'ee雷達站10月23日1500時關機');
  const r = await pasteReport(page, R3.replace('雷達（10）', '雷達（11）').replace('正常運作（3）', '正常運作（4）'));
  expect(r.rows['ee雷達站']).toBe('status off 10-23 15:00 known');
  expect(r.rows['ff雷達站']).toBe('status off 10-24 10:10 asof');
  expect(r.asof).toEqual(['dd雷達站', 'ff雷達站', 'gg雷達站', 'hh雷達站', 'ii雷達站']);
  expect(r.warns).toEqual(['「（一）正常運作（4）」寫 4 處，列了 3 處', '「一、雷達（11）」寫 11 處，底下列了 10 處']);
  const chk = page.locator('.chk-card');
  await expect(chk).toContainText('5 站狀態變了、報告沒寫幾點改的，先用截止時間 10/24 10:10');
  await expect(chk).toContainText('寫 11 處，底下列了 10 處');
  // 使用者：單位不要一站一站填——報告寫了單位，名單上還沒\u5206的直接歸過去，不用按鈕
  await expect(page.locator('#toast')).toHaveText('依報告把 12 站歸到單位');
  expect(await page.evaluate(u => DB.stations.filter(s => s.unit === u).length, UA)).toBe(12);
  await expect(page.locator('#impUnit')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('彙整報告：標題、發電機清單、結語不當成站；段落寫的單位照對照表歸；新站帶時間與單位', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-10-24T10:30:00+08:00'));
  const errors = await bootWith(page, [{ id: 'z', name: 'zz雷達站', type: 't1', status: 'on', unit: UA }], { ty: PLACEHOLDER });
  const r = await pasteReport(page, RC);
  expect(r.units).toEqual([UA, UB]);                                        // 兩個段落各自照對照表歸到 UA、UB
  expect(r.ask).toEqual([`xx雷達站 off 10-22 17:15 ${UA}`, `ww雷達站 on 10-22 11:00 ${UB}`,
    `ss雷達站 off 10-22 20:00 ${UB}`, 'qq彌補雷達站 standby 10-20 16:00 ']);  // 「二、彌補雷達站」不沿用上一段的單位
  expect(r.ev).toEqual(['雷達站由發電機供電計3處：tt雷達、uu雷達、yy雷達。']);
  expect(r.other).toContain('四、本\u5206\u7f72賡續管制雷達復機及運作情形。');
  expect(r.roster).toBe(false);                                             // 只列關、待機：沒提到的不算「減少」
  await expect(page.locator('#impBody .imp-ex')).toContainText('（一）第四\u5de1\u9632\u5340(關機1處)：');   // 兩份範例放進「認得的寫法」
  await expect(page.locator('#impBody .imp-ex')).toContainText('三\u968a報告');
  await expect(page.locator('.chk-card')).not.toContainText('減少');
  // 確認成新站：單位跟著報告
  await page.evaluate(() => { IMP.ask.forEach(a => pasteFix(a.key, { name: a.cand, type: a.type, status: a.status, at: a.at, hum: null })); refreshImp();
    applyPaste(IMP.rows.filter(x => x.kind === 'new')); });
  expect(await page.evaluate(() => Object.fromEntries(DB.stations.map(s => [s.name, s.unit || ''])))).toEqual(
    { zz雷達站: UA, xx雷達站: UA, ww雷達站: UB, ss雷達站: UB, qq彌補雷達站: '' });
  expect(errors).toEqual([]);
});

test('站臺所屬單位：編輯填同轄\u5340的另一種寫法存成代表名；卡片標單位；單位下拉篩選；對照表可改', async ({ page }) => {
  const errors = await bootWith(page, [NEAR, { ...NEAR, id: 'n2', name: '第二站', lat: 30, lng: 130 }]);
  await toEq(page);
  await page.locator('.st[data-id="n1"] [data-edit]').click();
  await page.locator('#f_unit').fill('第四\u5de1\u9632\u5340');
  await page.locator('#f_save').click();
  expect(await page.evaluate(() => DB.stations[0].unit)).toBe(UA);
  await expect(page.locator('.st[data-id="n1"] .unitlb')).toHaveText(UA);
  await expect(page.locator('#uSel option')).toHaveText(['全部單位', '三\u968a／四\u5de1（1）', '未\u5206單位']);
  await page.locator('#uSel').selectOption(UA);
  await expect(page.locator('.st')).toHaveCount(1);
  await page.locator('#uSel').selectOption('-');
  await expect(page.locator('.st[data-id="n2"]')).toBeVisible();
  await expect(page.locator('.st')).toHaveCount(1);
  await page.evaluate(() => { DB.cfg.unitEq = '三\u968a＝四\u5de1＝第3\u5cb8\u5de1\u968a\n四\u968a＝五\u5de1'; });
  expect(await page.evaluate(() => [unitKey('第三\u5cb8\u5de1\u968a'), unitKey('3\u968a'), unitKey('五\u5de1'), unitLabel('三\u968a')]))
    .toEqual([UA, UA, UB, '三\u968a／四\u5de1']);
  expect(errors).toEqual([]);
});

test('沒填單位的站依位置推估：附近只有一個單位、有把握的直接寫進站臺（不再標問號）；編輯欄帶出目前單位；太遠的不推', async ({ page }) => {
  const S = (id, name, lat, lng, unit = '') => ({ ...NEAR, id, name, lat, lng, unit });
  const errors = await bootWith(page, [S('a', '甲站', 24.0, 120.5, UA), S('b', '乙站', 24.1, 120.55), S('c', '丙站', 25.5, 121.9)]);
  await toEq(page);
  expect(await page.evaluate(() => DB.stations.map(s => [s.id, s.unit || '', s.unitSrc || '']))).toEqual(
    [['a', UA, ''], ['b', UA, 'geo'], ['c', '', '']]);
  await expect(page.locator('.st[data-id="b"] .unitlb')).toHaveText(UA);
  await expect(page.locator('.st[data-id="b"] .unitlb')).not.toHaveClass(/guess/);
  await expect(page.locator('.st[data-id="c"] .unitlb')).toHaveCount(0);
  await expect(page.locator('#uSel option')).toHaveText(['全部單位', '三\u968a／四\u5de1（2）', '未\u5206單位']);
  await page.locator('.st[data-id="b"] [data-edit]').click();
  await expect(page.locator('#f_unit')).toHaveValue(UA);
  await expect(page.locator('#sheet')).toContainText('依位置推估（最近已知 甲站 12 km）');
  expect(errors).toEqual([]);
});

test('單位邊界：兩個單位差不多近就不替你決定，標「待確認」；只改別的欄位儲存不會把推估寫進去；報告寫的單位蓋過位置推估', async ({ page }) => {
  const S = (id, name, lat, lng, unit = '') => ({ ...NEAR, id, name, lat, lng, unit });
  // 甲（UA）、丁（UB）相距約 20 km；戊在中間偏甲（9 km 對 11 km）；乙只靠近甲
  const errors = await bootWith(page, [S('a', '甲站', 24.0, 120.5, UA), S('d', '丁站', 24.0, 120.7, UB),
    S('e', '戊站', 24.0, 120.59), S('b', '乙站', 24.0, 120.4)]);
  await toEq(page);
  expect(await page.evaluate(() => DB.stations.map(s => s.unit || ''))).toEqual([UA, UB, '', UA]);
  await expect(page.locator('.st[data-id="e"] .unitlb')).toHaveText(`${UA}／${UB} 待確認`);
  await page.locator('.st[data-id="e"] [data-edit]').click();
  await expect(page.locator('#f_unit')).toHaveValue(UA);
  await expect(page.locator('#sheet')).toContainText('邊界不確定');
  await page.locator('#f_note').fill('只改備註');
  await page.locator('#f_save').click();
  expect(await page.evaluate(() => DB.stations[2].unit || '')).toBe('');      // 沒動單位欄：推估不會被當成確認
  await page.locator('.st[data-id="e"] [data-edit]').click();
  await page.locator('#f_unit').fill(UB);
  await page.locator('#f_save').click();
  expect(await page.evaluate(() => [DB.stations[2].unit, DB.stations[2].unitSrc])).toEqual([UB, 'manual']);
  // 乙是位置推估的 UA；報告寫它屬於 UB → 以報告為準，不列「單位不符」
  expect(await page.evaluate(() => DB.stations[3].unitSrc)).toBe('geo');
  const r = await pasteReport(page, `${UB}報告\n（一）正常運作（1）：乙站`);
  expect(await page.evaluate(() => [DB.stations[3].unit, DB.stations[3].unitSrc])).toEqual([UB, 'report']);
  await expect(page.locator('.chk-card')).not.toContainText('名單上是');       // 不列「單位不符」
  expect(r.units).toEqual([UB]);
  expect(errors).toEqual([]);
});

test('即時告警：山區大雨以上與打雷——戰情頁最上面一行、播報員最上面一張卡、站臺卡標籤，雨與雷分顏色；山區特報不算平地的站', async ({ page }) => {
  const errors = await bootWith(page, [KEEL], { ty: PLACEHOLDER, ltn: ltnNearKeel(),
    wx: wxFile({ rain: [['基隆', 25.135, 121.74, 0, 1, 5, 9, 30]] }) });
  await page.waitForFunction(() => WX_SRC === 'backend' && LTN_AT);
  await page.evaluate(() => {
    ADV = [{ kind: '大雷雨', cat: 'thunder', areas: ['基隆市'], end: Date.now() + 3600000 },
      { kind: '豪雨', cat: 'rain', areas: ['基隆市山區', '新北市'] }, { kind: '大雨', cat: 'rain', areas: ['基隆市'] }];
    renderTyphoon({}); renderStations();
  });
  const bar = page.locator('#tyAlertBar .al-bar');
  await expect(bar).toContainText('豪雨（含山區）・1 站在大雨以上範圍');
  await expect(bar).toContainText('⚡ 1 站附近落雷');
  await bar.click();
  const card = page.locator('#ty-brief #tyAlert .al-card');
  await expect(card).toBeVisible();
  await expect(card).toContainText('🌧 豪雨山區：基隆市山區；其他：新北市');
  await expect(card.locator('.al-r', { hasText: '站臺在大雨範圍' }).locator('.stn')).toHaveText('基隆測試站');   // 平地站：山區豪雨不算，算大雨
  await expect(card).toContainText('基隆測試站 15 km 內 1 次');
  await expect(card.locator('.al-r', { hasText: '⚡ 大雷雨' })).toContainText('範圍內站臺：基隆測試站');
  const cols = await card.locator('.al-r').evaluateAll(rs => rs.map(r => r.style.getPropertyValue('--c')));
  expect(cols).toEqual(['#EF8A35', '#EFC935', '#EFC935', 'var(--thz)', 'var(--thz)']);
  await toEq(page);
  const meta = page.locator('.st[data-id="k1"] .st-m').first();
  await expect(meta).toContainText('🌧 大雨');
  await expect(meta).toContainText('⚡ 15 km 內落雷 1 次');
  // 沒取得資料要講「沒取得」
  await page.evaluate(() => { ADV = []; WX_SRC = ''; LTN_AT = 0; renderTyphoon({}); });
  await expect(page.locator('#tyAlert')).toContainText('豪大雨與雷雨特報、落雷實測還沒取得，不能當成沒有');
  await expect(page.locator('#tyAlertBar .al-bar')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('播報稿：有颱風時開頭加「即時告警」一段；站名、打雷各有自己的顏色', async ({ page }) => {
  const errors = await bootWith(page, [NEAR]);
  const r = await page.evaluate(() => {
    ADV = [{ kind: '豪雨', cat: 'rain', areas: ['臺中市山區'] }];
    const t = briefText();
    const h = briefHtml('　站臺在大雨範圍：中心旁測試站。\n　大雷雨特報：基隆市。');
    return { t, st: (h.match(/class="b-st">([^<]+)/) || [])[1], thz: (h.match(/class="b-thz">([^<]+)/) || [])[1] };
  });
  expect(r.t).toContain('\n即時告警\n　豪雨：山區 臺中市山區。');
  expect(r.st).toBe('中心旁測試站');
  expect(r.thz).toBe('大雷雨特報');
  expect(errors).toEqual([]);
});
