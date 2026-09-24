#!/usr/bin/env node
import { chromium } from 'playwright-core';
import { existsSync, mkdirSync } from 'node:fs';

const baseUrl = process.argv[2] || 'http://127.0.0.1:8125/tests/vector-globe/';
const outputDir = process.argv[3] || '/tmp/vector-globe-final-qa';
mkdirSync(outputDir, { recursive: true });

const executableCandidates = [
  process.env.CHROME_BIN,
  '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable',
  '/usr/bin/chromium-browser',
  '/usr/bin/chromium'
].filter(Boolean);
const executablePath = executableCandidates.find(path => existsSync(path));
if (!executablePath) throw new Error('No Chrome/Chromium executable found');

const browser = await chromium.launch({
  headless: true,
  executablePath,
  args: [
    '--enable-webgl',
    '--ignore-gpu-blocklist',
    '--enable-unsafe-swiftshader',
    '--use-angle=swiftshader',
    '--disable-gpu-sandbox'
  ]
});

const page = await browser.newPage({
  viewport: { width: 1920, height: 1080 },
  deviceScaleFactor: 1
});

const pageErrors = [];
const httpErrors = [];
page.on('pageerror', error => pageErrors.push(String(error)));
page.on('response', response => {
  if (response.status() >= 400) httpErrors.push(response.status() + ' ' + response.url());
});

await page.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
await page.waitForFunction(() => Boolean(window.__vectorGlobe), null, { timeout: 20000 });
await page.waitForFunction(() => {
  const s = window.__vectorGlobe.getState();
  return s.activeTiles > 0 && s.activeLoads === 0 && s.queuedLoads === 0;
}, null, { timeout: 30000 });
await page.evaluate(() => window.__vectorGlobe.setQaLighting(true));

const regions = [
  { name: 'japan', lon: 139.7, lat: 35.7, maxTiles: 50 },
  { name: 'norway', lon: 20.0, lat: 65.0, maxTiles: 70 },
  { name: 'southern-chile', lon: -75.0, lat: -50.0, maxTiles: 70 },
  { name: 'aleutians-dateline', lon: 179.0, lat: 52.0, maxTiles: 70 },
  { name: 'antarctica', lon: 0.0, lat: -82.0, maxTiles: 90 }
];

const results = [];
for (const region of regions) {
  await page.evaluate(({lon,lat}) => window.__vectorGlobe.setView(lon, lat, 10), region);
  await page.waitForFunction(({lon,lat}) => {
    const s = window.__vectorGlobe.getState();
    const dlon = Math.abs((((s.centerLon - lon) + 540) % 360) - 180);
    return dlon < 0.01 &&
      Math.abs(s.centerLat - lat) < 0.01 &&
      s.currentLod === 4 &&
      s.activeTiles > 0 &&
      s.activeLoads === 0 &&
      s.queuedLoads === 0;
  }, region, { timeout: 90000 });

  const state = await page.evaluate(() => window.__vectorGlobe.getState());
  const status = await page.locator('#status').textContent();

  if (state.deliveryBundleMode) throw new Error(region.name + ': LOD4 unexpectedly uses a delivery bundle');
  if (state.activeTiles > region.maxTiles) throw new Error(region.name + ': too many active tiles: ' + state.activeTiles);
  if (state.cacheGpuBytes > state.cacheBudgetBytes * 1.3) throw new Error(region.name + ': GPU cache exceeds allowance');
  if (!/LOD4\s+full/.test(status || '')) throw new Error(region.name + ': status is not full LOD4: ' + status);
  if (/error/i.test(status || '')) throw new Error(region.name + ': viewer status reports error: ' + status);

  const screenshot = outputDir + '/' + region.name + '.png';
  await page.screenshot({ path: screenshot, fullPage: true });
  results.push({ ...region, state, status, screenshot });
  await page.evaluate(() => window.__vectorGlobe.clearUnusedGpuCache());
}

if (pageErrors.length) throw new Error('page errors: ' + pageErrors.join(' | '));
if (httpErrors.length) throw new Error('HTTP resource errors: ' + httpErrors.join(' | '));

console.log(JSON.stringify({ executablePath, regions: results }, null, 2));
await browser.close();
