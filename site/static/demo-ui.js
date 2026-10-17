/* Loaded only by the read-only demo entry point. */
(() => {
  document.body.classList.add('portfolio-demo');
  const bar = document.createElement('div');
  bar.className = 'demo-toolbar';
  bar.innerHTML = '<span>Демо на готовом примере · без списаний и новых AI-запросов</span><nav><a href="/?demo=result">Готовый результат</a><a href="/">Показать процесс</a></nav>';
  document.body.prepend(bar);
  const result = new URLSearchParams(location.search).get('demo') === 'result';
  if (result) {
    document.body.classList.add('demo-result');
    document.getElementById('sidebar')?.classList.add('collapsed');
    useExampleProduct();
  }
  document.addEventListener('click', e => {
    const b = e.target.closest('[onclick],a[href]');
    if (!b) return;
    const action = b.getAttribute('onclick') || '';
    if (/editCard\(|editDescription\(|requireAuth\(|openAuthModal\(|openSettings\(|regenerateDescription\(/.test(action) || /^\/(checkout|profile|settings)/.test(b.getAttribute('href') || '')) {
      e.preventDefault(); e.stopImmediatePropagation();
      if (typeof showToast === 'function') showToast('В демо можно рассмотреть и скачать готовые карточки. Новая генерация отключена.');
      else location.href = '/?demo=result';
    }
  }, true);
})();
