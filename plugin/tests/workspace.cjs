const assert = require('node:assert/strict');

module.exports = async function checkWorkspace(page) {
  const viewport = page.viewportSize();
  const dark = await page.locator('html').evaluate(element => element.classList.contains('dark'));
  const labels = page.locator('#hostname-label-form');
  const layers = page.locator('#device-placement-form');
  const rows = page.locator('#device-list tbody tr:visible').filter({has:page.locator('.hostname-device')});
  const first = rows.nth(0), second = rows.nth(1);
  const hostname = await first.locator('td small').first().innerText();
  const layerId = await second.locator('.placement-device').inputValue();
  const layerCheckbox = page.locator(`.placement-device[value="${layerId}"]`);
  await first.locator('.hostname-device').check();
  await layerCheckbox.check();
  await page.locator('#hostname-parts').fill('3');
  await page.locator('#device-layer').fill('-7');
  assert.match(await page.locator('#hostname-impact').innerText(), /components to 3 for 1 device/);
  assert.match(await page.locator('#placement-impact').innerText(), /layer -7 for 1 device/);
  await page.locator('#device-search').fill(hostname);
  assert.match(await page.locator('#placement-hidden-count').innerText(), /1 hidden/);
  assert.match(await page.locator('#placement-impact').innerText(), /including 1 hidden/);
  const payloads = await page.evaluate(() => {
    const labels = new FormData(document.querySelector('#hostname-label-form'));
    const layers = new FormData(document.querySelector('#device-placement-form'));
    return {parts:labels.get('hostname_parts'), layer:layers.get('placement'), labelDevices:labels.getAll('hostname_device_ids[]').length,
      layerDevices:layers.getAll('placement_device_ids[]').length, crossed:labels.has('placement_device_ids[]') || layers.has('hostname_device_ids[]')};
  });
  assert.deepEqual(payloads, {parts:'3', layer:'-7', labelDevices:1, layerDevices:1, crossed:false}, 'Each value must submit only its own selected devices, including hidden selections.');
  await labels.scrollIntoViewIfNeeded();
  await page.screenshot({path:'/artifacts/workspace-effect.png'});
  await labels.getByRole('button', {name:'Clear hostname selection', exact:true}).click();
  assert.equal(await page.locator('.hostname-device:checked').count(), 0);
  assert(await layerCheckbox.isChecked(), 'Clearing hostname selections must preserve hidden layer selections.');
  await layers.getByRole('button', {name:'Clear layer selection', exact:true}).click();
  assert.equal(await page.locator('.placement-device:checked').count(), 0);
  assert(await layers.locator('button[type="submit"]').isDisabled());
  await page.locator('#device-search').fill('');
  await page.locator('#hostname-parts').fill('');
  await first.locator('.hostname-device').check();
  assert.match(await page.locator('#hostname-impact').innerText(), /Restore map label defaults for 1 device/);
  await labels.getByRole('button', {name:'Clear hostname selection', exact:true}).click();
  await page.locator('#device-layer').fill('');

  const help = labels.locator('legend .lm-help');
  await help.focus();
  const tooltip = page.getByRole('tooltip');
  await tooltip.waitFor({state:'visible'});
  assert.match(await tooltip.innerText(), /does not rename LibreNMS devices/);
  await page.locator('#hostname-parts').focus();
  await tooltip.waitFor({state:'hidden'});
  await help.hover();
  await tooltip.waitFor({state:'visible'});
  await page.locator('#device-search').hover();
  await tooltip.waitFor({state:'hidden'});

  await labels.scrollIntoViewIfNeeded();
  await page.screenshot({path:'/artifacts/workspace-lanes.png'});
  await page.locator('html').evaluate(element => element.classList.add('dark'));
  await page.screenshot({path:'/artifacts/workspace-lanes-dark.png'});
  await page.locator('html').evaluate((element, value) => element.classList.toggle('dark', value), dark);
  await page.setViewportSize({width:390, height:1100});
  await labels.scrollIntoViewIfNeeded();
  const labelBox = await labels.boundingBox(), layerBox = await layers.boundingBox();
  assert(layerBox.y >= labelBox.y + labelBox.height, 'Narrow screens must stack the independent control lanes.');
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'The workspace must fit a narrow viewport; tables scroll within their own region.');
  await page.screenshot({path:'/artifacts/workspace-lanes-mobile.png'});
  await page.setViewportSize(viewport);
};
