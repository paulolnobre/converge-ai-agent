/* Sign-in and invite pages for /painel. No inline script anywhere: the page's
   Content-Security-Policy allows only this file. */
(function () {
  "use strict";

  var errorBox = document.getElementById("error");

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }

  function post(path, body) {
    return fetch("/painel/api/auth/" + path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var detail = data && data.detail;
          if (Array.isArray(detail)) detail = "Confira os campos preenchidos.";
          throw new Error(detail || ("Erro " + r.status));
        }
        return data;
      });
    });
  }

  function busy(form, on) {
    var button = form.querySelector("button[type=submit]");
    if (button) button.disabled = on;
  }

  // ── /painel/entrar ────────────────────────────────────────────────────────
  function login() {
    var first = document.getElementById("step-password");
    var second = document.getElementById("step-code");

    first.addEventListener("submit", function (event) {
      event.preventDefault();
      showError("");
      busy(first, true);
      post("login", { email: first.email.value.trim(), password: first.password.value,
        remember_device: first.remember_device.checked })
        .then(function () {
          first.hidden = true;
          second.hidden = false;
          second.code.focus();
        })
        .catch(function (err) { showError(err.message); })
        .then(function () { busy(first, false); });
    });

    second.addEventListener("submit", function (event) {
      event.preventDefault();
      showError("");
      busy(second, true);
      post("code", { code: second.code.value.trim() })
        .then(function (data) {
          if (data.recovery_left != null) {
            try { sessionStorage.setItem("painel_recovery_left", String(data.recovery_left)); } catch (e) {}
          }
          // A Telegram button opened /painel#inbox/<id>: land on that conversation.
          location.replace("/painel/" + (/^#inbox\/\d+$/.test(location.hash) ? location.hash : "#inbox"));
        })
        .catch(function (err) {
          showError(err.message);
          second.code.value = "";
          second.code.focus();
          // The password step expires after a few minutes: start over.
          if (/senha primeiro/.test(err.message)) {
            second.hidden = true;
            first.hidden = false;
          }
        })
        .then(function () { busy(second, false); });
    });
  }

  // ── /painel/convite#<token> ───────────────────────────────────────────────
  function invite() {
    // The token rides in the fragment, which the browser never sends to a
    // server or puts in a Referer; it is then wiped from the address bar.
    var token = location.hash.replace(/^#/, "");
    if (history.replaceState) history.replaceState(null, "", location.pathname);
    var loading = document.getElementById("loading");
    var form = document.getElementById("enroll");
    var done = document.getElementById("done");

    if (!token) {
      loading.hidden = true;
      showError("Link de convite incompleto. Peça um novo convite.");
      return;
    }

    post("invite/start", { token: token })
      .then(function (data) {
        loading.hidden = true;
        form.hidden = false;
        document.getElementById("who").textContent = data.name;
        document.getElementById("email-field").value = data.email;
        document.getElementById("qr").src = data.qr;
        document.getElementById("secret").textContent = data.secret.replace(/(.{4})/g, "$1 ").trim();
        document.getElementById("rule").textContent = "(mínimo " + data.min_password + " caracteres)";
      })
      .catch(function (err) {
        loading.hidden = true;
        showError(err.message);
      });

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      showError("");
      if (form.password.value !== form.confirm.value) {
        showError("As senhas não conferem.");
        return;
      }
      busy(form, true);
      post("invite/finish", { token: token, password: form.password.value, code: form.code.value.trim() })
        .then(function (data) {
          token = "";
          form.hidden = true;
          done.hidden = false;
          var list = document.getElementById("codes");
          data.recovery_codes.forEach(function (code) {
            var item = document.createElement("li");
            item.textContent = code;
            list.appendChild(item);
          });
          wireCodes(data.recovery_codes);
        })
        .catch(function (err) { showError(err.message); })
        .then(function () { busy(form, false); });
    });
  }

  function wireCodes(codes) {
    var text = "Converge — códigos de recuperação do painel\n" +
      "Cada código funciona uma única vez.\n\n" + codes.join("\n") + "\n";
    document.getElementById("copy").addEventListener("click", function (event) {
      navigator.clipboard.writeText(text).then(function () {
        event.target.textContent = "Copiados";
      });
    });
    document.getElementById("download").addEventListener("click", function () {
      var link = document.createElement("a");
      link.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
      link.download = "converge-codigos-de-recuperacao.txt";
      link.click();
      URL.revokeObjectURL(link.href);
    });
    var saved = document.getElementById("saved");
    var go = document.getElementById("go");
    saved.addEventListener("change", function () { go.disabled = !saved.checked; });
    go.addEventListener("click", function () { location.replace("/painel/#inbox"); });
  }

  var page = document.body.getAttribute("data-page");
  if (page === "login") login();
  if (page === "invite") invite();
})();
