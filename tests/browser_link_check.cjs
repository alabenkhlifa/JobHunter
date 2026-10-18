const assert = require('node:assert/strict');
const playwright = require('playwright');

(async () => {
  const [engine, senderUrl, link] = process.argv.slice(2);
  const browser = await playwright[engine].launch({headless: true});
  try {
    const context = await browser.newContext({ignoreHTTPSErrors: true});
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    await page.goto(senderUrl);
    const viewerResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/browser/vnc.html');
    await page.getByRole('link', {name: 'Open browser'}).click();
    const response = await viewerResponse;
    assert.equal(response.status(), 200, 'Fresh cross-site link must reach the viewer');
    assert.equal(await page.locator('body').innerText(), 'Synthetic candidate viewer');
    const cookie = (await context.cookies()).find(item => item.name === 'jobhunter_browser');
    assert.ok(cookie.secure && cookie.httpOnly);
    assert.equal(cookie.sameSite, 'Lax');
    assert.equal(cookie.path, '/browser');
    const replay = await page.goto(link);
    assert.equal(replay.status(), 403, 'Connection link must remain single-use');
    console.log(`${engine}: cross-site viewer navigation and replay rejection passed`);
    await context.close();
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error.message); process.exit(1);});
