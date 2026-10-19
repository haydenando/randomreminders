// Debug helpers for local testing of the mini app
// Replaces the Save button handler so we can show a helpful error when no user_id is present

function getUserIdFromPage() {
  const tg = window.Telegram?.WebApp;
  return tg?.initDataUnsafe?.user?.id ?? new URLSearchParams(location.search).get("user_id") ?? "";
}

function showToast(msg, type = "err") {
  const el = document.querySelector('#toast');
  if (!el) return;
  el.textContent = msg;
  el.className = `toast show ${type}`;
  setTimeout(() => { el.className = 'toast'; }, 2200);
}

window.addEventListener('DOMContentLoaded', () => {
  const btn = document.querySelector('#btn-save');
  if (!btn) return;
  // Replace the button to remove existing listeners and add our own
  const clone = btn.cloneNode(true);
  clone.addEventListener('click', async (e) => {
    const uid = getUserIdFromPage();
    if (!uid) {
      showToast('Missing user_id. Open this page with ?user_id=YOUR_TELEGRAM_ID (use @userinfobot to get your id).', 'err');
      return;
    }
    // Call the original saveForm defined in app.js
    try {
      if (typeof window.saveForm === 'function') {
        await window.saveForm();
      } else {
        // Try to call the function by name (in case it's not on window)
        const fn = window['saveForm'];
        if (typeof fn === 'function') await fn();
        else showToast('saveForm() not available', 'err');
      }
    } catch (err) {
      showToast(err.message || String(err), 'err');
    }
  });
  btn.parentNode.replaceChild(clone, btn);
});
