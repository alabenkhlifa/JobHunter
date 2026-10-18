const assert = require('node:assert/strict');
let playwright;
try { playwright = require('playwright'); }
catch { playwright = require('playwright-core'); }

(async () => {
  const browser = await playwright.chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const scoped = process.argv[3] === 'scoped';
    const basePath = scoped ? ['variants', 1, 'base_overrides'] : ['base'];
    const detail = path => page.locator(`details[data-details-key='${JSON.stringify(path)}']`);
    const skills = detail([...basePath, 'skills']);
    const backend = detail([...basePath, 'skills', 0]);
    const delivery = detail([...basePath, 'skills', 1]);
    const saved = () => page.waitForFunction(() => document.querySelector('#save-status').textContent === 'All changes saved ✓');
    const read = () => page.evaluate(async () => (await fetch('/api/state')).json());
    const readSkills = async () => {const state = await read(); return scoped ? state.catalog.variants[1].base_overrides.skills : state.catalog.base.skills;};
    const open = async (element, expected) => assert.equal(await element.evaluate(node => node.open), expected);
    const items = () => backend.locator('textarea[data-path]').evaluateAll(nodes => nodes.map(node => node.value));

    await page.goto(process.argv[2]);
    await page.waitForFunction(() => document.querySelector('#render-status').textContent === 'Preview up to date ✓');
    await page.locator('[data-section="sidebar"]').click();
    await skills.locator(':scope > summary').click();
    await delivery.locator(':scope > summary').click();

    await backend.getByRole('button', {name: 'Move Skill 1 down', exact: true}).click();
    await saved();
    await open(skills, true);
    await open(backend, true);
    await open(delivery, false);
    assert.deepEqual(await items(), ['PostgreSQL', 'Java']);
    await backend.getByRole('button', {name: 'Move Skill 2 up', exact: true}).click();
    await saved();
    await open(skills, true);
    await open(backend, true);
    await open(delivery, false);
    assert.deepEqual(await items(), ['Java', 'PostgreSQL']);

    const heading = page.getByRole('textbox', {name: 'Skill group heading 1', exact: true});
    await heading.fill('Platform');
    await heading.pressSequentially(' & "Architecture"');
    await saved();
    assert.equal(await heading.inputValue(), 'Platform & "Architecture"');
    assert.deepEqual(Object.keys(await readSkills()), ['Platform & "Architecture"', 'Delivery']);
    assert.equal(await backend.locator(':scope > summary').textContent(), 'Platform & "Architecture"');
    await open(skills, true);
    await open(backend, true);
    await open(delivery, false);

    await backend.getByRole('textbox', {name: 'Skill 1', exact: true}).fill('Kotlin');
    await saved();
    await backend.getByRole('button', {name: 'Move Skill 1 down', exact: true}).click();
    await saved();
    assert.deepEqual((await readSkills())['Platform & "Architecture"'], ['PostgreSQL', 'Kotlin']);
    await open(skills, true);
    await open(backend, true);
    await open(delivery, false);

    await backend.getByRole('button', {name: 'Add skill'}).click();
    await saved();
    await open(skills, true);
    await backend.locator('[data-remove]').last().click();
    await saved();
    await open(skills, true);
    assert.deepEqual((await readSkills())['Platform & "Architecture"'], ['PostgreSQL', 'Kotlin']);

    await backend.locator('[data-request]').first().click();
    assert.ok((await page.locator('#request-target').textContent()).includes('Platform & "Architecture"'));
    await page.getByRole('button', {name: 'Cancel', exact: true}).click();

    await heading.fill('');
    await heading.press('Tab');
    assert.equal(await backend.locator('[data-heading-error]').textContent(), 'Enter a skill group heading.');
    await heading.fill('Delivery');
    await heading.press('Tab');
    assert.equal(await backend.locator('[data-heading-error]').textContent(), 'A skill group already uses this heading.');
    assert.deepEqual(Object.keys(await readSkills()), ['Platform & "Architecture"', 'Delivery']);
    await heading.fill('Platform & "Architecture"');
    await heading.press('Tab');
    assert.equal(await backend.locator('[data-heading-error]').isVisible(), false);
    await saved();

    await page.locator('[data-section="flavor"]').click();
    await page.locator('[data-section="sidebar"]').click();
    await open(skills, true);
    await open(backend, true);
    await open(delivery, false);
    assert.deepEqual(await items(), ['PostgreSQL', 'Kotlin']);

    await page.reload();
    await page.locator('[data-section="sidebar"]').click();
    await skills.locator(':scope > summary').click();
    assert.equal(await heading.inputValue(), 'Platform & "Architecture"');
    assert.deepEqual(await items(), ['PostgreSQL', 'Kotlin']);
    if (scoped) {
      await page.locator('[data-section="projects"]').click();
      await page.getByRole('textbox', {name: 'Bullet 1', exact: true}).fill('Designed a version-specific Java service.');
      await saved();
      const state = await read();
      assert.equal(state.catalog.projects[0].entry.bullets[0], 'Owned five **Java services**.');
      assert.equal(state.catalog.variants[1].project_overrides.services.entry.bullets[0], 'Designed a version-specific Java service.');
    }
    assert.deepEqual(errors, []);
    console.log('Skills heading edits, validation, reorder, disclosure state, autosave and reload passed');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.stack); process.exit(1); });
