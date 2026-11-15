/* Automações — the tab in /painel where the team turns automations on, edits
   their messages and approves what goes out. Names and reasons come from
   strangers on Telegram, so everything here is written with textContent. */
(function () {
  "use strict";

  var KINDS = {
    reactivate: {
      title: "Reativar inativos",
      desc: "Quem não conversa com vocês há algum tempo recebe uma mensagem para retomar o contato.",
      days: "Dias sem conversa"
    },
    stalled: {
      title: "Parou no meio",
      desc: "O agente respondeu por último e a pessoa sumiu antes de marcar, ou depois da proposta. A mensagem pergunta o motivo, com botões.",
      days: "Dias sem resposta"
    },
    no_show: {
      title: "Não compareceu",
      desc: "Quando a equipe marca “Não compareceu” no grupo, a pessoa recebe a oferta de um novo horário.",
      days: "Dias depois da falta"
    },
    no_proposal: {
      title: "Conversa sem proposta",
      desc: "Avisa só a equipe, no grupo, quando uma conversa aconteceu e o lead continua em “agendado”. Nada é enviado ao cliente.",
      days: "Dias depois da conversa"
    }
  };
  var REASONS = { preco: "Preço", avaliando: "Ainda avaliando", momento: "Não é o momento", outro: "Outro motivo" };

  var loaded = false;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function api(path, options) {
    var opts = options || {};
    return fetch("/painel/api/automations" + path, {
      method: opts.method || "GET",
      credentials: "same-origin",
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (r) {
      if (r.status === 401) {
        if (window.PanelApp) window.PanelApp.signOut();
        else location.replace("/painel/entrar");
        throw new Error("Sessão expirada.");
      }
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var detail = data && data.detail;
          throw new Error(Array.isArray(detail) ? "Confira os campos." : (detail || "Erro " + r.status));
        }
        return data;
      });
    });
  }

  function load() {
    return api("").then(render).catch(function (err) {
      var list = document.getElementById("auto-list");
      list.textContent = "";
      list.appendChild(el("p", "empty-line", "Não foi possível carregar as automações: " + err.message));
    });
  }

  function render(data) {
    loaded = true;
    renderQueue(data.queue || []);
    renderList(data.automations || []);
    renderReasons(data.reasons || {});
    renderTemplates(data.templates || [], data.whatsapp_ready !== false);
  }

  // ── WhatsApp templates ──────────────────────────────────────────────────
  // Active = named in the server's .env with WhatsApp set up; Meta's own
  // approval is checked in WhatsApp Manager, not here.
  function renderTemplates(items, whatsappReady) {
    var box = document.getElementById("auto-templates");
    box.textContent = "";
    box.appendChild(el("p", "tpl-note", "Ativo = nome configurado neste servidor. Se a Meta aprovou o modelo, confira no WhatsApp Manager."));
    if (!whatsappReady) {
      box.appendChild(el("p", "empty-line", "O WhatsApp não está configurado neste servidor: nenhum modelo sai até ele estar."));
    }
    // Without the channel, a named template is still listed under its name.
    var shown = items.filter(function (t) { return t.active || (!whatsappReady && t.name); });
    var missing = items.filter(function (t) { return !t.name; });
    if (whatsappReady && !shown.length) {
      box.appendChild(el("p", "empty-line", "Nenhum modelo ativo neste servidor."));
    }
    shown.forEach(function (t) {
      var row = el("div", "tpl-row");
      var head = el("div", "tpl-head");
      head.appendChild(el("strong", null, t.label));
      head.appendChild(el("code", "tpl-name", t.name || ""));
      if (t.manual) head.appendChild(el("span", "tpl-tag", "Também na Inbox"));
      row.appendChild(head);
      row.appendChild(el("p", "tpl-purpose", t.purpose));
      box.appendChild(row);
    });
    // Apart from the active list: these do not go out until named in .env.
    if (missing.length) {
      var rest = el("div", "tpl-missing");
      rest.appendChild(el("strong", "tpl-missing-title", "Ainda não configurados"));
      rest.appendChild(el("p", "tpl-purpose", missing.map(function (t) { return t.label; }).join(", ") + "."));
      box.appendChild(rest);
    }
  }

  // ── The approval queue ──────────────────────────────────────────────────
  function renderQueue(queue) {
    var box = document.getElementById("auto-queue");
    var count = document.getElementById("count-auto");
    count.textContent = queue.length;
    count.hidden = queue.length === 0;
    document.getElementById("auto-queue-hint").textContent =
      queue.length ? queue.length + " na fila" : "";
    box.textContent = "";
    if (!queue.length) {
      box.appendChild(el("p", "empty-line", "Nada aguardando. Quando uma automação ligada encontrar alguém, a mensagem aparece aqui para você aprovar."));
      return;
    }
    queue.forEach(function (item) {
      var row = el("label", "queue-row");
      var check = el("input");
      check.type = "checkbox";
      check.value = item.id;
      check.checked = true;
      row.appendChild(check);
      row.appendChild(el("span", "queue-name", item.name || "Sem nome"));
      row.appendChild(el("span", "queue-kind", (KINDS[item.kind] || {}).title || item.kind));
      row.appendChild(el("span", "queue-why", item.reason || ""));
      box.appendChild(row);
    });
    var actions = el("div", "queue-actions");
    var approve = el("button", "btn btn-accent", "Aprovar selecionadas");
    var discard = el("button", "btn", "Descartar selecionadas");
    var note = el("span", "auto-msg");
    [approve, discard].forEach(function (button) { button.type = "button"; });
    approve.addEventListener("click", function () { decide(true, note); });
    discard.addEventListener("click", function () { decide(false, note); });
    actions.appendChild(approve);
    actions.appendChild(discard);
    actions.appendChild(note);
    box.appendChild(actions);
  }

  function decide(approve, note) {
    var ids = Array.prototype.map.call(
      document.querySelectorAll("#auto-queue input[type=checkbox]:checked"),
      function (input) { return Number(input.value); }
    );
    if (!ids.length) { note.className = "auto-msg err"; note.textContent = "Selecione ao menos uma."; return; }
    api("/queue", { method: "POST", body: { ids: ids, approve: approve } })
      .then(function (data) {
        render(data);
        var box = document.getElementById("auto-queue");
        var done = el("p", "auto-msg ok", approve
          ? data.changed + " aprovada(s). Saem no próximo horário comercial."
          : data.changed + " descartada(s).");
        box.insertBefore(done, box.firstChild);
      })
      .catch(function (err) { note.className = "auto-msg err"; note.textContent = err.message; });
  }

  // ── One card per automation ─────────────────────────────────────────────
  function renderList(items) {
    var list = document.getElementById("auto-list");
    list.textContent = "";
    items.forEach(function (item) { list.appendChild(card(item)); });
  }

  function field(label, control, grow) {
    var wrap = el("label", "auto-field" + (grow ? " grow" : ""));
    wrap.appendChild(document.createTextNode(label));
    wrap.appendChild(control);
    return wrap;
  }

  function card(item) {
    var info = KINDS[item.kind] || { title: item.kind, desc: "", days: "Dias" };
    var clientFacing = item.kind !== "no_proposal";
    var box = el("div", "card auto-card");
    var head = el("div", "card-head");
    head.appendChild(el("h2", null, info.title));
    var toggle = el("label", "switch");
    var enabled = el("input");
    enabled.type = "checkbox";
    enabled.checked = !!item.enabled;
    enabled.id = "auto-enabled-" + item.id;
    toggle.appendChild(enabled);
    // Both words sit in the same cell, one hidden, so the label keeps the
    // width of the longer one and the switch never shifts.
    var words = el("span", "switch-label");
    words.appendChild(el("span", "on", "Ligada"));
    words.appendChild(el("span", "off", "Desligada"));
    toggle.appendChild(words);
    head.appendChild(toggle);
    box.appendChild(head);

    var body = el("div", "card-body");
    box.appendChild(body);
    body.appendChild(el("p", "auto-desc", info.desc));

    var row = el("div", "auto-row");
    var days = el("input");
    days.type = "number"; days.min = 1; days.max = 365; days.value = item.days;
    days.id = "auto-days-" + item.id;
    row.appendChild(field(info.days, days));
    var mode = null;
    if (clientFacing) {
      mode = el("select");
      mode.id = "auto-mode-" + item.id;
      [["approve", "Aprovar antes de enviar"], ["auto", "Enviar sozinho"]].forEach(function (option) {
        var o = el("option", null, option[1]);
        o.value = option[0];
        o.selected = item.mode === option[0];
        mode.appendChild(o);
      });
      row.appendChild(field("Modo", mode));
    }
    body.appendChild(row);

    var message = null;
    if (clientFacing) {
      message = el("textarea");
      message.id = "auto-message-" + item.id;
      message.maxLength = 1000;
      message.value = item.message || "";
      body.appendChild(field("Mensagem ({nome} vira o primeiro nome da pessoa; o “responda SAIR” entra sozinho no fim)", message, true));
    }

    body.appendChild(el("div", "auto-stats", clientFacing
      ? "Últimos 30 dias: " + item.sent_30d + " enviada(s) · " + item.replied_30d + " respondida(s) · " + item.pending + " aguardando"
      : "Últimos 30 dias: " + item.sent_30d + " aviso(s) no grupo"));

    var actions = el("div", "auto-actions");
    var save = el("button", "btn btn-accent", "Salvar");
    var peek = el("button", "btn", "Quem entraria agora");
    save.type = "button"; peek.type = "button";
    var note = el("span", "auto-msg");
    actions.appendChild(save);
    actions.appendChild(peek);
    actions.appendChild(note);
    body.appendChild(actions);
    var preview = el("div", "preview");
    preview.hidden = true;
    body.appendChild(preview);

    save.addEventListener("click", function () {
      var change = { enabled: enabled.checked, days: Number(days.value) };
      if (mode) change.mode = mode.value;
      if (message) change.message = message.value;
      note.className = "auto-msg"; note.textContent = "Salvando…";
      api("/" + item.id, { method: "PATCH", body: change })
        .then(function (data) {
          render(data);
          var fresh = document.getElementById("auto-enabled-" + item.id);
          var msg = fresh && fresh.closest(".card").querySelector(".auto-msg");
          if (msg) { msg.className = "auto-msg ok"; msg.textContent = "Salvo."; }
        })
        .catch(function (err) { note.className = "auto-msg err"; note.textContent = err.message; });
    });

    peek.addEventListener("click", function () {
      preview.hidden = false;
      preview.textContent = "Procurando…";
      api("/" + item.id + "/preview").then(function (data) {
        preview.textContent = "";
        var people = data.people || [];
        preview.appendChild(el("b", null, people.length
          ? people.length + (people.length === 50 ? "+" : "") + " pessoa(s) entrariam agora:"
          : "Ninguém entraria agora."));
        people.slice(0, 12).forEach(function (person) {
          preview.appendChild(el("span", null, person.name + " · " + person.reason));
        });
        if (people.length > 12) preview.appendChild(el("span", null, "… e mais " + (people.length - 12)));
      }).catch(function (err) { preview.textContent = err.message; });
    });
    return box;
  }

  // ── Why people stopped ──────────────────────────────────────────────────
  function renderReasons(counts) {
    var box = document.getElementById("auto-reasons");
    box.textContent = "";
    var total = Object.keys(REASONS).reduce(function (sum, key) { return sum + (counts[key] || 0); }, 0);
    if (!total) {
      box.appendChild(el("p", "empty-line", "Ainda sem respostas. Aparecem aqui quando alguém toca num motivo da automação “Parou no meio”."));
      return;
    }
    Object.keys(REASONS).forEach(function (key) {
      var n = counts[key] || 0;
      var row = el("div", "reason-row");
      row.appendChild(el("span", null, REASONS[key]));
      var bar = el("div", "reason-bar");
      var fill = el("span");
      fill.style.width = (n / total * 100) + "%";
      bar.appendChild(fill);
      row.appendChild(bar);
      row.appendChild(el("span", "reason-count", String(n)));
      box.appendChild(row);
    });
  }

  // Loaded when the tab is opened, and kept fresh while it is open.
  var nav = document.querySelector('.nav-item[data-view="automacoes"]');
  function onOpen() { load(); }
  nav.addEventListener("click", onOpen);
  nav.addEventListener("keydown", function (e) { if (e.key === "Enter") onOpen(); });
  if (location.hash === "#automacoes") onOpen();
  // The queue count in the menu is worth knowing from any tab.
  api("").then(function (data) {
    var count = document.getElementById("count-auto");
    count.textContent = (data.queue || []).length;
    count.hidden = !(data.queue || []).length;
  }).catch(function () {});
  setInterval(function () {
    var view = document.getElementById("view-automacoes");
    var editing = document.activeElement && view.contains(document.activeElement);
    if (loaded && view.classList.contains("active") && !editing && !document.hidden) load();
  }, 60000);
})();
