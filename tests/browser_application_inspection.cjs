const fs = require('node:fs');
let playwright;
try { playwright = require('playwright'); }
catch { playwright = require('playwright-core'); }

(async () => {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await playwright.chromium.launch({headless: true,
    executablePath: process.env.JOBHUNTER_CHROMIUM_EXECUTABLE});
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => route.fulfill({contentType: 'text/html',
      body: input.frames?.[new URL(route.request().url()).pathname] ?? input.html}));
    await page.goto('https://careers.example.test/application/tech-leader');
    if (input.reveal) await page.locator('#method-cv').click();
    process.stdout.write(JSON.stringify(await page.evaluate(input.expression)));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
