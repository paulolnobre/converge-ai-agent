/* Installation and updates shared by the sign-in, install and panel pages. */
(function () {
  "use strict";
  var installed = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  var promptEvent = null, leaving = false, refreshing = false, registration = null;
  var installButton = document.getElementById("install-app");
  var remember = document.getElementById("remember-device");
  if (remember) remember.checked = !!installed;
  var retry = document.getElementById("retry-app");
  if (retry) retry.addEventListener("click", function () {
    if (location.pathname === "/painel/offline") location.replace("/painel/#inbox");
    else location.reload();
  });

  function signOut() {
    if (leaving) return;
    leaving = true;
    document.body.classList.add("signed-out");
    if (navigator.serviceWorker && navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage({ type: "CLEAR_PUBLIC_CACHE" });
    }
    var clear = "caches" in window ? caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (key) { return key.indexOf("converge-panel-") === 0; })
        .map(function (key) { return caches.delete(key); }));
    }) : Promise.resolve();
    if (navigator.clearAppBadge) navigator.clearAppBadge().catch(function () {});
    clear.catch(function () {}).then(function () {
      location.replace("/painel/entrar" + (/^#inbox\/\d+$/.test(location.hash) ? location.hash : ""));
    });
  }
  window.PanelApp = { installed: !!installed, signOut: signOut };

  // A fixed screen: Safari's pinch and a trackpad's pinch (ctrl+wheel) do not
  // zoom the app. Larger text will be a panel setting, not a zoom.
  document.addEventListener("gesturestart", function (event) { event.preventDefault(); });
  window.addEventListener("wheel", function (event) {
    if (event.ctrlKey) event.preventDefault();
  }, { passive: false });

  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    promptEvent = event;
    if (installButton && !installed) installButton.hidden = false;
  });
  if (installButton) installButton.addEventListener("click", function () {
    if (!promptEvent) { location.href = "/painel/instalar"; return; }
    var current = promptEvent;
    promptEvent = null;
    current.prompt();
    current.userChoice.then(function () { installButton.hidden = true; });
  });
  window.addEventListener("appinstalled", function () {
    if (installButton) installButton.hidden = true;
    if (remember) remember.checked = true;
    if (registration) registration.update().catch(function () {});
  });

  if (!installed && /iPad|iPhone|iPod/.test(navigator.userAgent)) {
    try {
      if (!localStorage.getItem("converge-install-hint")) {
        var hint = document.createElement("div");
        hint.className = "app-notice";
        var link = document.createElement("a");
        link.href = "/painel/instalar"; link.textContent = "Adicionar a Converge à tela inicial";
        var close = document.createElement("button");
        close.type = "button"; close.textContent = "Agora não";
        close.addEventListener("click", function () { hint.remove(); });
        hint.append(link, close); document.body.appendChild(hint);
        localStorage.setItem("converge-install-hint", "1");
      }
    } catch (e) {}
  }

  function updateAvailable(reg) {
    if (!reg.waiting || document.getElementById("app-update")) return;
    var notice = document.createElement("div");
    notice.id = "app-update"; notice.className = "app-notice";
    var text = document.createElement("span"); text.textContent = "Nova versão disponível";
    var button = document.createElement("button");
    button.type = "button"; button.textContent = "Atualizar";
    button.addEventListener("click", function () {
      if (!reg.waiting) return;
      // Reply drafts stay on the page until the person chooses to update.
      var draft = document.getElementById("thread-text");
      if (draft && draft.value.trim() && !window.confirm("Há uma resposta escrita. Atualizar e descartar o texto?")) return;
      refreshing = true;
      reg.waiting.postMessage({ type: "ACTIVATE_UPDATE" });
    });
    notice.append(text, button); document.body.appendChild(notice);
  }
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("controllerchange", function () {
      if (refreshing) location.reload();
    });
    // A tapped notification switches the open panel to its conversation.
    navigator.serviceWorker.addEventListener("message", function (event) {
      if (!event.data || event.data.type !== "OPEN_URL") return;
      try {
        var url = new URL(event.data.url);
        if (url.origin !== location.origin || url.pathname.indexOf("/painel/") !== 0) return;
        if (url.pathname === location.pathname) location.hash = url.hash;
        else location.href = url.href;
      } catch (e) {}
    });
    navigator.serviceWorker.register("/painel/sw.js", { scope: "/painel/", updateViaCache: "none" })
      .then(function (reg) {
        registration = reg;
        updateAvailable(reg);
        setupPush(reg);
        reg.addEventListener("updatefound", function () {
          var worker = reg.installing;
          if (worker) worker.addEventListener("statechange", function () {
            if (worker.state === "installed" && navigator.serviceWorker.controller) updateAvailable(reg);
          });
        });
      }).catch(function () {}); // Installation support never blocks sign-in.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && registration) registration.update().catch(function () {});
    });
  }

  // ── Push notifications (docs/PUSH.md) ─────────────────────────────────
  // Only on the panel page, only where the browser can, never on its own:
  // a subscription is made on a tap, and only re-sent when it already exists.
  var pushButton = document.getElementById("push-toggle");
  var pushInstall = document.getElementById("push-install");
  function apple() {
    return /iPad|iPhone|iPod/.test(navigator.userAgent) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  }
  function pushApi(method, path, body) {
    return fetch("/painel/api" + path, {
      method: method, credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) {
      if (!r.ok) throw new Error("push " + r.status);
      return r.json();
    });
  }
  function keyBytes(key) {
    var raw = atob((key + "===".slice((key.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  // The worker that answers now must be one that shows notifications.
  function workerHandlesPush(reg) {
    return new Promise(function (resolve) {
      if (reg.waiting || !reg.active || !window.MessageChannel) { resolve(false); return; }
      var channel = new MessageChannel();
      var timer = setTimeout(function () { resolve(false); }, 1000);
      channel.port1.onmessage = function (event) {
        clearTimeout(timer); resolve(!!(event.data && event.data.push));
      };
      reg.active.postMessage({ type: "CAPABILITIES" }, [channel.port2]);
    });
  }
  // The person's "desativar" outlives a failure: until it is done, the app
  // keeps trying to turn it off instead of sending the subscription again.
  var OFF = "converge-push-off";
  function wantsOff() { try { return localStorage.getItem(OFF) === "1"; } catch (e) { return false; } }
  function rememberOff(off) {
    try { if (off) localStorage.setItem(OFF, "1"); else localStorage.removeItem(OFF); } catch (e) {}
  }
  function turnOff(sub) {
    return sub.unsubscribe().then(function () {
      return pushApi("DELETE", "/push/subscribe", { endpoint: sub.endpoint });
    });
  }
  function sendSubscription(sub) {
    var data = sub.toJSON();
    return pushApi("POST", "/push/subscribe", { endpoint: data.endpoint, keys: data.keys });
  }
  function setupPush(reg) {
    if (!pushButton) return;
    if (apple() && !installed) { if (pushInstall) pushInstall.hidden = false; return; }
    if (!("PushManager" in window) || !("Notification" in window) || !reg.pushManager) return;
    if (Notification.permission === "denied") return;
    var publicKey = null, current = null;
    function show(label, disabled) {
      pushButton.textContent = label; pushButton.disabled = !!disabled; pushButton.hidden = false;
    }
    pushApi("GET", "/push/key").then(function (data) {
      publicKey = data.public_key;
      // A first install is still running: wait for the worker to be active.
      return navigator.serviceWorker.ready;
    }).then(function () {
      return workerHandlesPush(reg);
    }).then(function (ready) {
      if (!ready) { show("Atualize o app para ativar notificações", true); return null; }
      return reg.pushManager.getSubscription();
    }).then(function (sub) {
      if (!publicKey || pushButton.disabled) return;
      if (sub && wantsOff()) {
        current = sub;
        return turnOff(sub).then(function () {
          current = null; show("Ativar notificações");
        }, function () { show("Não foi possível desativar · tentar de novo"); });
      }
      current = sub && Notification.permission === "granted" ? sub : null;
      if (current) sendSubscription(current).catch(function () {});
      show(current ? "Notificações ativas · desativar" : "Ativar notificações");
    }).catch(function () { pushButton.hidden = true; });  // off on this server: no item

    pushButton.addEventListener("click", function () {
      if (pushButton.disabled || !publicKey) return;
      pushButton.disabled = true;
      if (current) {
        rememberOff(true);
        turnOff(current).then(function () {
          current = null; show("Ativar notificações");
        }, function () { show("Não foi possível desativar · tentar de novo"); });
        return;
      }
      // Asked right in the tap: iPhone only allows it from a gesture.
      Notification.requestPermission().then(function (answer) {
        if (answer !== "granted") {
          if (answer === "denied") pushButton.hidden = true;
          else show("Ativar notificações");
          return null;
        }
        return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) })
          .then(function (sub) { current = sub; rememberOff(false); return sendSubscription(sub); })
          .then(function () { show("Notificações ativas · desativar"); });
      }).catch(function () { current = null; show("Não foi possível ativar · tentar de novo"); });
    });
  }

  var offline = document.getElementById("offline-status");
  function connection() { if (offline) offline.hidden = navigator.onLine; }
  window.addEventListener("offline", connection);
  window.addEventListener("online", connection);
  window.addEventListener("pageshow", function (event) {
    // A history restore must revalidate the session before showing old DOM.
    if (event.persisted && document.querySelector(".app")) location.reload();
  });
  connection();
})();
