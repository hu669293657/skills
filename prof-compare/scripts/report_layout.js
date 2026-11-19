(() => {
  const menu = document.querySelector('.nav-sidebar');
  const compactMenu = matchMedia('(max-width:1199px)');
  function sizeMenu() { menu.open = !compactMenu.matches; }
  sizeMenu();
  compactMenu.addEventListener('change', sizeMenu);
  for (const link of menu.querySelectorAll('a')) {
    if (!document.getElementById(link.hash.slice(1))) {
      link.remove();
      continue;
    }
    link.addEventListener('click', () => { if (compactMenu.matches) menu.open = false; });
  }
  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape' && menu.open) {
      menu.open = false;
      menu.querySelector('summary').focus();
    }
  });

  // The compact layout uses the same cells and values as the wide table.
  for (const table of document.querySelectorAll('main table')) {
    if (table.closest('#kernel-execution') || !table.rows.length) continue;
    let labels, dataRows;
    if (table.classList.contains('paired-table')) {
      const metrics = Array.from(table.tHead.rows[0].cells).slice(1, -1);
      const sides = Array.from(table.tHead.rows[1].cells, cell => cell.textContent.trim());
      labels = ['#'];
      for (const [i, metric] of metrics.entries()) {
        for (const side of sides.slice(i * 2, i * 2 + 2)) labels.push(side + ' · ' + metric.textContent);
      }
      labels.push('差值（左 − 右 ms）', '倍率（左 / 右）');
      dataRows = table.tBodies[0].rows;
    } else {
      const [header, ...rows] = table.rows;
      if (header.cells.length <= 4 || !Array.from(header.cells).every(c => c.tagName === 'TH' && c.colSpan === 1)) continue;
      labels = Array.from(header.cells, cell => cell.textContent.trim());
      dataRows = rows;
      table.classList.add('compact-table');
    }
    for (const row of dataRows) {
      Array.from(row.cells).forEach((cell, i) => { cell.dataset.label = labels[i]; });
    }
  }
})();
