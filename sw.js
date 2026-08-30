/* 風哨 GALEPOST — 離線快取
 *
 * 颱風天網路是最先斷的東西,所以 app 本體一律走「快取優先」:
 * 裝過一次之後就算完全沒有訊號,站臺管制與回報產生照樣能用。
 *
 * 氣象資料是唯一的例外 —— 過期的颱風資訊比沒有資訊更危險,
 * 所以 CWA / GDACS 的請求一律直接走網路,絕不快取。
 */
const CACHE = 'galepost-v36';
const SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './assets/icon-192.png',
  './assets/icon-512.png',
  './assets/galepost-icon.svg',
  './data/typhoon.json',
  // Leaflet 收在站內,不再走 CDN:第一次安裝之後離線也開得出地圖。
  // 圖磚仍然要連網,但地圖框架與控制項本身不會再因為斷線而整個消失。
  './vendor/leaflet/leaflet.js',
  './vendor/leaflet/leaflet.css',
  './vendor/leaflet/images/layers.png',
  './vendor/leaflet/images/layers-2x.png',
  './vendor/leaflet/images/marker-icon.png',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      // 個別加入:任何一個資產抓不到都不該讓整個安裝失敗
      .then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => {}))))
  );
  // 這裡刻意不呼叫 skipWaiting:颱風天正在操作的人不該被新版本從腳下抽換。
  // 新版本會在旁邊等,由使用者按下「重新載入」才接手(見下方 message 處理)。
});

self.addEventListener('message', e => {
  if (e.data === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // 氣象資料永遠取最新,不進快取。
  // Windy 一併排除:那是逐格的地圖圖磚,快取起來會很快把裝置的儲存配額塞爆,
  // 而且離線時看舊的雷達回波本來就沒有意義。
  if (/(^|\.)cwa\.gov\.tw$/.test(url.hostname) ||
      /(^|\.)gdacs\.org$/.test(url.hostname) ||
      /(^|\.)windy\.com$/.test(url.hostname)) {
    return;
  }

  /* 停班停課公告同理 —— 而且更嚴重:昨天的公告寫著「明天停止上班」,
     今天拿快取讀到那一行,會直接讀成今天放假。
     公告是透過代理取回來的,官方網址在 query string 裡,所以比對整串 URL
     而不是 hostname —— 代理是誰、叫什麼名字,程式不該預設。 */
  if (/dgpa\.gov\.tw/.test(req.url)) return;

  // 颱風資料:網路優先,離線才退回快取。
  // 這份會隨颱風動態改變,拿快取優先會慢一輪;但離線時有舊的總比沒有好,
  // 畫面上另外會標明取得時間,不會讓人誤以為是即時的。
  if (url.origin === location.origin && url.pathname.endsWith('/data/typhoon.json')) {
    e.respondWith(
      fetch(req)
        .then(res => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then(c => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // 同源資產:快取優先,背景順手更新
  if (url.origin === location.origin) {
    e.respondWith(
      caches.match(req).then(hit => {
        const net = fetch(req)
          .then(res => {
            if (res && res.ok) {
              const copy = res.clone();
              caches.open(CACHE).then(c => c.put(req, copy));
            }
            return res;
          })
          .catch(() => hit);
        return hit || net;
      })
    );
    return;
  }

  // 跨源(地圖圖磚、Leaflet):網路優先,失敗才回快取
  e.respondWith(
    fetch(req)
      .then(res => {
        if (res && res.ok && res.type !== 'opaque') {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req))
  );
});
