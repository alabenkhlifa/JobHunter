const assert = require('node:assert/strict');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const origin = 'https://review.example';
    const prefix = '/resume-review';
    await page.route(origin + '/**', async route => {
      const request = route.request();
      const url = new URL(request.url());
      assert.ok(url.pathname.startsWith(prefix + '/'));
      const response = await route.fetch({url: process.argv[2] + url.pathname.slice(prefix.length),
        headers: {...request.headers(), host: 'review.example'}});
      await route.fulfill({response});
    });
    await page.goto(origin + prefix + '/#token=synthetic-private-access-token-1234567890');
    await page.waitForFunction(() => document.querySelector('#render-status').textContent === 'Preview up to date ✓');
    assert.equal(new URL(page.url()).hash, '');
    assert.equal(await page.locator('#flavor-heading').isVisible(), false);
    assert.equal(await page.locator('.variant-heading').isVisible(), false);
    assert.equal(await page.locator('[data-duplicate="flavor"]').count(), 0);
    const cookies = (await page.context().cookies()).filter(cookie => cookie.domain === 'review.example');
    assert.equal(cookies.length, 1);
    assert.equal(cookies[0].httpOnly, true);
    assert.equal(cookies[0].secure, true);
    assert.equal(cookies[0].path, '/resume-review/');
    const saved = () => page.waitForFunction(() => document.querySelector('#save-status').textContent === 'All changes saved ✓');
    const read = () => page.evaluate(async () => (await fetch('api/state')).json());
    await page.getByRole('textbox', {name: 'Professional summary', exact: true}).fill('Builds reliable Java services.');
    await saved();
    await page.locator('[data-section="projects"]').click();
    await page.getByRole('textbox', {name: 'Bullet 1', exact: true}).fill('Owned five Java services with integration tests.');
    await saved();
    await page.locator('[data-section="sidebar"]').click();
    const skills = page.locator('details[data-details-key=\'["base","skills"]\']');
    await skills.locator(':scope > summary').click();
    await page.getByRole('textbox', {name: 'Skill 1', exact: true}).fill('Kotlin');
    await saved();
    await page.getByRole('button', {name: /Add skill group/}).click();
    await saved();
    await page.getByRole('textbox', {name: 'Skill group heading 2', exact: true}).fill('Delivery');
    await saved();
    await skills.getByRole('textbox', {name: 'Skill 1', exact: true}).last().fill('Docker');
    await saved();
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#render-status').textContent === 'Preview up to date ✓');
    const state = await read();
    assert.equal(state.catalog.flavors[0].summary, 'Builds reliable Java services.');
    assert.equal(state.catalog.projects[0].entry.bullets[0], 'Owned five Java services with integration tests.');
    assert.deepEqual(state.catalog.base.skills, {Backend: ['Kotlin', 'PostgreSQL'], Delivery: ['Docker']});
    assert.equal(state.catalog.flavors.length, 1);
    assert.equal(state.catalog.variants.length, 1);
    assert.ok((await page.locator('#pages img').first().getAttribute('src')).startsWith(prefix + '/previews/'));
    const pdf = await page.evaluate(async () => {
      const response = await fetch(document.querySelector('#download').href);
      return {status: response.status, signature: new TextDecoder().decode((await response.arrayBuffer()).slice(0, 5))};
    });
    assert.equal(pdf.status, 200);
    assert.equal(pdf.signature, '%PDF-');
    assert.deepEqual(errors, []);
    console.log('Authenticated single resume: autosave, bullets, skills, reload, preview and PDF passed');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.stack); process.exit(1); });
