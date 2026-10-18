const assert = require('node:assert/strict');
const fs = require('node:fs');
const playwright = require('playwright');

(async () => {
  const {expression, cases} = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await playwright[process.argv[2]].launch({headless: true});
  try {
    const page = await browser.newPage();
    let html = '';
    // Every page is synthetic and offline, including the provider hostname.
    await page.route('**/*', route => route.fulfill({contentType: 'text/html', body: html}));
    for (const fixture of cases) {
      html = fixture.html;
      await page.goto('https://www.linkedin.com' + fixture.path);
      assert.equal(await page.evaluate(expression), fixture.expected, fixture.name);
    }
    console.log(`${process.argv[2]}: ${cases.length} sign-in fixtures passed`);
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error.message); process.exit(1);});
