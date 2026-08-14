/* =====================================================================
   account.js — shared client for the WBnator token economy & auth.
   Loaded by index.html, profile.html, checkout.html.

   • Talks to /api/account/* with the httponly session cookie.
   • One reusable email+password auth modal (login / register + consent).
   • Formatting helpers for the unified "tokens (≈ карточки)" currency.
   Namespaced under window.Account; modal styles use the `wbnacc-` prefix so
   they never clash with a host page.
   ===================================================================== */
(function () {
  'use strict';

  const API = '/api/account';
  let _me = undefined;          // undefined = unknown, null = anonymous, obj = user
  let _pricing = null;
  const meListeners = [];

  async function jfetch(url, opts) {
    const res = await fetch(url, Object.assign({ credentials: 'include' }, opts || {}));
    let data = null;
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) {
      const err = new Error((data && data.error) || ('HTTP ' + res.status));
      err.code = data && data.code; err.status = res.status; err.data = data;
      throw err;
    }
    return data;
  }

  // ── currency helpers (валюта = карточки) ──────────────────────────────────
  function fmt(n) { return Number(n).toLocaleString('ru-RU'); }
  function declCards(n) {
    const t = n % 100, l = n % 10;
    if (t >= 11 && t <= 14) return 'карточек';
    if (l === 1) return 'карточка';
    if (l >= 2 && l <= 4) return 'карточки';
    return 'карточек';
  }
  // Мгновенная цена ползунка — считаем на клиенте по кривой из /pricing,
  // без round-trip на каждое движение (фикс «цена долго подгружается»).
  function quoteLocal(cards) {
    const sl = _pricing && _pricing.slider;
    if (!sl) return null;
    cards = Math.max(sl.min_cards, Math.min(sl.max_cards, Math.round(cards)));
    const a = sl.anchors;
    let per;
    if (cards <= a[0][0]) per = a[0][1];
    else if (cards >= a[a.length - 1][0]) per = a[a.length - 1][1];
    else {
      let i = 0; while (i < a.length - 1 && a[i + 1][0] <= cards) i++;
      const x0 = a[i][0], y0 = a[i][1], x1 = a[i + 1][0], y1 = a[i + 1][1];
      per = y0 + (y1 - y0) * ((cards - x0) / (x1 - x0));
    }
    const amount_kopecks = Math.round(cards * per);
    // base = цена за карточку на самом маленьком объёме (первый якорь) — от неё
    // считаем «выгоду N%», как делает quote_cards() в accounts/pricing.py.
    const base = a[0][1];
    const save_kopecks = Math.max(0, Math.round(cards * base - amount_kopecks));
    const discount_pct = Math.round((1 - per / base) * 100);
    return { cards, amount_kopecks, amount_rub: amount_kopecks / 100,
             per_card_kopecks: per, per_card_rub: per / 100,
             save_kopecks, save_rub: save_kopecks / 100, discount_pct };
  }

  // ── referral capture (?ref=CODE persists for later registration) ──────────
  function captureRef() {
    try {
      const p = new URLSearchParams(location.search);
      const ref = (p.get('ref') || '').trim().toUpperCase();
      if (ref) localStorage.setItem('wbn_ref', ref);
    } catch (e) {}
  }
  function storedRef() {
    try { return localStorage.getItem('wbn_ref') || ''; } catch (e) { return ''; }
  }

  // ── API ───────────────────────────────────────────────────────────────────
  async function me(force) {
    if (_me !== undefined && !force) return _me;
    try {
      const d = await jfetch(API + '/me');
      _me = d.user || null;
    } catch (e) { _me = null; }
    meListeners.forEach(fn => { try { fn(_me); } catch (e) {} });
    return _me;
  }
  function onMe(fn) { meListeners.push(fn); if (_me !== undefined) fn(_me); }
  function current() { return _me; }

  async function pricing(force) {
    if (_pricing && !force) return _pricing;
    _pricing = await jfetch(API + '/pricing');
    return _pricing;
  }
  async function quote(cards) {
    return jfetch(API + '/quote', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cards })
    });
  }
  async function login(email, password) {
    const d = await jfetch(API + '/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    });
    _me = d.user; meListeners.forEach(fn => { try { fn(_me); } catch (e) {} });
    return _me;
  }
  async function register(payload) {
    const d = await jfetch(API + '/register', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    _me = d.user; meListeners.forEach(fn => { try { fn(_me); } catch (e) {} });
    try { localStorage.removeItem('wbn_ref'); } catch (e) {}
    return _me;
  }
  async function logout() {
    try { await jfetch(API + '/logout', { method: 'POST' }); } catch (e) {}
    _me = null; meListeners.forEach(fn => { try { fn(_me); } catch (e) {} });
  }
  async function requestPasswordReset(email) {
    return jfetch(API + '/password-reset/request', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email })
    });
  }
  async function confirmPasswordReset(token, password) {
    return jfetch(API + '/password-reset/confirm', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token, password })
    });
  }
  async function applyPromo(code) {
    return jfetch(API + '/promo', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code })
    });
  }
  async function createPurchase(payload) {
    return jfetch(API + '/purchase', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
  }
  async function history() { return jfetch(API + '/history'); }
  async function referral() { return jfetch(API + '/referral'); }
  async function updateAccount(payload) {
    return jfetch(API + '/account', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
  }
  async function deleteAccount() {
    return jfetch(API + '/account', { method: 'DELETE' });
  }

  // ── OAuth (Google / Yandex) ─────────────────────────────────────────────────
  let _oauthProviders = null;     // null = unknown, [] = none configured
  async function oauthProviders(force) {
    if (_oauthProviders && !force) return _oauthProviders;
    try {
      const d = await jfetch(API + '/oauth/providers');
      _oauthProviders = (d && d.providers) || [];
    } catch (e) { _oauthProviders = []; }
    return _oauthProviders;
  }
  // Запуск соц-входа. next = куда вернуть после входа.
  function oauthStart(provider, next) {
    const ref = storedRef();
    const nextPath = next || (location.pathname + location.search) || '/';
    // В Android-приложении (WebView) Google/Яндекс НЕЛЬЗЯ открывать внутри WebView
    // (провайдер блокирует «disallowed_useragent»). Отдаём нативному мосту: он
    // откроет провайдера во внешнем браузере (Custom Tab) и вернёт сессию обратно
    // в приложение через deep-link. Если моста нет — обычный веб-флоу.
    if (window.kartochka && typeof window.kartochka.startOAuth === 'function') {
      try { window.kartochka.startOAuth(provider, nextPath, ref || ''); return; }
      catch (e) { /* падать не на чем — уходим в веб-флоу ниже */ }
    }
    const params = new URLSearchParams();
    params.set('next', nextPath);
    if (ref) params.set('ref', ref);
    location.href = API + '/oauth/' + encodeURIComponent(provider) + '/start?' + params.toString();
  }
  // Считать и очистить ?auth_error= (после неудачного соц-входа). Возвращает
  // человекочитаемую причину или null. Хост-страница сама решает, как показать.
  function consumeAuthError() {
    try {
      const u = new URL(location.href);
      const code = u.searchParams.get('auth_error');
      if (!code) return null;
      u.searchParams.delete('auth_error');
      history.replaceState(null, '', u.pathname + (u.search || '') + (u.hash || ''));
      const MAP = {
        cancelled: 'Вход отменён',
        state: 'Сессия входа истекла — попробуйте ещё раз',
        exchange: 'Провайдер не подтвердил вход — попробуйте ещё раз',
        noprofile: 'Не удалось получить профиль у провайдера',
        account: 'Не удалось создать аккаунт',
      };
      return MAP[code] || 'Не удалось войти';
    } catch (e) { return null; }
  }

  // ── reusable auth modal ───────────────────────────────────────────────────
  let _modalEl = null, _afterAuth = null;

  function ensureStyles() {
    if (document.getElementById('wbnacc-styles')) return;
    const css = `
.wbnacc-back{position:fixed;inset:0;z-index:10000;background:rgba(31,26,53,.55);
  backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center;padding:20px;}
.wbnacc-back.show{display:flex;animation:wbnaccFade .25s ease;}
@keyframes wbnaccFade{from{opacity:0}to{opacity:1}}
.wbnacc-modal{background:#fff;border-radius:26px;width:100%;max-width:430px;
  padding:34px 32px;box-shadow:0 30px 80px rgba(60,50,110,.4);position:relative;
  font-family:'Inter',system-ui,sans-serif;color:#1F1A35;animation:wbnaccPop .35s cubic-bezier(.22,1,.36,1);}
@keyframes wbnaccPop{from{transform:scale(.92);opacity:0}to{transform:scale(1);opacity:1}}
.wbnacc-x{position:absolute;top:16px;right:16px;background:none;border:none;cursor:pointer;
  color:#9aa0ad;padding:6px;border-radius:8px;line-height:0;}
.wbnacc-x:hover{background:#f1f0f7;color:#1F1A35;}
.wbnacc-logo{display:flex;align-items:center;gap:9px;justify-content:center;margin-bottom:6px;}
.wbnacc-logo svg{color:#4C29D3;}
.wbnacc-title{font-family:'Fraunces',Georgia,serif;font-weight:500;font-size:24px;text-align:center;letter-spacing:-.01em;}
.wbnacc-title em{font-style:italic;color:#4C29D3;}
.wbnacc-sub{text-align:center;color:rgba(31,26,53,.62);font-size:13px;margin:4px 0 20px;}
.wbnacc-tabs{display:flex;background:#f1f0f7;border-radius:12px;padding:4px;margin-bottom:18px;}
.wbnacc-tab{flex:1;padding:9px;border:none;background:none;border-radius:9px;cursor:pointer;
  font-family:inherit;font-weight:600;font-size:13.5px;color:rgba(31,26,53,.6);transition:.15s;}
.wbnacc-tab.active{background:#fff;color:#4C29D3;box-shadow:0 2px 8px rgba(60,50,110,.1);}
.wbnacc-field{margin-bottom:13px;}
.wbnacc-field label{display:block;font-size:11px;font-weight:700;color:rgba(31,26,53,.6);
  text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px;}
.wbnacc-inp{width:100%;padding:12px 14px;border:1px solid #e4e2ee;border-radius:11px;
  font-family:inherit;font-size:14.5px;background:#fbfafe;transition:.15s;}
.wbnacc-inp:focus{outline:none;border-color:#4C29D3;background:#fff;box-shadow:0 0 0 3px rgba(76,41,211,.12);}
.wbnacc-pass-wrap{position:relative;}
.wbnacc-pass-wrap .wbnacc-inp{padding-right:44px;}
.wbnacc-eye{position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;
  cursor:pointer;padding:7px;color:rgba(31,26,53,.5);display:flex;align-items:center;justify-content:center;
  border-radius:7px;transition:.15s;}
.wbnacc-eye:hover{color:#4C29D3;background:rgba(76,41,211,.08);}
.wbnacc-eye:focus{outline:none;color:#4C29D3;background:rgba(76,41,211,.12);}
.wbnacc-consent{display:flex;gap:9px;align-items:flex-start;margin:6px 0 4px;font-size:12px;color:rgba(31,26,53,.7);line-height:1.45;}
.wbnacc-consent input{margin-top:2px;width:16px;height:16px;accent-color:#4C29D3;flex-shrink:0;}
.wbnacc-consent a{color:#4C29D3;text-decoration:none;}
.wbnacc-consent a:hover{text-decoration:underline;}
.wbnacc-btn{width:100%;padding:14px;border:none;border-radius:13px;cursor:pointer;margin-top:14px;
  background:linear-gradient(135deg,#6B4FE0,#4C29D3);color:#fff;font-family:inherit;font-weight:700;
  font-size:15px;display:flex;align-items:center;justify-content:center;gap:9px;transition:.15s;}
.wbnacc-btn:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 10px 26px rgba(76,41,211,.4);}
.wbnacc-btn:disabled{opacity:.6;cursor:default;}
.wbnacc-err{color:#d13b53;font-size:12.5px;text-align:center;margin-top:11px;min-height:1px;display:none;}
.wbnacc-err.show{display:block;}
.wbnacc-refnote{background:rgba(98,191,164,.12);border:1px solid rgba(98,191,164,.3);border-radius:10px;
  padding:9px 12px;font-size:12px;color:#1F1A35;margin-bottom:14px;display:flex;gap:8px;align-items:center;}
.wbnacc-spin{width:17px;height:17px;border:2.4px solid rgba(255,255,255,.4);border-top-color:#fff;
  border-radius:50%;animation:wbnaccSpin .7s linear infinite;}
@keyframes wbnaccSpin{to{transform:rotate(360deg)}}
/* ── social login ── */
.wbnacc-social{display:flex;flex-direction:column;gap:9px;margin-bottom:4px;}
.wbnacc-soc{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;
  padding:12px 14px;border-radius:13px;cursor:pointer;font-family:inherit;font-weight:600;
  font-size:14.5px;border:1px solid #e4e2ee;background:#fff;color:#1F1A35;
  transition:transform .14s,box-shadow .14s,border-color .14s,background .14s;-webkit-tap-highlight-color:transparent;}
.wbnacc-soc:hover{transform:translateY(-1px);box-shadow:0 8px 22px rgba(60,50,110,.16);}
.wbnacc-soc:active{transform:translateY(0);}
.wbnacc-soc svg, .wbnacc-soc img{width:20px;height:20px;flex-shrink:0;display:block;}
.wbnacc-soc--google{background:#fff;border-color:#e4e2ee;color:#1F1A35;}
.wbnacc-soc--yandex{background:#fff;border-color:#e9e2e2;color:#1F1A35;}
.wbnacc-soc--yandex .wbnacc-soc-ic{width:22px;height:22px;border-radius:6px;background:#FC3F1D;
  display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:14px;font-family:Arial,sans-serif;}
.wbnacc-divider{display:flex;align-items:center;gap:12px;margin:16px 0 14px;
  color:rgba(31,26,53,.42);font-size:12px;font-weight:600;}
.wbnacc-divider::before,.wbnacc-divider::after{content:'';flex:1;height:1px;background:#ece9f4;}
.wbnacc-legal-note{font-size:11px;color:rgba(31,26,53,.5);text-align:center;line-height:1.45;margin:2px 2px 0;}
.wbnacc-legal-note a{color:#4C29D3;text-decoration:none;}
.wbnacc-legal-note a:hover{text-decoration:underline;}`;
    const st = document.createElement('style');
    st.id = 'wbnacc-styles'; st.textContent = css;
    document.head.appendChild(st);
  }

  function buildModal() {
    ensureStyles();
    const ref = storedRef();
    const back = document.createElement('div');
    back.className = 'wbnacc-back';
    back.innerHTML = `
      <div class="wbnacc-modal" role="dialog" aria-modal="true">
        <button class="wbnacc-x" aria-label="Закрыть">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
        <div class="wbnacc-logo" style="background:transparent;padding:0;">
          <img src="/static/assets/logo.png" alt="карточка Ai" width="44" height="44" style="display:block;border-radius:10px;">
        </div>
        <div class="wbnacc-title" id="wbnaccTitle">С возвращением</div>
        <div class="wbnacc-sub" id="wbnaccSub">Войдите, чтобы создавать карточки</div>
        <div class="wbnacc-tabs">
          <button class="wbnacc-tab active" data-mode="login">Вход</button>
          <button class="wbnacc-tab" data-mode="register">Регистрация</button>
        </div>
        <div class="wbnacc-social" id="wbnaccSocial" style="display:none;"></div>
        <div class="wbnacc-divider" id="wbnaccDivider" style="display:none;">или по почте</div>
        <form id="wbnaccForm" novalidate>
          ${ref ? `<div class="wbnacc-refnote" id="wbnaccRefNote">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#62BFA4" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
            Вы по приглашению — на счёт зачислим бонусные карточки</div>` : ''}
          <div class="wbnacc-field" id="wbnaccNameField" style="display:none;">
            <label>Имя (необязательно)</label>
            <input class="wbnacc-inp" id="wbnaccName" autocomplete="name" placeholder="Как к вам обращаться">
          </div>
          <div class="wbnacc-field">
            <label>Email</label>
            <input class="wbnacc-inp" id="wbnaccEmail" type="email" autocomplete="email" placeholder="you@example.com" required>
          </div>
          <div class="wbnacc-field" id="wbnaccPassField">
            <label>Пароль</label>
            <div class="wbnacc-pass-wrap">
              <input class="wbnacc-inp" id="wbnaccPass" type="password" autocomplete="current-password" placeholder="минимум 6 символов" required>
              <button type="button" class="wbnacc-eye" id="wbnaccEye" aria-label="Показать пароль" aria-pressed="false" tabindex="-1">
                <svg id="wbnaccEyeOn" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                <svg id="wbnaccEyeOff" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:none;"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
              </button>
            </div>
          </div>
          <div id="wbnaccForgotRow" style="text-align:right;margin:-6px 0 8px;">
            <a href="#" id="wbnaccForgot" style="font-size:12px;color:#4C29D3;text-decoration:none;">Забыли пароль?</a>
          </div>
          <div id="wbnaccResetNote" style="display:none;font-size:12.5px;color:rgba(31,26,53,0.72);margin-bottom:8px;line-height:1.45;"></div>
          <div id="wbnaccConsents" style="display:none;">
            <label class="wbnacc-consent">
              <input type="checkbox" id="wbnaccC1">
              <span>Даю <a href="/legal#consent" target="_blank" rel="noopener" onclick="event.stopPropagation()">согласие на обработку персональных данных</a></span>
            </label>
            <label class="wbnacc-consent">
              <input type="checkbox" id="wbnaccC2">
              <span>Принимаю <a href="/legal#offer" target="_blank" rel="noopener" onclick="event.stopPropagation()">условия оферты</a> и <a href="/legal#privacy" target="_blank" rel="noopener" onclick="event.stopPropagation()">политику конфиденциальности</a></span>
            </label>
          </div>
          <button class="wbnacc-btn" id="wbnaccSubmit" type="submit"><span id="wbnaccBtnLabel">Войти</span></button>
          <div class="wbnacc-err" id="wbnaccErr"></div>
          <div id="wbnaccBackRow" style="display:none;text-align:center;margin-top:12px;">
            <a href="#" id="wbnaccBack" style="font-size:12.5px;color:rgba(31,26,53,0.6);text-decoration:none;">← Назад ко входу</a>
          </div>
        </form>
      </div>`;
    document.body.appendChild(back);

    let mode = 'login';
    const q = sel => back.querySelector(sel);
    const setMode = m => {
      mode = m;
      back.querySelectorAll('.wbnacc-tab').forEach(t =>
        t.classList.toggle('active', t.dataset.mode === m));
      const isReset = m === 'reset', isReg = m === 'register';
      q('#wbnaccTitle').innerHTML = isReset ? 'Сброс <em>пароля</em>'
        : (m === 'login' ? 'С возвращением' : 'Создать <em>аккаунт</em>');
      q('#wbnaccSub').textContent = isReset ? 'Пришлём ссылку для нового пароля на вашу почту'
        : (m === 'login' ? 'Войдите, чтобы создавать карточки' : 'Регистрация за 10 секунд — без карты');
      q('#wbnaccNameField').style.display = isReg ? '' : 'none';
      q('#wbnaccConsents').style.display = isReg ? '' : 'none';
      q('#wbnaccPassField').style.display = isReset ? 'none' : '';
      q('#wbnaccForgotRow').style.display = (m === 'login') ? '' : 'none';
      q('#wbnaccResetNote').style.display = 'none';
      q('#wbnaccBackRow').style.display = isReset ? '' : 'none';
      q('#wbnaccSubmit').style.display = '';
      q('#wbnaccBtnLabel').textContent = isReset ? 'Получить ссылку'
        : (m === 'login' ? 'Войти' : 'Создать аккаунт');
      q('#wbnaccPass').setAttribute('autocomplete', m === 'login' ? 'current-password' : 'new-password');
      if (typeof updateSocialVisibility === 'function') updateSocialVisibility();
      showErr('');
    };
    const showErr = msg => {
      const el = q('#wbnaccErr'); el.textContent = msg || '';
      el.classList.toggle('show', !!msg);
    };

    // ── social login buttons ──────────────────────────────────────────────────
    let _socList = [];
    const SOC_ICON = {
      google: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="#4285F4" d="M23.49 12.27c0-.79-.07-1.54-.19-2.27H12v4.51h6.47a5.54 5.54 0 0 1-2.4 3.64v3.02h3.88c2.27-2.09 3.54-5.17 3.54-8.9z"/><path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.88-3.02c-1.08.72-2.45 1.16-4.05 1.16-3.11 0-5.75-2.1-6.69-4.93H1.3v3.1A12 12 0 0 0 12 24z"/><path fill="#FBBC05" d="M5.31 14.3a7.2 7.2 0 0 1 0-4.6V6.6H1.3a12 12 0 0 0 0 10.8z"/><path fill="#EA4335" d="M12 4.77c1.76 0 3.34.61 4.59 1.8l3.43-3.43C17.95 1.18 15.24 0 12 0A12 12 0 0 0 1.3 6.6l4.01 3.1C6.25 6.87 8.89 4.77 12 4.77z"/></svg>',
      yandex: '<span class="wbnacc-soc-ic">Я</span>',
    };
    const SOC_LABEL = { google: 'Google', yandex: 'Яндекс' };
    const renderSocial = async () => {
      let list = [];
      try { list = await oauthProviders(); } catch (e) { list = []; }
      _socList = list || [];
      const host = q('#wbnaccSocial');
      if (!host) return;
      if (!_socList.length) { updateSocialVisibility(); return; }
      host.innerHTML = _socList.map(p =>
        `<button type="button" class="wbnacc-soc wbnacc-soc--${p.id}" data-prov="${p.id}" data-configured="${p.configured === false ? '0' : '1'}">
           ${SOC_ICON[p.id] || ''}<span>Войти через ${SOC_LABEL[p.id] || p.label || p.id}</span>
         </button>`).join('') +
        `<div class="wbnacc-legal-note">Продолжая, вы принимаете <a href="/legal#offer" target="_blank" rel="noopener" onclick="event.stopPropagation()">оферту</a> и <a href="/legal#consent" target="_blank" rel="noopener" onclick="event.stopPropagation()">согласие на обработку ПДн</a></div>`;
      host.querySelectorAll('.wbnacc-soc').forEach(b =>
        b.addEventListener('click', () => {
          // Кнопка показана без ключей провайдера → не редиректим на /start
          // (он отдаст 404), а явно подсказываем ПРЯМО В МОДАЛКЕ (видно сразу).
          if (b.dataset.configured === '0') {
            const nm = SOC_LABEL[b.dataset.prov] || b.dataset.prov;
            showErr('Вход через ' + nm + ' ещё не подключён — нужно добавить ключи провайдера (client_id/secret) в config.json → oauth. Пока войдите по почте.');
            return;
          }
          oauthStart(b.dataset.prov);
        }));
      updateSocialVisibility();
    };
    const updateSocialVisibility = () => {
      const show = _socList.length > 0 && mode !== 'reset';
      const soc = q('#wbnaccSocial'), div = q('#wbnaccDivider');
      if (soc) soc.style.display = show ? '' : 'none';
      if (div) div.style.display = show ? '' : 'none';
    };
    renderSocial();

    back.querySelectorAll('.wbnacc-tab').forEach(t =>
      t.addEventListener('click', () => setMode(t.dataset.mode)));
    q('.wbnacc-x').addEventListener('click', closeAuth);
    back.addEventListener('click', e => { if (e.target === back) closeAuth(); });
    q('#wbnaccForgot').addEventListener('click', e => { e.preventDefault(); setMode('reset'); });
    q('#wbnaccBack').addEventListener('click', e => { e.preventDefault(); setMode('login'); });
    q('#wbnaccEye').addEventListener('click', () => {
      const inp = q('#wbnaccPass'), btn = q('#wbnaccEye');
      const show = inp.type === 'password';
      inp.type = show ? 'text' : 'password';
      btn.setAttribute('aria-pressed', show ? 'true' : 'false');
      btn.setAttribute('aria-label', show ? 'Скрыть пароль' : 'Показать пароль');
      q('#wbnaccEyeOn').style.display  = show ? 'none' : '';
      q('#wbnaccEyeOff').style.display = show ? '' : 'none';
    });

    q('#wbnaccForm').addEventListener('submit', async e => {
      e.preventDefault();
      const email = q('#wbnaccEmail').value.trim();
      const pass = q('#wbnaccPass').value;
      if (!email || !email.includes('@')) { showErr('Введите корректный email'); return; }
      if (mode === 'reset') {
        const rbtn = q('#wbnaccSubmit'); rbtn.disabled = true;
        q('#wbnaccBtnLabel').innerHTML = '<span class="wbnacc-spin"></span>';
        try {
          const r = await requestPasswordReset(email);
          rbtn.style.display = 'none';
          const note = q('#wbnaccResetNote'); note.style.display = '';
          // НИКОГДА не печатаем r.reset_link в UI — даже в dev/stub-режиме.
          // Раньше ссылка вылезала в модалку, если SMTP был не настроен →
          // кто угодно у экрана получал takeover-токен на чужой email.
          // В dev смотреть link нужно в server-логе.
          note.textContent = 'Если аккаунт с таким email существует — мы отправили на него ссылку для смены пароля. Проверьте почту.';
        } catch (ex) {
          rbtn.disabled = false; q('#wbnaccBtnLabel').textContent = 'Получить ссылку';
          showErr(ex.message || 'Ошибка');
        }
        return;
      }
      if (!pass || pass.length < 6) { showErr('Пароль минимум 6 символов'); return; }
      if (mode === 'register' && (!q('#wbnaccC1').checked || !q('#wbnaccC2').checked)) {
        showErr('Нужно согласие на обработку ПДн и принятие оферты'); return;
      }
      const btn = q('#wbnaccSubmit'); btn.disabled = true;
      const lbl = q('#wbnaccBtnLabel').textContent;
      q('#wbnaccBtnLabel').innerHTML = '<span class="wbnacc-spin"></span>';
      try {
        if (mode === 'login') {
          await login(email, pass);
        } else {
          await register({
            email, password: pass,
            name: q('#wbnaccName').value.trim() || null,
            consent_pdn: true, consent_offer: true,
            ref_code: storedRef() || null,
          });
        }
        const cb = _afterAuth; _afterAuth = null;
        closeAuth();
        if (cb) cb(_me);
      } catch (ex) {
        btn.disabled = false; q('#wbnaccBtnLabel').textContent = lbl;
        showErr(ex.message || 'Ошибка');
      }
    });

    _modalEl = { back, setMode, q };
    return _modalEl;
  }

  function openAuth(opts) {
    opts = opts || {};
    if (!_modalEl) buildModal();
    _afterAuth = opts.onSuccess || null;
    _modalEl.setMode(opts.mode || 'login');
    _modalEl.back.classList.add('show');
    document.body.style.overflow = 'hidden';
    setTimeout(() => { const i = _modalEl.q('#wbnaccEmail'); if (i) i.focus(); }, 120);
  }
  function closeAuth() {
    if (_modalEl) _modalEl.back.classList.remove('show');
    document.body.style.overflow = '';
  }

  /** Ensure logged in; resolves with user or opens auth modal. */
  function requireAuth(onSuccess, opts) {
    me().then(u => {
      if (u) { onSuccess && onSuccess(u); }
      else openAuth(Object.assign({ onSuccess, mode: 'register' }, opts || {}));
    });
  }

  captureRef();

  // Базовый домен для реф-ссылок (из config.site_base_url; fallback — текущий origin).
  function siteBaseUrl() {
    const b = _pricing && _pricing.site_base_url;
    return (b && b.replace(/\/$/, '')) || location.origin;
  }

  window.Account = {
    me, onMe, current, pricing, quote, quoteLocal, login, register, logout, applyPromo,
    requestPasswordReset, confirmPasswordReset,
    createPurchase, history, referral, updateAccount, deleteAccount,
    openAuth, closeAuth, requireAuth,
    oauthProviders, oauthStart, consumeAuthError,
    fmt, declCards, siteBaseUrl, storedRef,
  };
})();
