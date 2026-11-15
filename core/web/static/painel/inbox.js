/* Inbox — every conversation in progress, and answering from the panel.
   Names and messages come from strangers, so everything is written with
   textContent. The list refreshes every 10s and an open conversation every
   4s, only while this tab is on screen. */
(function () {
  "use strict";

  var FILTERS = [["all", "Todas"], ["waiting", "Esperando"], ["mine", "Comigo"], ["bot", "Bot"], ["archived", "Arquivadas"], ["blocked", "Bloqueadas"]];
  var CHANNELS = { telegram: "Telegram", whatsapp: "WhatsApp", instagram: "Instagram", messenger: "Facebook", email: "E-mail" };
  var STAGES = { novo: "Novo lead", respondido: "Bot respondeu", agendado: "Conversa agendada",
                 proposta: "Proposta enviada", fechado: "Fechado", perdido: "Perdido" };
  var LIST_MS = 10000, THREAD_MS = 4000, COUNT_MS = 30000, RECONCILE_EVERY = 8;
  var TOUCH = window.matchMedia("(pointer: coarse)").matches;

  var filter = "all", channelFilter = "", openId = null, current = null;
  var seen = {};            // message id -> message, for the open conversation
  var originDirty = false;
  var listedRows = [];
  var uploadsEnabled = false;
  var attachments = window.PanelAttachments.create(document.getElementById("thread-attachments"), {
    caption: function () { return document.getElementById("thread-text").value; },
    url: function () { return "/painel/api/inbox/" + openId + "/attachments"; },
    sent: function (url, caption) { if (url === "/painel/api/inbox/" + openId + "/attachments") { if (document.getElementById("thread-text").value.trim() === caption) document.getElementById("thread-text").value = ""; loadThread(true); loadList(); } },
    refresh: function () { loadThread(true); }
  });
  var ocrEnabled = false, ocrRun = 0, ocrAbort = null;
  var billing = window.PanelBilling.create({
    api: api, note: note,
    applied: function (data) { apply(data, false); loadList(); }
  });

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function button(label, cls, onClick) {
    var b = el("button", cls, label); b.type = "button"; b.addEventListener("click", onClick); return b;
  }

  function api(path, options) {
    var opts = options || {};
    return fetch("/painel/api" + path, {
      method: opts.method || "GET",
      credentials: "same-origin",
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal
    }).then(function (r) {
      if (r.status === 401) {
        if (window.PanelApp) window.PanelApp.signOut();
        else location.replace("/painel/entrar" + (/^#inbox\/\d+$/.test(location.hash) ? location.hash : ""));
        throw new Error("Sessão expirada.");
      }
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var detail = data && data.detail;
          var error = new Error(Array.isArray(detail) ? "Confira a mensagem." : (detail || "Erro " + r.status));
          error.status = r.status;
          throw error;
        }
        return data;
      });
    });
  }

  function when(value) {
    if (!value) return "";
    var d = new Date(value), today = new Date();
    var time = d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === today.toDateString()) return time;
    return d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }) + " " + time;
  }
  function stateText(c) {
    if (c.mine) return "Com você";
    if (c.state === "claimed") return "Com " + (c.owner_name || "alguém");
    return c.state === "waiting" ? "Esperando pessoa" : "Bot";
  }
  function stateClass(c) { return "inbox-state " + c.state + (c.mine ? " mine" : ""); }
  function isActive() { return $("view-inbox").classList.contains("active"); }

  // ── The list ───────────────────────────────────────────────────────────
  function renderFilters() {
    var box = $("inbox-filter"); box.textContent = "";
    FILTERS.forEach(function (f) {
      var chip = button(f[1], "agenda-chip " + (filter === f[0] ? "on" : "off"), function () {
        filter = f[0]; renderFilters(); loadList();
      });
      chip.setAttribute("aria-pressed", String(filter === f[0]));
      box.appendChild(chip);
    });
  }

  function setCount(n) { var c = $("count-inbox"); c.textContent = n; c.hidden = !n; }

  function loadList() {
    var query = "?filter=" + filter + (channelFilter ? "&channel=" + encodeURIComponent(channelFilter) : "");
    return api("/inbox" + query).then(function (data) {
      listedRows = data.conversations || [];
      renderList(listedRows);
      setCount(data.waiting_total || 0);
    }).catch(function (err) {
      var list = $("inbox-list"); list.textContent = "";
      list.appendChild(el("p", "empty-line", "Não foi possível carregar as conversas: " + err.message));
    });
  }

  // A round avatar with the initials, in a colour that stays the same for a
  // name — the list reads like WhatsApp's.
  function initials(name) {
    var parts = (name || "").trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return "?";
    return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
  }
  function hue(name) {
    var h = 0, s = name || "";
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
    return h;
  }
  function avatar(name) {
    var a = el("span", "inbox-avatar", initials(name));
    a.style.setProperty("--avatar-hue", String(hue(name)));
    a.setAttribute("aria-hidden", "true");
    return a;
  }

  function renderList(rows) {
    var query = searchText($("inbox-search").value);
    if (query) rows = rows.filter(function (c) {
      return searchText([c.name, c.display_name, c.phone, c.last_message, c.owner_name, CHANNELS[c.channel] || c.channel]
        .filter(Boolean).join(" ")).indexOf(query) !== -1;
    });
    var list = $("inbox-list"); list.textContent = "";
    $("inbox-empty").textContent = rows.length ? "Escolha uma conversa." : "Nenhuma conversa para mostrar.";
    if (!rows.length) {
      list.appendChild(el("p", "empty-line", query ? "Nenhuma conversa encontrada." : filter === "waiting"
        ? "Ninguém esperando uma pessoa agora."
        : filter === "archived" ? "Nenhuma conversa arquivada."
        : filter === "blocked" ? "Nenhum contato bloqueado."
        : "Nenhuma conversa nos últimos 15 dias. As mais antigas ficam em Arquivadas."));
      return;
    }
    rows.forEach(function (c) {
      var row = button("", "inbox-row" + (c.contact_id === openId ? " active" : ""), function () {
        openConversation(c.contact_id);
      });
      row.dataset.id = String(c.contact_id);
      var body = el("div", "inbox-row-body");
      var top = el("div", "inbox-row-top");
      top.appendChild(el("span", "inbox-name", c.display_name || c.name || "Sem nome"));
      if (c.is_test) top.appendChild(el("span", "test-tag", "TESTE"));
      top.appendChild(el("span", "inbox-when", when(c.last_at)));
      body.appendChild(top);
      var mid = el("div", "inbox-row-mid");
      var preview = [CHANNELS[c.channel] || c.channel || "",
        (c.last_role === "user" ? "" : "↪ ") + (c.last_message || "Sem mensagens")]
        .filter(Boolean).join(" · ");
      mid.appendChild(el("div", "inbox-preview", preview));
      mid.appendChild(el("span", stateClass(c), stateText(c)));
      body.appendChild(mid);
      row.appendChild(avatar(c.name)); row.appendChild(body);
      list.appendChild(row);
    });
  }

  // ── One conversation ───────────────────────────────────────────────────
  function openConversation(id) {
    closeOcr();
    if (openId !== id) { attachments.reset(); billing.reset(); }
    openId = id; current = null; seen = {}; originDirty = false;
    history.replaceState(null, "", "#inbox/" + id);
    $("inbox").classList.add("thread-open");
    $("inbox-empty").hidden = true; $("inbox-thread").hidden = false;
    $("thread-messages").textContent = ""; $("thread-actions").textContent = "";
    clearReopen();
    $("thread-name").textContent = "Carregando…"; $("thread-sub").textContent = "";
    $("thread-origin-msg").textContent = ""; note("", false);
    $("thread-details").open = false;
    // The open conversation is highlighted at once, not at the next refresh.
    Array.prototype.forEach.call(document.querySelectorAll(".inbox-row"), function (r) {
      r.classList.toggle("active", r.dataset.id === String(id));
    });
    loadThread(true);
  }

  function closeConversation() {
    closeOcr();
    attachments.reset(); attachments.setEnabled(false); billing.reset();
    openId = null; current = null;
    history.replaceState(null, "", "#inbox");
    $("inbox").classList.remove("thread-open");
    $("inbox-thread").hidden = true; $("inbox-empty").hidden = false;
    loadList();
  }

  function lastSeenId() {
    var ids = Object.keys(seen).map(Number);
    return ids.length ? Math.max.apply(null, ids) : null;
  }

  // full: a new conversation, start over. reconcile: fetch the last 200 again
  // and merge — a row committed late, behind more than the server's catch-up
  // window, still shows up within RECONCILE_EVERY polls.
  function loadThread(full, reconcile) {
    if (openId == null) return;
    var id = openId, after = (full || reconcile) ? null : lastSeenId();
    api("/inbox/" + id + (after != null ? "?after=" + after : "")).then(function (data) {
      if (id === openId) apply(data, full);
    }).catch(function (err) {
      if (id !== openId) return;
      // A link to a conversation that no longer exists (deleted, or a stale
      // address): back to the list, not a half-open screen with an error.
      if (err.status === 404) { closeConversation(); return; }
      note(err.message, true);
    });
  }

  // The server may send a message again (the overlap of the last ten minutes)
  // or one older than the last seen: merge by id, render in id order.
  function apply(data, full) {
    if (full) seen = {};
    var wasMine = current && current.mine;
    current = data.contact;
    if (full || wasMine !== current.mine) billing.load(openId, current);
    ocrEnabled = !!data.ocr_enabled;
    uploadsEnabled = !!data.uploads_enabled;
    renderHead();
    var fresh = false;
    (data.messages || []).forEach(function (m) {
      var previous = seen[m.id];
      if (!previous || (m.media && previous.media &&
          (m.media.delivery_state !== previous.media.delivery_state || m.media.available !== previous.media.available))) {
        seen[m.id] = m; fresh = true;
      }
    });
    if (fresh || full) renderMessages(full);
  }

  function renderHead() {
    var c = current;
    $("thread-name").textContent = c.display_name || c.name || "Sem nome";
    if (c.is_test) $("thread-name").appendChild(el("span", "test-tag", "TESTE"));
    if (c.blocked) $("thread-name").appendChild(el("span", "test-tag", "BLOQUEADO"));
    renderManage(c);
    $("thread-sub").textContent = [CHANNELS[c.channel] || c.channel, stateText(c)]
      .filter(Boolean).join(" · ");
    $("thread-avatar").textContent = initials(c.name);
    $("thread-avatar").style.setProperty("--avatar-hue", String(hue(c.name)));
    $("thread-contact-details").textContent = [c.phone, c.email, STAGES[c.stage] || c.stage]
      .filter(Boolean).join(" · ");
    var actions = $("thread-actions"); actions.textContent = "";
    var tools = $("thread-tools"); tools.textContent = "";
    tools.appendChild(el("span", stateClass(c), stateText(c)));
    // Asked for a proposal by e-mail (propostas.js): a link to the request.
    var proposal = window.PanelProposals && window.PanelProposals.forContact(c.contact_id);
    if (proposal) {
      var link = el("a", "prop-tag", "Proposta solicitada · " + proposal.status);
      link.title = link.textContent;
      link.href = window.PanelProposals.href(proposal.id);
      tools.appendChild(link);
    }
    // With you: only "Devolver". With the bot (or waiting for a person): only
    // "Assumir". With someone else: nothing.
    if (c.mine) {
      if (c.billing_enabled && (c.window_open !== false || c.charge_after_window)) {
        actions.appendChild(button("Cobrar", "btn", function () { billing.open(); }));
      }
      actions.appendChild(button("Devolver", "btn", function () { act("release"); }));
    } else if (c.state !== "claimed") {
      actions.appendChild(button("Assumir", "btn btn-accent",
        function () { act("claim"); }));
    }

    var origin = $("thread-origin"), detail = $("thread-origin-detail");
    if (!originDirty && document.activeElement !== origin && document.activeElement !== detail) {
      origin.value = c.origin || ""; detail.value = c.origin_detail || "";
    }

    // WhatsApp takes free text only within 24h of the customer's last message.
    var windowClosed = c.window_open === false;
    var canWrite = !!c.mine && !windowClosed;
    attachments.setEnabled(canWrite && uploadsEnabled);
    $("thread-text").disabled = !canWrite;
    $("thread-send").disabled = !canWrite;
    var lock = $("thread-lock");
    lock.hidden = canWrite;
    lock.textContent = windowClosed ? (c.window_notice || "")
      : c.state === "claimed"
        ? "Conversa com " + (c.owner_name || "outra pessoa") + "."
        : "Assuma a conversa para responder.";
    lock.title = lock.textContent;
    renderReopen(c, windowClosed && !!c.mine && !!c.can_reopen);
  }

  // Past 24h the reply box is closed, but an approved template can ask the
  // customer to tap a button and reopen it. With more than one approved, the
  // owner picks which one goes. The bar is rebuilt only when the conversation
  // or the list changes, so the 4s refresh keeps the pick and the focus.
  function clearReopen() {
    var bar = $("thread-reopen");
    bar.textContent = ""; bar.hidden = true; bar.setAttribute("data-sig", "");
  }

  function renderReopen(c, show) {
    var bar = $("thread-reopen");
    var forId = openId;
    var choices = show ? (c.reopen_choices || []) : [];
    var sig = choices.length ? openId + "|" + choices.map(function (x) { return x.key; }).join(",") : "";
    if (bar.getAttribute("data-sig") === sig) return;
    bar.setAttribute("data-sig", sig);
    bar.textContent = "";
    bar.hidden = !choices.length;
    if (!choices.length) return;
    var picker = null;
    if (choices.length > 1) {
      var label = el("label", "thread-reopen-label", c.reopen_choose_label || "Modelo");
      picker = el("select", null);
      picker.id = "thread-reopen-choice";
      label.htmlFor = picker.id;
      choices.forEach(function (choice) {
        var option = el("option", null, choice.label);
        option.value = choice.key;
        picker.appendChild(option);
      });
      bar.appendChild(label);
      bar.appendChild(picker);
    }
    // Optional: written now, it reaches the customer the moment they tap.
    var reply = el("textarea", "thread-reopen-reply");
    reply.id = "thread-reopen-reply";
    reply.rows = 2; reply.maxLength = 4000;
    reply.placeholder = "Resposta (opcional): chega assim que o cliente tocar no botão";
    reply.setAttribute("aria-label", "Resposta enviada quando o cliente tocar no botão");
    bar.appendChild(reply);
    bar.appendChild(button(c.reopen_label, "btn btn-accent", function () {
      if (forId !== openId) return; // drawn for another conversation
      act("reopen", { template: picker ? picker.value : choices[0].key,
                      reply: reply.value.trim() || null });
    }));
  }

  function renderMessages(scrollToEnd) {
    var box = $("thread-messages");
    var atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.textContent = "";
    var ids = Object.keys(seen).map(Number).sort(function (a, b) { return a - b; });
    if (!ids.length) box.appendChild(el("p", "empty-line", "Sem mensagens."));
    ids.forEach(function (id) {
      var m = seen[id];
      var team = !!m.author || m.intent === "relay";
      var who = m.role === "user" ? ((current && current.name) || "Cliente")
        : (m.author || (team ? "Equipe" : "Assistente"));
      var wrap = el("div", "msg " + (m.role === "user" ? "user" : "assistant") + (team ? " team" : ""));
      var body = m.role === "user" && m.intent === "unreadable" && !m.media
        ? "Conteúdo original indisponível: apenas o aviso de mensagem não lida foi salvo na época."
        : m.message;
      wrap.appendChild(el("div", "msg-body", body));
      if (m.media) {
        wrap.appendChild(mediaBlock(m.media));
        if (m.media.delivery_state && m.media.delivery_state !== "sent") wrap.appendChild(el("span", "media-delivery", m.media.delivery_state === "failed" ? "Envio recusado" : "Envio não confirmado — confira antes de reenviar"));
      }
      wrap.appendChild(el("div", "msg-when", (m.role === "user" ? "" : who + " · ") + when(m.created_at)));
      box.appendChild(wrap);
    });
    if (scrollToEnd || atEnd) box.scrollTop = box.scrollHeight;
  }

  // ── Photos, files and audio (kept for MEDIA_RETENTION_DAYS) ──────────────
  // The state comes from the server when the conversation opens; with it
  // open, a file that stopped being available is found out on use (a HEAD
  // through api(), which also takes an expired session to the login).
  var KINDS = { image: "foto", audio: "áudio", video: "vídeo", document: "documento", sticker: "figurinha" };

  function size(bytes) {
    if (!bytes) return "";
    var mb = bytes / 1048576;
    return mb >= 1 ? mb.toFixed(1).replace(".", ",") + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
  }
  function mediaPath(media) { return "/inbox/" + Number(openId) + "/media/" + Number(media.id); }
  function gone(box, err) {
    box.textContent = "";
    box.appendChild(el("span", "media-gone", err && err.status === 503
      ? "tente de novo em instantes" : "arquivo indisponível — reabra a conversa"));
  }
  function check(box, path) {
    return api(path, { method: "HEAD" }).catch(function (err) { gone(box, err); throw err; });
  }
  function downloadButton(box, path) {
    return button("Baixar", "btn media-download", function () {
      // A link with `download`, to the attachment form of the file: the Inbox
      // stays open (a reply being typed is kept) and a file the browser
      // cannot play — Ogg on an iPhone — is still saved.
      check(box, path).then(function () {
        var save = el("a"); save.setAttribute("href", "/painel/api" + path + "?download=1");
        save.setAttribute("download", ""); save.style.display = "none";
        document.body.appendChild(save); save.click(); save.remove();
      }, function () {});
    });
  }
  function mediaBlock(media) {
    var box = el("div", "msg-media");
    var label = [KINDS[media.kind] || "arquivo", size(media.stored_bytes || media.declared_bytes)]
      .filter(Boolean).join(", ");
    if (!media.available) {
      box.appendChild(el("span", "media-gone", (media.status_text || "arquivo indisponível") + " — " + label));
      return box;
    }
    var path = mediaPath(media), url = "/painel/api" + path;
    var onError = function () { check(box, path).catch(function () {}); };
    if (media.inline && (media.kind === "image" || media.kind === "sticker")) {
      var link = el("a"); link.setAttribute("href", url); link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener");
      var img = el("img", "media-img"); img.setAttribute("src", url); img.setAttribute("alt", label);
      img.setAttribute("loading", "lazy"); img.addEventListener("error", onError);
      link.appendChild(img); box.appendChild(link);
    } else if (media.inline && (media.kind === "audio" || media.kind === "video")) {
      var player = el(media.kind, "media-player");
      player.setAttribute("controls", ""); player.setAttribute("preload", "none");
      player.setAttribute("src", url); player.addEventListener("error", onError);
      box.appendChild(player);
      // Always beside the player: a browser that cannot play the format
      // (an Ogg voice note on an iPhone) still gets the file.
      box.appendChild(downloadButton(box, path));
    } else {
      var card = el("div", "media-card");
      card.appendChild(el("span", "media-name", media.filename || "arquivo"));
      card.appendChild(el("span", "media-meta", label));
      card.appendChild(downloadButton(box, path));
      box.appendChild(card);
    }
    if (ocrEnabled && (media.kind === "image" || media.kind === "document" || media.kind === "sticker")) {
      box.appendChild(button("Extrair texto", "btn media-ocr", function () { openOcr(media); }));
    }
    return box;
  }

  // OCR stays in this dialog, in memory, and never enters the reply field.
  function closeOcr() {
    ocrRun += 1;
    // Closing stops the reading on the server too, not only on this screen.
    if (ocrAbort) { ocrAbort.abort(); ocrAbort = null; }
    if ($("ocr-dialog").open) $("ocr-dialog").close();
    $("ocr-text").value = "";
    $("ocr-original").removeAttribute("src");
    $("ocr-original").hidden = true;
    $("ocr-copy").disabled = true;
  }
  function openOcr(media) {
    closeOcr();
    var run = ocrRun, id = openId;
    $("ocr-status").textContent = "Lendo o arquivo…";
    if (media.inline && (media.kind === "image" || media.kind === "sticker")) {
      $("ocr-original").src = "/painel/api" + mediaPath(media);
      $("ocr-original").hidden = false;
    }
    $("ocr-dialog").showModal();
    ocrAbort = window.AbortController ? new AbortController() : null;
    api(mediaPath(media) + "/ocr", { method: "POST", signal: ocrAbort && ocrAbort.signal }).then(function (data) {
      if (run !== ocrRun || id !== openId || !$("ocr-dialog").open) return;
      $("ocr-text").value = data.text;
      $("ocr-copy").disabled = !data.text;
      $("ocr-status").textContent = !data.text ? "Nenhum texto legível encontrado."
        : data.limited ? "Leitura parcial: até 3 páginas e 20 mil caracteres. Confira o original."
        : "Texto extraído. Confira o resultado com o original.";
    }).catch(function (err) {
      if (run === ocrRun && $("ocr-dialog").open) $("ocr-status").textContent = err.message;
    });
  }

  function note(text, isError) {
    var n = $("thread-msg"); n.className = "auto-msg" + (isError ? " err" : ""); n.textContent = text;
    n.title = text;
    $("thread-lock").classList.toggle("covered", !!text);
  }

  // Behind the ⋮: end the conversation (archived until the person writes
  // again) and block (the bot answers nothing and tells nobody). Both ask first.
  function renderManage(c) {
    var box = $("thread-manage"); box.textContent = "";
    if (c.blocked) {
      box.appendChild(el("p", "thread-manage-note",
        "Bloqueado: o bot não responde e ninguém é avisado. As mensagens ficam guardadas aqui."));
      box.appendChild(button("Desbloquear", "btn", function () { act("unblock"); }));
      return;
    }
    var heldByOther = c.state === "claimed" && !c.mine;
    if (!c.ended && !heldByOther) {
      box.appendChild(button("Encerrar conversa", "btn", function () {
        if (window.confirm("Encerrar esta conversa? Ela vai para Arquivadas. Se a pessoa escrever de novo, o bot atende como uma conversa nova.")) {
          $("thread-details").open = false; act("end");
        }
      }));
    }
    box.appendChild(button("Bloquear contato", "btn btn-danger", function () {
      if (window.confirm("Bloquear este contato? O bot não responde mais e ninguém da equipe é avisado das mensagens dele. Dá para desbloquear depois.")) {
        $("thread-details").open = false; act("block");
      }
    }));
  }

  function act(kind, extra) {
    var id = openId; note("", false);
    // "Assumir" says what the screen showed: if someone took it meanwhile,
    // the server refuses instead of reading the click as "Assumir no lugar".
    var body = kind === "claim" && current
      ? { state: current.state, owner_user_id: current.owner_user_id == null ? null : current.owner_user_id }
      : extra;
    api("/inbox/" + id + "/" + kind, { method: "POST", body: body }).then(function (data) {
      if (id !== openId) return;
      apply(data, true); loadList();
      if (kind === "claim" && !TOUCH) $("thread-text").focus();
      if (kind === "reopen" && $("thread-reopen-reply")) $("thread-reopen-reply").value = "";
    }).catch(function (err) { note(err.message, true); loadThread(true); });
  }

  function sendReply() {
    if (attachments.hasFile()) { attachments.send(); return; }
    var box = $("thread-text"), text = box.value.trim();
    if (!text || openId == null || $("thread-send").disabled) return;
    var id = openId;
    $("thread-send").disabled = true; note("Enviando…", false);
    api("/inbox/" + id + "/reply", { method: "POST", body: { text: text } }).then(function (data) {
      if (id !== openId) return;
      box.value = ""; note("", false);
      apply(data, true); loadList();
    }).catch(function (err) {
      note(err.message, true);
      if (current && current.mine) $("thread-send").disabled = false;
    });
  }

  function saveOrigin() {
    if (openId == null) return;
    var id = openId, msg = $("thread-origin-msg");
    msg.className = "auto-msg"; msg.textContent = "Salvando…";
    api("/contacts/" + id + "/origin", { method: "PATCH", body: {
      origin: $("thread-origin").value || null,
      detail: $("thread-origin-detail").value.trim() || null
    } }).then(function (data) {
      originDirty = false;
      if (id !== openId) return;
      apply(data, true);
      msg.className = "auto-msg ok"; msg.textContent = "Salvo.";
    }).catch(function (err) { msg.className = "auto-msg err"; msg.textContent = err.message; });
  }

  // ── Wiring ─────────────────────────────────────────────────────────────
  function searchText(text) {
    return (text || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();
  }
  // Keep the frame fixed. When a phone keyboard covers it, lift only the
  // composer; typing, status changes and browser toolbars never resize the chat.
  if (window.visualViewport) {
    var viewport = window.visualViewport;
    function fitViewport() {
      if (viewport.scale !== 1) return;
      var field = document.activeElement;
      var writing = field && field.id === "thread-text";
      var pane = document.querySelector(".inbox-thread-pane");
      var covered = pane.getBoundingClientRect().bottom - viewport.height - viewport.offsetTop;
      var box = $("thread-messages");
      var atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      document.documentElement.style.setProperty("--keyboard-lift",
        (writing && covered > 120 ? covered : 0) + "px");
      // Reading the latest when the keyboard opened: keep the latest in view.
      if (atEnd) box.scrollTop = box.scrollHeight;
    }
    viewport.addEventListener("resize", fitViewport);
    viewport.addEventListener("scroll", fitViewport);
    document.addEventListener("focusin", fitViewport);
    document.addEventListener("focusout", function () { setTimeout(fitViewport, 0); });
    fitViewport();
  }
  $("inbox-search").addEventListener("input", function () { renderList(listedRows); });
  $("ocr-close").addEventListener("click", closeOcr);
  $("ocr-dialog").addEventListener("cancel", function (event) { event.preventDefault(); closeOcr(); });
  $("ocr-copy").addEventListener("click", function () {
    if (!navigator.clipboard) {
      $("ocr-text").select();
      $("ocr-status").textContent = "Texto selecionado. Use a opção de copiar do aparelho.";
      return;
    }
    navigator.clipboard.writeText($("ocr-text").value).then(function () {
      $("ocr-status").textContent = "Texto copiado.";
    }).catch(function () { $("ocr-status").textContent = "Selecione o texto para copiar."; });
  });
  document.addEventListener("click", function (event) {
    var details = $("thread-details");
    if (details.open && !details.contains(event.target)) details.open = false;
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") $("thread-details").open = false;
  });
  $("thread-send").addEventListener("click", sendReply);
  $("thread-text").addEventListener("keydown", function (e) {
    // Enter sends on a computer; on a phone Enter is a new line and the button sends.
    if (!TOUCH && e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendReply(); }
  });
  $("thread-back").addEventListener("click", closeConversation);
  $("thread-origin").addEventListener("change", function () { originDirty = true; });
  $("thread-origin-detail").addEventListener("input", function () { originDirty = true; });
  $("thread-origin-save").addEventListener("click", saveOrigin);
  // ── A lead added by hand ───────────────────────────────────────────────
  var leadBusy = false;
  function leadStatus(text, isError) {
    var n = $("lead-msg"); n.className = "auto-msg" + (isError ? " err" : ""); n.textContent = text;
  }
  $("lead-open").addEventListener("click", function () {
    $("lead-form").reset(); leadStatus("", false); $("lead-notice").hidden = true;
    $("lead-dialog").showModal(); $("lead-name").focus();
  });
  $("lead-cancel").addEventListener("click", function () { $("lead-dialog").close(); });
  $("lead-form").addEventListener("submit", function (event) {
    event.preventDefault();
    if (leadBusy) return;
    var body = {
      name: $("lead-name").value.trim(), phone: $("lead-phone").value.trim(),
      email: $("lead-email").value.trim() || null, origin: $("lead-origin").value,
      detail: $("lead-detail").value.trim() || null, send: $("lead-send").checked
    };
    if (!body.name || !body.phone) { leadStatus("Preencha nome e WhatsApp.", true); return; }
    leadBusy = true; $("lead-submit").disabled = true; leadStatus("Cadastrando…", false);
    api("/leads", { method: "POST", body: body }).then(function (data) {
      // Added from the Leads tab: it stays there, with the table refreshed and
      // the outcome shown above it, and a link to the conversation.
      $("lead-dialog").close();
      if (window.PanelLeads) window.PanelLeads.reload();
      var n = $("lead-notice");
      n.className = "auto-msg leads-notice" + (!data.sent && !data.existing && body.send ? " err" : "");
      $("lead-notice-text").textContent = data.notice;
      $("lead-notice-open").href = "#inbox/" + data.contact_id;
      n.hidden = false;
    }).catch(function (err) { leadStatus(err.message, true); })
      .then(function () { leadBusy = false; $("lead-submit").disabled = false; });
  });

  $("inbox-channel").addEventListener("change", function (e) { channelFilter = e.target.value; loadList(); });

  var nav = document.querySelector('.nav-item[data-view="inbox"]');
  function onOpen() {
    // painel.js has just set the address to #inbox; keep the open conversation in it.
    if (openId != null) history.replaceState(null, "", "#inbox/" + openId);
    loadList();
  }
  nav.addEventListener("click", onOpen);
  nav.addEventListener("keydown", function (e) { if (e.key === "Enter") onOpen(); });

  // A Telegram button opened in a tab where the panel is already loaded only
  // changes the address after the #: open that conversation.
  window.addEventListener("hashchange", function () {
    var m = location.hash.match(/^#inbox\/(\d+)$/);
    if (!m || (Number(m[1]) === openId && isActive())) return;
    nav.click();   // painel.js shows the tab; onOpen() loads the list
    openConversation(Number(m[1]));
  });

  document.addEventListener("proposals:loaded", function () { if (current) renderHead(); });

  renderFilters();
  var linked = location.hash.match(/^#inbox\/(\d+)$/);
  if (location.hash.split("/")[0] === "#inbox") {
    loadList();
    if (linked) openConversation(Number(linked[1]));
  } else {
    api("/inbox?filter=waiting").then(function (data) { setCount(data.waiting_total || 0); }).catch(function () {});
  }

  setInterval(function () { if (!document.hidden && isActive()) loadList(); }, LIST_MS);
  var threadTicks = 0;
  setInterval(function () {
    if (document.hidden || !isActive() || openId == null) return;
    threadTicks += 1;
    loadThread(false, threadTicks % RECONCILE_EVERY === 0);
  }, THREAD_MS);
  setInterval(function () {
    if (document.hidden || isActive()) return;
    api("/inbox?filter=waiting").then(function (data) { setCount(data.waiting_total || 0); }).catch(function () {});
  }, COUNT_MS);
})();
