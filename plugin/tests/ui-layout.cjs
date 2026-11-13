const assert = require('node:assert/strict');

module.exports = async function checkUiLayout(page, base) {
  const map = 'generated-core-browser';
  const open = async (tab, subtab) => page.goto(`${base}/plugin/settings/libremapper?map=${map}&tab=${tab}&subtab=${subtab}`);
  await page.setViewportSize({width:1920, height:1080});
  await open('devices', 'inventory');
  assert(await page.locator('#device-scope-form').isVisible());
  assert.equal(await page.locator('#device-list').count(), 0);
  await open('devices', 'matched');
  assert(await page.locator('#device-list').isVisible());
  assert.equal(await page.locator('#device-scope-form').count(), 0);
  await page.screenshot({path:'/artifacts/ui-matched-devices-1080.png'});

  for (const [tab, table, editor, editPane] of [
    ['devices', '#manual-devices', '#manual-device-editor', '#manual-device-edit'],
    ['links', '#manual-connections', '.connection-form', '#manual-link-edit'],
  ]) {
    await open(tab, 'manual');
    assert(await page.locator(table).isVisible(), 'Manual records must be the default view.');
    assert(!await page.locator(editor).isVisible(), 'The manual editor must have its own tab.');
    const headers = page.locator(table === '#manual-devices' ? `${table} thead th` : '#plan-links thead th');
    assert.equal(await headers.nth(-2).innerText(), 'This map');
    assert.equal(await headers.last().innerText(), 'Shared across maps');
    const row = page.locator(`${table} tr`).filter({has:page.locator('input[name="source_id"]')}).first();
    if (await row.count()) assert((await row.boundingBox()).height <= 110, 'Manual rows must stay compact at 1080p.');
    await page.screenshot({path:`/artifacts/ui-manual-${tab}-1080.png`});
    await page.locator('html').evaluate(element => element.classList.add('dark'));
    const colors = await page.locator(table).evaluate(element => {
      const link = element.querySelector('a');
      return {link:getComputedStyle(link).color, text:getComputedStyle(element).color};
    });
    assert.notEqual(colors.link, colors.text, 'Dark-mode links must be distinct from regular text.');
    await page.screenshot({path:`/artifacts/ui-manual-${tab}-dark-1080.png`});
    await page.setViewportSize({width:390, height:844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Manual tables must scroll inside the viewport.');
    await page.screenshot({path:`/artifacts/ui-manual-${tab}-mobile.png`});
    await page.setViewportSize({width:1920, height:1080});
    await page.locator(`[href="${editPane}"]`).click();
    assert(await page.locator(editor).isVisible());
    assert(!await page.locator(table).isVisible());
    await page.locator('html').evaluate(element => element.classList.remove('dark'));
  }

  for (const [subtab, selector] of [['topology', '#map-layout-form'], ['overrides', '#device-list']]) {
    await open('layout', subtab);
    assert(await page.locator(selector).isVisible());
    assert.equal(await page.locator(subtab === 'topology' ? '#device-list' : '#topology-preview').count(), 0);
  }
  for (const [subtab, selector] of [
    ['configuration', '#selectors_yaml'], ['appearance', '#layout_type'], ['oxidized', '#oxidized_mode'],
    ['parsers', '#parser-catalog'], ['agent-access', '#agent-access'], ['files', '#file_content'], ['maintenance', '#confirmation'],
  ]) {
    await open('advanced', subtab);
    assert(await page.locator(selector).isVisible(), `Advanced ${subtab} must show its controls.`);
    assert.equal(await page.locator('#libremapper-settings form[action$="/reset"]').count(), subtab === 'maintenance' ? 1 : 0);
  }
  await open('links', 'parsers');
  assert.equal(await page.locator('#oxidized_mode').count(), 0);
  await open('review', '');
  const order = await page.evaluate(() => {
    const preview = document.querySelector('#topology-preview');
    const form = document.querySelector('#preview-options').closest('form');
    const publish = document.querySelector('form[action$="/apply"]');
    return Boolean(form.compareDocumentPosition(preview) & Node.DOCUMENT_POSITION_FOLLOWING)
      && Boolean(preview.compareDocumentPosition(publish) & Node.DOCUMENT_POSITION_FOLLOWING);
  });
  assert(order, 'Review actions must precede topology and publishing must follow it.');
  await page.screenshot({path:'/artifacts/ui-review-1080.png', fullPage:true});
};
