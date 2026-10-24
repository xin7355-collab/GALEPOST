// 風哨的瀏覽器自動測試。
// 站臺是單檔 HTML，用 Python 內建的靜態伺服器就跑得起來，不必另外裝 http-server。
// 測試一律擋掉對外連線（氣象署、Windy、GDACS），所以結果不受網路與金鑰影響。
import { defineConfig, devices } from '@playwright/test';

const PORT = 4173;

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    // Service Worker 會把舊版快取起來，測到的可能不是剛改的那一版
    serviceWorkers: 'block',
    // 使用者在臺灣：時間顯示、日期換日都要照臺灣時間測（CI 機器是 UTC，警報時間會差 8 小時）
    timezoneId: 'Asia/Taipei',
    locale: 'zh-TW',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    // 使用者主要用 iPhone：以 iPhone 13 的畫面大小與觸控模擬，引擎用 Chromium（CI 上最穩）
    { name: 'iphone', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
  ],
  webServer: {
    command: `python3 -m http.server ${PORT} --bind 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}/index.html`,
    reuseExistingServer: !process.env.CI,
    timeout: 20_000,
    stderr: 'ignore',   // http.server 每個請求都印一行，蓋掉真正的測試結果
  },
});
