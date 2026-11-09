'use strict';
const assert = require('node:assert/strict');
const {chromium} = require(process.argv[2]);
(async () => {
  const browser = await chromium.launch({executablePath:process.argv[3],headless:true});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror',error => errors.push(error.message));
    await page.route(/^https?:/, route => { errors.push('unexpected network access'); return route.abort(); });
    await page.goto(process.argv[4]);
    const problems = page.locator('#core-problems');
    assert.equal(await page.locator('h2[id^="sec-"]').count(),6,'six main report sections');
    assert.match(await page.locator('#sec-6').innerText(), /采集环境与软件栈/);
    assert.equal(await problems.locator('xpath=preceding::h2[1]').getAttribute('id'),'sec-1','findings live in section one');
    assert.equal(await page.locator('#diagnosis-evidence, #frequency-idle, #metric-help').count(),0,'removed technical sections are absent');
    if (process.argv[5] === '--e2e') {
      assert.equal(await page.locator('details.data-details[open]').count(),0,'complete archive tables initially collapsed');
      await page.setViewportSize({width:1440,height:900});
      const cardGeometry = await page.locator('.npu-grid').first().evaluate(grid => {
        const card=grid.querySelector('.npu-card');
        return {cards:grid.children.length,gridWidth:grid.getBoundingClientRect().width,width:card.getBoundingClientRect().width,height:card.getBoundingClientRect().height};
      });
      assert.equal(cardGeometry.cards,1);
      assert.ok(Math.abs(cardGeometry.width-cardGeometry.gridWidth)<2,'single NPU fills NUMA group');
      assert.ok(cardGeometry.height<450,'conclusion-first NPU card remains compact');
      const unbroken = await page.locator('td').evaluateAll(cells=>cells.filter(cell=>/^(5001|python3)$/.test(cell.textContent.trim()) && cell.getBoundingClientRect().height>0).every(cell=>{
        const range=document.createRange();range.selectNodeContents(cell);
        return getComputedStyle(cell).overflowWrap==='normal' && range.getClientRects().length===1;
      }));
      assert.ok(unbroken,'numeric IDs and process names remain unbroken in local table scroll');
      assert.match(await problems.locator('h3').innerText(),/核心问题：调度延迟严重/);
      const levels = await problems.locator('.diagnosis-status').allTextContents();
      assert.match(levels[0],/^严重/);
      const severity = {'严重': 3, '警告': 2, '提示': 1};
      const ranks = levels.map(label => severity[label.split(' · ')[0]]);
      assert.deepEqual(ranks, [...ranks].sort((a,b)=>b-a),'findings descend by severity');
      for (const width of [1440,375]) {
        await page.setViewportSize({width,height:900});
        assert.ok(await page.evaluate(()=>document.body.scrollWidth<=window.innerWidth+1),'no page-wide horizontal overflow at '+width);
      }
      assert.deepEqual(errors,[]);
      console.log('generated archive offline browser checks passed');
      return;
    }
    assert.equal(await page.locator('details.data-details[open]').count(),0,'Full tables start collapsed');
    const cores = page.locator('details.data-details').filter({has: page.locator('summary', {hasText:'全部CPU核心明细'})});
    await cores.locator('summary').click();
    assert.equal(await cores.locator('tbody tr').count(),100);
    await cores.locator('[data-table-search]').fill('99');
    assert.equal(await cores.locator('tbody tr:visible').count(),1);
    assert.equal(await cores.locator('tbody tr:visible td').first().innerText(),'99');
    await cores.locator('[data-table-search]').fill('');
    await cores.locator('[data-table-direction]').click();
    assert.equal(await cores.locator('tbody tr').first().locator('td').first().innerText(),'99','numeric descending sort');
    assert.equal(await cores.locator('tbody tr').count(),100,'search and sorting retain the displayed 100 rows');
    assert.deepEqual(errors,[],'offline scripts execute without errors');
    await page.close();
    const noJS = await browser.newContext({javaScriptEnabled:false});
    const fallback = await noJS.newPage(); await fallback.goto(process.argv[4]);
    const native = fallback.locator('details.data-details').filter({has: fallback.locator('summary',{hasText:'全部CPU核心明细'})});
    await native.locator('summary').click();
    assert.equal(await native.locator('tbody tr:visible').count(),100,'native disclosure works with JS disabled');
    assert.equal(await fallback.locator('#core-problems').isVisible(),true,'core analysis is readable with JS disabled');
    await noJS.close();
    console.log('all offline browser interactions passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
