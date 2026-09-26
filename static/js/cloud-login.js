/* Keep no-referrer and exact Origin: a CORS-mode same-origin POST, not a navigation POST. */
(function () {
  'use strict';
  const form = document.getElementById('cloud-login');
  const button = document.getElementById('cloud-login-button');
  const status = document.getElementById('cloud-login-status');
  button.disabled = false;
  form.addEventListener('submit', async event => {
    event.preventDefault(); button.disabled = true;
    status.textContent = 'Opening Google sign-in…';
    try {
      const response = await fetch('/oauth/login', { method:'POST', mode:'cors',
        credentials:'same-origin', cache:'no-store', redirect:'error',
        headers:{'Accept':'application/json'}, body:new URLSearchParams(new FormData(form)) });
      if (!response.ok) throw new Error('login_unavailable');
      const data = await response.json();
      const target = new URL(data.authorization_url);
      if (target.origin !== 'https://accounts.google.com' || target.pathname !== '/o/oauth2/v2/auth')
        throw new Error('invalid_login_target');
      window.location.assign(target.href);
    } catch (_) {
      status.textContent = 'Could not start sign-in. Reload this page to try again.';
    }
  });
})();
