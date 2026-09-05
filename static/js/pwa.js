// Registers the service worker and wires up the optional "Install" button.
// Chrome also shows its own install control in the omnibox; the button just
// makes it discoverable. It stays hidden unless the browser offers a prompt.

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((err) => {
      console.warn('Service worker registration failed', err);
    });
  });
}

let deferredPrompt = null;
const installBtn = document.getElementById('installAppBtn');

window.addEventListener('beforeinstallprompt', (event) => {
  // Keep the event so the install can be triggered from our own button.
  event.preventDefault();
  deferredPrompt = event;
  if (installBtn) installBtn.hidden = false;
});

if (installBtn) {
  installBtn.addEventListener('click', async () => {
    if (!deferredPrompt) return;
    installBtn.disabled = true;
    deferredPrompt.prompt();
    await deferredPrompt.userChoice;
    // A prompt can only be used once.
    deferredPrompt = null;
    installBtn.hidden = true;
    installBtn.disabled = false;
  });
}

window.addEventListener('appinstalled', () => {
  deferredPrompt = null;
  if (installBtn) installBtn.hidden = true;
});
