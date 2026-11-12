// Turns browser alerts on or off for this device in one click. Any button with
// data-alerts-toggle works; an element with data-alerts-status shows the state.
(function () {
  var config = document.querySelector('meta[name="marvin-alerts"]');
  var buttons = document.querySelectorAll('[data-alerts-toggle]');
  if (!config || !buttons.length) return;
  var status = document.querySelector('[data-alerts-status]');
  var registration = null;
  var subscribed = false;

  function show(text, on) {
    subscribed = on;
    buttons.forEach(function (button) {
      button.disabled = false;
      button.textContent = on ? 'Turn off alerts' : 'Turn on alerts';
      button.classList.toggle('btn-primary', !on);
      button.classList.toggle('btn-outline', on);
    });
    if (status && text) status.textContent = text;
  }

  function unavailable(text) {
    buttons.forEach(function (button) { button.disabled = true; });
    if (status) status.textContent = text;
  }

  function save(type, subscription) {
    var agent = navigator.userAgent;
    var browser = (agent.match(/(firefox|msie|chrome|safari|trident)/i) || ['other'])[0].toLowerCase();
    return fetch(config.dataset.saveUrl, {
      method: 'post',
      headers: {'Content-Type': 'application/json'},
      credentials: 'include',
      body: JSON.stringify({
        status_type: type, subscription: subscription.toJSON(), browser: browser, user_agent: agent,
      }),
    }).then(function (response) {
      if (!response.ok) throw new Error('save failed');
    });
  }

  function key() {
    var b64 = config.dataset.vapidKey.replace(/-/g, '+').replace(/_/g, '/');
    b64 += '='.repeat((4 - b64.length % 4) % 4);
    return Uint8Array.from(atob(b64), function (c) { return c.charCodeAt(0); });
  }

  function turnOn() {
    return registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key()})
      .then(function (subscription) { return save('subscribe', subscription); })
      .then(function () { show('Alerts are on for this device.', true); });
  }

  function turnOff() {
    return registration.pushManager.getSubscription().then(function (subscription) {
      if (!subscription) return;
      return save('unsubscribe', subscription).then(function () { return subscription.unsubscribe(); });
    }).then(function () { show('Alerts are off for this device.', false); });
  }

  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    unavailable('This browser cannot show alerts.');
    return;
  }
  if (Notification.permission === 'denied') {
    unavailable('Alerts are blocked for this site. Allow notifications in your browser settings to turn them on.');
    return;
  }

  navigator.serviceWorker.register(config.dataset.workerUrl).then(function (reg) {
    registration = reg;
    return reg.pushManager.getSubscription();
  }).then(function (subscription) {
    // Re-save an existing subscription, so the server knows this device after a reinstall.
    if (subscription) return save('subscribe', subscription).then(function () { show('Alerts are on for this device.', true); });
    show(null, false);
  }).catch(function () { unavailable('Alerts could not be set up in this browser.'); });

  buttons.forEach(function (button) {
    button.addEventListener('click', function () {
      buttons.forEach(function (b) { b.disabled = true; });
      (subscribed ? turnOff() : turnOn()).catch(function () {
        show(Notification.permission === 'denied'
          ? 'Alerts were not allowed. You can change this in your browser settings.'
          : 'Something went wrong. Please try again.', subscribed);
      });
    });
  });
})();
