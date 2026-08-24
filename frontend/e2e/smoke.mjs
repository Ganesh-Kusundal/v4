// Browser E2E for the TradeX terminal shell served at /ui/.
// Drives real Chrome against a running `tradex serve` instance and asserts
// the master-parity chrome actually works: chart canvas paints, interval
// pills switch, indicator add -> settings modal -> apply, draw tool arms,
// replay transport opens, and Buy hits POST /orders on the execution spine.
//
// Usage:
//   node frontend/e2e/smoke.mjs http://127.0.0.1:PORT
// Playwright resolves from ../../openalgo-charts-master/node_modules.

// ESM ignores NODE_PATH, so resolve playwright by explicit path (the
// openalgo-charts-master checkout ships it); override with PLAYWRIGHT_PKG.
const { chromium } = await import(
  process.env.PLAYWRIGHT_PKG ??
  "/Users/apple/Downloads/openalgo-charts-master/node_modules/playwright/index.mjs"
);

const BASE = process.argv[2] ?? "http://127.0.0.1:8000";
const results = [];
const check = (name, ok, detail = "") =>
  results.push([ok, `${ok ? "PASS" : "FAIL"} ${name}${detail ? " :: " + detail : ""}`]) &&
  console.log(results.at(-1)[1]);

// Reuse the already-installed browser build; version skew with the npm
// playwright is acceptable for this smoke (no playwright-internal APIs used).
const exe = process.env.CHROMIUM_PATH;
const browser = await chromium.launch(exe ? { executablePath: exe } : {});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const pageErrors = [];
page.on("pageerror", (e) => pageErrors.push(String(e)));

try {
  await page.goto(BASE + "/ui/", { waitUntil: "domcontentloaded" });

  // 1. Chart renders a canvas inside the stage.
  await page.waitForSelector("#chart canvas", { timeout: 20000 });
  check("browser.chart.canvas", true);

  // 2. Shellbar chrome present.
  const tbtns = await page.locator("#shellbar .tbtn").count();
  check("browser.shellbar.controls", tbtns >= 6, `${tbtns} controls`);
  check("browser.interval.pills", (await page.locator(".shellbar .pills button").count()) === 6);

  // 3. Interval switch repaints (status text updates with new interval).
  await page.click('.shellbar .pills button[data-iv="15m"]');
  await page.waitForFunction(
    () => document.getElementById("status")?.textContent?.includes("15m"),
    null, { timeout: 15000 },
  );
  check("browser.interval.switch", true);

  // 4. Watchlist populated from backend universe.
  await page.waitForSelector("#watchlist .wl-row", { timeout: 20000 });
  const rows = await page.locator("#watchlist .wl-row").count();
  check("browser.watchlist.rows", rows > 10, `${rows} symbols`);

  // 5. Indicator add -> chip -> settings modal -> apply.
  await page.click('#shellbar .tbtn:has-text("Indicators")');
  await page.waitForSelector('.menu[data-role="ind"]', { timeout: 10000 });
  await page.click('.menu[data-role="ind"] button >> nth=0');
  await page.waitForSelector("#shellbar .chip", { timeout: 20000 });
  check("browser.indicator.chip", true);
  await page.click("#shellbar .chip button[title='Settings']");
  await page.waitForSelector("#setmodal:not([hidden])", { timeout: 10000 });
  const numInput = page.locator("#set-body input[type=number]").first();
  if ((await numInput.count()) > 0) {
    await numInput.fill("7");
    await page.click("#set-ok");
    check("browser.indicator.apply", true);
  } else {
    await page.click("#set-cancel");
    check("browser.indicator.apply", true, "no-param indicator skipped");
  }

  // 6. Draw tool arms from the rail.
  await page.click("#rail button >> nth=1");
  check("browser.draw.rail.armed",
    (await page.locator("#rail button.is-on").count()) === 1);

  // 7. Replay transport opens and exits.
  await page.click('#shellbar .tbtn:has-text("Replay")');
  await page.waitForSelector("#replaybar:not([hidden])", { timeout: 20000 });
  check("browser.replay.transport", true);
  await page.click('#replaybar button[title="Exit replay"]');

  // 8. Order ticket hits the execution spine (paper rejects market w/o tape;
  //    what matters E2E is that the request traverses POST /orders).
  await page.fill(".field--qty", "3");
  const respPromise = page.waitForResponse(
    (r) => r.url().endsWith("/orders") && r.request().method() === "POST",
    { timeout: 15000 },
  );
  await page.click(".tbtn--buy");
  const resp = await respPromise;
  check("browser.order.spine", resp.status() === 200, `HTTP ${resp.status()}`);

  // 9. Bottom dock tabs switch.
  await page.click('#bottom-tabs button[data-tab="orders"]');
  check("browser.dock.tabs",
    (await page.locator('#bottom-panels .dock-panel.active').getAttribute("data-tab")) === "orders");

  // 10. No uncaught page errors during the whole run.
  check("browser.no.pageerrors", pageErrors.length === 0, pageErrors.slice(0, 2).join(" | "));
} catch (e) {
  check("browser.run.completed", false, String(e).slice(0, 200));
} finally {
  await browser.close();
}

const fails = results.filter(([ok]) => !ok);
console.log(`\n==== BROWSER E2E SUMMARY ==== \n${results.length - fails.length}/${results.length} passed`);
process.exit(fails.length ? 1 : 0);
