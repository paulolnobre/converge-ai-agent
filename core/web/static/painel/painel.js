/* Funil Converge — reads /painel/api and renders the four views.
 * Every value that came from a contact goes through esc() before it is
 * placed in HTML: names and messages are written by strangers on Telegram. */
(function () {
  "use strict";

  // ── vocabulary ────────────────────────────────────────────────────────
  var STAGES = [
    { id: "novo",       label: "Novo lead",         dot: "var(--text-3)" },
    { id: "respondido", label: "Bot respondeu",     dot: "#4F8FE8" },
    { id: "agendado",   label: "Conversa agendada", dot: "var(--warning)" },
    { id: "proposta",   label: "Proposta enviada",  dot: "var(--accent)" },
    { id: "fechado",    label: "Fechado",           dot: "var(--success)" },
    { id: "perdido",    label: "Perdido",           dot: "var(--critical)" }
  ];
  var stageById = {}; STAGES.forEach(function (s) { stageById[s.id] = s; });
  var OPEN_STAGES = ["novo", "respondido", "agendado", "proposta"];

  var CHANNELS = {
    telegram:  { label: "Telegram",  icon: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>' },
    whatsapp:  { label: "WhatsApp",  icon: '<path d="M20.5 3.5a10 10 0 0 0-16.9 10.9L2 21l6.8-1.5A10 10 0 1 0 20.5 3.5Z"/>' },
    instagram: { label: "Instagram", icon: '<rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/>' },
    messenger: { label: "Facebook",  icon: '<path d="M18 2h-3a5 5 0 0 0-5 5v3H7v4h3v8h4v-8h3l1-4h-4V7a1 1 0 0 1 1-1h3z"/>' },
    email:     { label: "E-mail",    icon: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>' }
  };
  function channel(id) {
    return CHANNELS[id] || { label: id || "—", icon: '<circle cx="12" cy="12" r="9"/>' };
  }
  var ORIGIN_LABELS = {
    anuncio_instagram: "Anúncio no Instagram", anuncio_facebook: "Anúncio no Facebook",
    site: "Site", google: "Google", indicacao: "Indicação", email: "E-mail", outro: "Outro", "": "Não informado"
  };
  var PALETTE = ["#4F8FE8", "#F5C518", "#A855F7", "#22C55E", "#F97316", "#EC4899"];
  var STATUS_LABEL = { confirmed: "Confirmado", done: "Concluído", cancelled: "Cancelado", no_show: "Não compareceu" };
  // What the calendar sync could not follow: the appointment gets no reminder
  // until someone fixes it in Google.
  var SYNC_PROBLEM = {
    all_day: "virou dia inteiro no Google",
    not_organizer: "a agenda não organiza mais",
    start_collision: "horário igual a outro agendamento"
  };

  // ── safety: everything below comes from strangers on Telegram ─────────
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ── state ─────────────────────────────────────────────────────────────
  var state = { leads: [], staff: [], appts: [], loadedAt: null };
  var filters = { q: "", stage: "", owner: "", channel: "", origin: "" };
  var agendaOn = {};
  var boardStage = "";  // "" = every stage
  var panelId = null, panelAppt = null;

  function toDate(v) { return v ? new Date(v) : null; }
  function initials(name) {
    return String(name || "?").split(/\s+/).filter(Boolean).slice(0, 2)
      .map(function (p) { return p[0]; }).join("").toUpperCase() || "?";
  }
  function fmtDateTime(d) {
    if (!d) return "—";
    return d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }) + " às " +
      d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  }
  function fmtRel(d) {
    if (!d) return "—";
    var mins = Math.round((Date.now() - d.getTime()) / 60000);
    if (mins < 1) return "agora";
    if (mins < 60) return "há " + mins + " min";
    var h = Math.round(mins / 60);
    if (h < 24) return "há " + h + "h";
    var days = Math.round(h / 24);
    return "há " + days + (days === 1 ? " dia" : " dias");
  }
  // A team member being tested as a customer (/teste): shown with a tag in
  // the lists, left out of every number.
  var TEST_TAG = '<span class="test-tag">TESTE</span>';
  function customers() { return state.leads.filter(function (l) { return !l.isTest; }); }
  function testContact(contactId) {
    var lead = state.leads.find(function (l) { return l.id === contactId; });
    return !!(lead && lead.isTest);
  }
  // Asked for a proposal by e-mail and still waiting on it (propostas.js).
  function proposalOf(contactId) {
    return window.PanelProposals ? window.PanelProposals.forContact(contactId) : null;
  }
  function proposalTag(contactId) {
    var p = proposalOf(contactId);
    return p && p.status !== "proposta enviada" ? '<span class="prop-tag" title="proposta solicitada">proposta solicitada</span>' : "";
  }
  function realAppts() { return state.appts.filter(function (a) { return !testContact(a.contactId); }); }

  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  // ── API ───────────────────────────────────────────────────────────────
  function api(path, options) {
    var opts = options || {};
    return fetch("/painel/api" + path, {
      method: opts.method || "GET",
      credentials: "same-origin",
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (r) {
      if (r.status === 401) {
        // Back to the conversation a Telegram button opened, and to nothing else.
        if (window.PanelApp) window.PanelApp.signOut();
        else location.replace("/painel/entrar" + (/^#inbox\/\d+$/.test(location.hash) ? location.hash : ""));
        throw new Error("Sessão expirada.");
      }
      return r.json().then(function (data) {
        if (!r.ok) throw new Error((data && data.detail) || ("Erro " + r.status));
        return data;
      });
    });
  }

  function normalize(row) {
    return {
      id: row.id,
      name: row.name || "Sem nome",
      org: row.organization || "",
      source: row.source,
      origin: row.origin || "",
      originDetail: row.origin_detail || "",
      interest: row.interest || "",
      stage: row.stage,
      ownerId: row.owner_staff_id,
      ownerName: row.owner_name || "",
      notes: row.notes || "",
      phone: row.phone || "",
      email: row.email || "",
      createdAt: toDate(row.created_at),
      lastAt: toDate(row.last_message_at) || toDate(row.created_at),
      lastMsg: row.last_message || "",
      nextAt: toDate(row.next_appointment_at),
      isTest: !!row.is_test
    };
  }

  function load() {
    setStatus("Atualizando…");
    return Promise.all([api("/leads"), api("/agenda")]).then(function (res) {
      state.leads = res[0].leads.map(normalize);
      state.staff = res[0].staff;
      state.appts = res[1].appointments.map(function (a) {
        return {
          id: a.id, start: toDate(a.starts_at), end: toDate(a.ends_at), status: a.status,
          title: a.title, who: a.staff_id ? String(a.staff_id) : "bot",
          contactId: a.contact_id, contactName: a.contact_name || "—", org: a.organization || "",
          meetingUrl: a.meeting_url || "", syncProblem: a.sync_problem || ""
        };
      });
      // The team's calendars as mirrored: events booked by hand in Google
      // show next to the appointments, under the same people filter.
      state.events = (res[1].events || []).map(function (e) {
        return {
          key: e.key, start: toDate(e.starts_at), end: toDate(e.ends_at), allDay: !!e.all_day,
          tentative: e.status === "tentative", title: e.title || "",
          who: (e.staff_ids || [null]).map(function (id) { return id == null ? "bot" : String(id); })
        };
      });
      state.sync = res[1].sync || null;
      state.loadedAt = new Date();
      if (!Object.keys(agendaOn).length) {
        agendaOn.bot = true;
        state.staff.forEach(function (s) { agendaOn[String(s.id)] = true; });
      }
      renderAll();
      setStatus("Atualizado às " + state.loadedAt.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }));
    }).catch(function (err) { setStatus(err.message, true); });
  }

  function setStatus(text, isError) {
    var s = document.getElementById("status-line");
    s.textContent = text;
    s.classList.toggle("error", !!isError);
  }

  function patchLead(id, fields) {
    return api("/leads/" + id, { method: "PATCH", body: fields }).then(function (row) {
      var fresh = normalize(row);
      state.leads = state.leads.map(function (l) { return l.id === id ? fresh : l; });
      renderAll();
      return row;
    }).catch(function (err) { setStatus(err.message, true); throw err; });
  }

  // ── who is who on the agenda ──────────────────────────────────────────
  function who(key) {
    if (key === "bot") return { name: "Agenda geral", init: "AG", color: "var(--text-3)" };
    var idx = state.staff.findIndex(function (s) { return String(s.id) === key; });
    var s = state.staff[idx];
    if (!s) return { name: "—", init: "?", color: "var(--text-3)" };
    return { name: s.name, init: initials(s.name), color: PALETTE[idx % PALETTE.length] };
  }

  // ── rendering ─────────────────────────────────────────────────────────
  function renderTiles() {
    var all = customers();
    var open = all.filter(function (l) { return OPEN_STAGES.indexOf(l.stage) > -1; });
    var won = all.filter(function (l) { return l.stage === "fechado"; });
    var decided = all.filter(function (l) { return l.stage === "fechado" || l.stage === "perdido"; });
    var conv = decided.length ? Math.round(won.length / decided.length * 100) + "%" : "—";
    var weekAgo = Date.now() - 7 * 864e5;
    var fresh = all.filter(function (l) { return l.createdAt && l.createdAt.getTime() >= weekAgo; });
    var weekAhead = Date.now() + 7 * 864e5;
    var upcoming = realAppts().filter(function (a) {
      return a.status === "confirmed" && a.start >= new Date() && a.start.getTime() <= weekAhead;
    });
    var tiles = [
      { label: "Leads em aberto", value: open.length, note: "no funil agora" },
      { label: "Taxa de conversão", value: conv, note: decided.length ? won.length + " fechados de " + decided.length + " decididos" : "nenhum negócio decidido ainda" },
      { label: "Novos na semana", value: fresh.length, note: "primeiro contato nos últimos 7 dias" },
      { label: "Agendamentos", value: upcoming.length, note: "nos próximos 7 dias" }
    ];
    var wrap = document.getElementById("tiles"); wrap.innerHTML = "";
    tiles.forEach(function (t) {
      wrap.appendChild(el("div", "tile",
        '<div class="tile-label">' + esc(t.label) + '</div><div class="tile-value mono">' + esc(t.value) +
        '</div><div class="tile-delta flat">' + esc(t.note) + '</div>'));
    });
  }

  function renderMiniFunnel() {
    var wrap = document.getElementById("mini-funnel"); wrap.innerHTML = "";
    var counts = STAGES.map(function (s) {
      return { stage: s, n: customers().filter(function (l) { return l.stage === s.id; }).length };
    });
    var max = Math.max.apply(null, counts.map(function (c) { return c.n; }).concat([1]));
    counts.forEach(function (c) {
      wrap.appendChild(el("div", "mf-row",
        '<div class="mf-label">' + esc(c.stage.label) + '</div>' +
        '<div class="mf-track"><div class="mf-fill" style="width:' + Math.round(c.n / max * 100) + '%;background:' + c.stage.dot + '"></div></div>' +
        '<div class="mf-count mono">' + c.n + '</div>'));
    });
  }

  function renderUpcoming() {
    var wrap = document.getElementById("upcoming-list"); wrap.innerHTML = "";
    var list = realAppts().filter(function (a) { return a.status === "confirmed" && a.start >= new Date(); });
    if (!list.length) { wrap.appendChild(el("div", "list-sub", "Nenhuma conversa agendada.")); return; }
    list.slice(0, 6).forEach(function (a) {
      var row = el("div", "list-row",
        '<div><div class="list-name">' + esc(a.contactName) + '</div><div class="list-sub">' + esc(who(a.who).name) + '</div></div>' +
        '<div class="list-when">' + esc(fmtDateTime(a.start)) + '</div>');
      if (a.contactId) row.addEventListener("click", function () { openPanel(a.contactId); });
      wrap.appendChild(row);
    });
  }

  function renderRecent() {
    var wrap = document.getElementById("recent-list"); wrap.innerHTML = "";
    var list = customers().sort(function (a, b) { return b.lastAt - a.lastAt; }).slice(0, 6);
    if (!list.length) { wrap.appendChild(el("div", "list-sub", "Ninguém falou com o agente ainda.")); return; }
    list.forEach(function (l) {
      var s = stageById[l.stage] || STAGES[0];
      var preview = l.lastMsg.length > 70 ? l.lastMsg.slice(0, 70) + "…" : l.lastMsg;
      var row = el("div", "list-row",
        '<div style="min-width:0"><div class="list-name">' + esc(l.name) + '</div><div class="list-sub">' + esc(preview || channel(l.source).label) + '</div></div>' +
        '<span class="chip" style="color:' + s.dot + ';margin-left:8px;">' + esc(s.label) + '</span>' +
        '<div class="list-when">' + esc(fmtRel(l.lastAt)) + '</div>');
      row.addEventListener("click", function () { openPanel(l.id); });
      wrap.appendChild(row);
    });
  }

  function renderOrigins() {
    var box = document.getElementById("origin-bars"); box.innerHTML = "";
    var since = Date.now() - 30 * 864e5;
    var recent = customers().filter(function (l) { return l.createdAt && l.createdAt.getTime() >= since; });
    var counts = Object.keys(ORIGIN_LABELS).map(function (key) {
      return { key: key, n: recent.filter(function (l) { return l.origin === key; }).length };
    }).filter(function (c) { return c.n > 0; }).sort(function (a, b) { return b.n - a.n; });
    if (!counts.length) { box.appendChild(el("div", "muted", "Nenhum contato novo nos últimos 30 dias.")); return; }
    var max = Math.max.apply(null, counts.map(function (c) { return c.n; }));
    counts.forEach(function (c) {
      box.appendChild(el("div", "mf-row",
        '<span class="mf-label">' + esc(ORIGIN_LABELS[c.key]) + '</span>' +
        '<div class="mf-track"><div class="mf-fill" style="width:' + (c.n / max * 100) + '%"></div></div>' +
        '<span class="mf-count">' + c.n + '</span>'));
    });
  }

  function renderStageFilter() {
    var wrap = document.getElementById("stage-filter"); wrap.innerHTML = "";
    [{ id: "", label: "Todas", dot: "var(--accent)" }].concat(STAGES).forEach(function (s) {
      // Counts what the board below shows, test cards included.
      var n = s.id ? state.leads.filter(function (l) { return l.stage === s.id; }).length : state.leads.length;
      var chip = el("button", "agenda-chip " + (boardStage === s.id ? "on" : "off"),
        '<span class="who-dot" style="background:' + s.dot + '"></span>' + esc(s.label) +
        ' <span class="count">' + n + '</span>');
      chip.setAttribute("aria-pressed", boardStage === s.id ? "true" : "false");
      chip.addEventListener("click", function () { boardStage = s.id; renderStageFilter(); renderBoard(); });
      wrap.appendChild(chip);
    });
  }

  var dragId = null;
  function renderBoard() {
    var board = document.getElementById("board"); board.innerHTML = "";
    STAGES.filter(function (s) { return !boardStage || s.id === boardStage; }).forEach(function (s) {
      var items = state.leads.filter(function (l) { return l.stage === s.id; });
      var col = el("div", "col");
      col.appendChild(el("div", "col-head",
        '<span class="col-dot" style="background:' + s.dot + '"></span><span class="col-title">' + esc(s.label) +
        '</span><span class="col-count mono">' + items.length + '</span>'));
      var cards = el("div", "col-cards");
      items.forEach(function (l) { cards.appendChild(makeCard(l)); });
      if (!items.length) cards.appendChild(el("div", "col-empty", "Nenhum contato nesta etapa."));
      cards.addEventListener("dragover", function (e) { e.preventDefault(); cards.classList.add("drag-over"); });
      // dragleave also fires when the pointer crosses onto a card inside the
      // column: the highlight went off and on with every card it passed.
      cards.addEventListener("dragleave", function (e) {
        if (e.relatedTarget && cards.contains(e.relatedTarget)) return;
        cards.classList.remove("drag-over");
      });
      cards.addEventListener("drop", function (e) {
        e.preventDefault(); cards.classList.remove("drag-over");
        if (dragId == null) return;
        var lead = state.leads.find(function (l) { return l.id === dragId; });
        if (lead && lead.stage !== s.id) patchLead(dragId, { stage: s.id });
        dragId = null;
      });
      col.appendChild(cards);
      board.appendChild(col);
    });
  }

  function makeCard(l) {
    var c = channel(l.source);
    var card = el("div", "lcard",
      '<div class="lcard-top"><span class="lcard-name">' + esc(l.name) + '</span>' + (l.isTest ? TEST_TAG : "") + proposalTag(l.id) + '</div>' +
      (l.org ? '<div class="lcard-org">' + esc(l.org) + '</div>' : "") +
      (l.interest ? '<div class="lcard-tags"><span class="chip product">' + esc(l.interest) + '</span></div>' : "") +
      (l.ownerName ? '<div class="lcard-owner">' + esc(l.ownerName) + '</div>' : "") +
      '<div class="lcard-foot"><span class="channel"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' + c.icon + '</svg>' + esc(c.label) + '</span>' +
      '<span class="list-when mono">' + esc(fmtRel(l.lastAt)) + '</span></div>');
    card.draggable = true;
    card.addEventListener("dragstart", function () { dragId = l.id; card.classList.add("dragging"); });
    card.addEventListener("dragend", function () {
      card.classList.remove("dragging");
      // Dropped outside a column, or cancelled: nothing is being dragged now.
      dragId = null;
      document.querySelectorAll(".col-cards.drag-over").forEach(function (c) { c.classList.remove("drag-over"); });
    });
    card.addEventListener("click", function () { openPanel(l.id); });
    return card;
  }

  function renderAgendaChips() {
    var wrap = document.getElementById("agenda-chips"); wrap.innerHTML = "";
    ["bot"].concat(state.staff.map(function (s) { return String(s.id); })).forEach(function (key) {
      var w = who(key);
      var chip = el("button", "agenda-chip " + (agendaOn[key] ? "on" : "off"),
        '<span class="who-dot" style="background:' + w.color + '"></span>' + esc(w.name));
      chip.addEventListener("click", function () { agendaOn[key] = !agendaOn[key]; renderAgendaChips(); renderAgenda(); });
      wrap.appendChild(chip);
    });
    var withCal = state.staff.filter(function (s) { return s.has_calendar; }).map(function (s) { return s.name; });
    document.getElementById("agenda-note-text").innerHTML =
      "<b>Agenda geral</b> é onde o bot marca enquanto a conversa não é atribuída a alguém. " +
      (withCal.length
        ? "Calendário próprio conectado: <b>" + esc(withCal.join(", ")) + "</b>."
        : "Ninguém da equipe tem calendário próprio conectado ainda.");
  }

  function dayLabel(d) {
    var today = new Date(); today.setHours(0, 0, 0, 0);
    var that = new Date(d); that.setHours(0, 0, 0, 0);
    var diff = Math.round((that - today) / 864e5);
    var weekday = d.toLocaleDateString("pt-BR", { weekday: "long" });
    var date = d.toLocaleDateString("pt-BR", { day: "2-digit", month: "long" });
    if (diff === 0) return { text: "Hoje", n: weekday + ", " + date, today: true };
    if (diff === -1) return { text: "Ontem", n: weekday + ", " + date };
    if (diff === 1) return { text: "Amanhã", n: weekday + ", " + date };
    return { text: weekday.charAt(0).toUpperCase() + weekday.slice(1), n: date };
  }

  function renderAgendaStale() {
    var box = document.getElementById("agenda-stale");
    var sync = state.sync;
    if (!sync || !sync.stale) { box.hidden = true; return; }
    // The server says which calendar is behind; a past error since fixed is not named.
    var names = sync.calendars.filter(function (c) { return c.stale; })
      .map(function (c) { return who(c.staff_id == null ? "bot" : String(c.staff_id)).name; })
      .filter(function (name, i, all) { return all.indexOf(name) === i; });
    box.textContent = "Agenda desatualizada" + (names.length ? ": " + names.join(", ") : "") +
      ". Os eventos do Google podem não estar em dia.";
    box.hidden = false;
  }

  function eventRow(e, now, spill) {
    var names = e.who.map(function (k) { return who(k).name; }).join(" · ");
    var first = who(e.who[0]);
    // A later day of a multi-day event is "the whole day" there.
    var time = e.allDay || spill ? "dia todo" : e.start.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
    return el("div", "appt-row event-row" + (e.end < now ? " is-past" : ""),
      '<div class="appt-time">' + esc(time) + '</div>' +
      '<div class="appt-who"><div class="who-avatar" style="background:' + first.color + '22;color:' + first.color + '">' + esc(first.init) + '</div><span class="wname">' + esc(names) + '</span></div>' +
      '<div class="appt-main"><div class="appt-title">' + esc(e.title || "(sem título)") + '</div><div class="appt-sub">' + esc(e.tentative ? "do Google · a confirmar" : "do Google") + '</div></div>' +
      '<span class="status-pill google-pill">do Google</span>');
  }

  function renderAgenda() {
    renderAgendaStale();
    var list = document.getElementById("agenda-list"); list.innerHTML = "";
    var appts = state.appts.filter(function (a) { return agendaOn[a.who] !== false; });
    var events = (state.events || []).filter(function (e) {
      return e.who.some(function (k) { return agendaOn[k] !== false; });
    });
    // An event spanning several days (vacation, a trip) shows on each day it
    // covers, from its own start or the first day of the agenda.
    var eventEntries = [];
    events.forEach(function (e) {
      var day = new Date(e.start); day.setHours(0, 0, 0, 0);
      var first = true;
      while (day < e.end) {
        eventEntries.push({ kind: "event", start: first ? e.start : new Date(day), item: e, spill: !first });
        first = false;
        day = new Date(day); day.setDate(day.getDate() + 1);
      }
    });
    var all = appts.map(function (a) { return { kind: "appt", start: a.start, item: a }; })
      .concat(eventEntries)
      .sort(function (x, y) {
        // All-day events first in their day, then by time.
        var xa = x.kind === "event" && (x.item.allDay || x.spill), ya = y.kind === "event" && (y.item.allDay || y.spill);
        if (x.start.toDateString() === y.start.toDateString() && xa !== ya) return xa ? -1 : 1;
        return x.start - y.start;
      });
    if (!all.length) { list.appendChild(el("div", "agenda-empty", "Nenhum compromisso nesse período.")); return; }
    var groups = [], byKey = {};
    all.forEach(function (entry) {
      var k = entry.start.toDateString();
      if (!byKey[k]) { byKey[k] = { date: entry.start, items: [] }; groups.push(byKey[k]); }
      byKey[k].items.push(entry);
    });
    var now = new Date();
    groups.forEach(function (g) {
      var lab = dayLabel(g.date);
      var section = el("div", "day-group");
      section.appendChild(el("div", "day-heading" + (lab.today ? " today" : ""),
        '<span>' + esc(lab.text) + '</span><span class="n">' + esc(lab.n) + '</span>'));
      g.items.forEach(function (entry) {
        if (entry.kind === "event") { section.appendChild(eventRow(entry.item, now, entry.spill)); return; }
        var a = entry.item;
        var w = who(a.who);
        var row = el("div", "appt-row" + (a.end < now ? " is-past" : ""),
          '<div class="appt-time">' + esc(a.start.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })) + '</div>' +
          '<div class="appt-who"><div class="who-avatar" style="background:' + w.color + '22;color:' + w.color + '">' + esc(w.init) + '</div><span class="wname">' + esc(w.name) + '</span></div>' +
          '<div class="appt-main"><div class="appt-title">' + esc(a.contactName) + '</div><div class="appt-sub">' + esc(a.org || a.title) + '</div></div>' +
          (a.syncProblem && a.status === "confirmed"
            ? '<span class="status-pill status-attention" title="Sem lembrete até ser corrigido no Google">precisa de atenção: ' + esc(SYNC_PROBLEM[a.syncProblem] || a.syncProblem) + '</span>'
            : '<span class="status-pill status-' + esc(a.status) + '">' + esc(STATUS_LABEL[a.status] || a.status) + '</span>'));
        if (a.contactId) row.addEventListener("click", function () { openPanel(a.contactId, a.id); });
        section.appendChild(row);
      });
      list.appendChild(section);
    });
  }

  function populateFilters() {
    var fs = document.getElementById("f-stage");
    STAGES.forEach(function (s) { var o = el("option", "", esc(s.label)); o.value = s.id; fs.appendChild(o); });
    var ps = document.getElementById("p-stage");
    STAGES.forEach(function (s) { var o = el("option", "", esc(s.label)); o.value = s.id; ps.appendChild(o); });
  }

  function renderOwnerOptions() {
    var fo = document.getElementById("f-owner");
    var keep = fo.value;
    fo.innerHTML = '<option value="">Todos os responsáveis</option><option value="none">Sem responsável</option>';
    state.staff.forEach(function (s) { var o = el("option", "", esc(s.name)); o.value = String(s.id); fo.appendChild(o); });
    fo.value = keep;
    var po = document.getElementById("p-owner");
    po.innerHTML = '<option value="">Ninguém ainda</option>';
    state.staff.forEach(function (s) { var o = el("option", "", esc(s.name)); o.value = String(s.id); po.appendChild(o); });
  }

  function renderTable() {
    var q = filters.q.toLowerCase();
    var rows = state.leads.filter(function (l) {
      if (filters.stage && l.stage !== filters.stage) return false;
      if (filters.owner === "none" && l.ownerId) return false;
      if (filters.owner && filters.owner !== "none" && String(l.ownerId) !== filters.owner) return false;
      if (filters.channel && l.source !== filters.channel) return false;
      if (filters.origin === "none" && l.origin) return false;
      if (filters.origin && filters.origin !== "none" && l.origin !== filters.origin) return false;
      if (q && (l.name + " " + l.org).toLowerCase().indexOf(q) < 0) return false;
      return true;
    }).sort(function (a, b) { return b.lastAt - a.lastAt; });
    var body = document.getElementById("table-body"); body.innerHTML = "";
    if (!rows.length) { body.appendChild(el("tr", "empty-row", "<td colspan='8'>Nenhum contato com esses filtros.</td>")); return; }
    rows.forEach(function (l) {
      var s = stageById[l.stage] || STAGES[0]; var c = channel(l.source);
      var tr = el("tr", "",
        '<td><div class="cell-name"><div class="avatar">' + esc(initials(l.name)) + '</div><div class="who"><span class="name">' + esc(l.name) + (l.isTest ? TEST_TAG : "") + proposalTag(l.id) + '</span><span class="org">' + esc(l.org) + '</span></div></div></td>' +
        '<td data-label="Canal"><span class="channel"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' + c.icon + '</svg>' + esc(c.label) + '</span></td>' +
        '<td data-label="Origem">' + (l.origin ? esc(ORIGIN_LABELS[l.origin] || l.origin) : '<span class="muted">—</span>') + '</td>' +
        '<td data-label="Interesse">' + (l.interest ? esc(l.interest) : '<span class="muted">—</span>') + '</td>' +
        '<td data-label="Etapa"><span class="stage-pill" style="background:color-mix(in srgb, ' + s.dot + ' 14%, transparent);color:' + s.dot + '"><span class="col-dot" style="background:' + s.dot + '"></span>' + esc(s.label) + '</span></td>' +
        '<td data-label="Responsável">' + (l.ownerName ? esc(l.ownerName) : '<span class="muted">—</span>') + '</td>' +
        '<td data-label="Última mensagem" class="mono">' + esc(fmtRel(l.lastAt)) + '</td>' +
        '<td data-label="Próxima conversa" class="mono">' + esc(l.nextAt ? fmtDateTime(l.nextAt) : "—") + '</td>');
      tr.addEventListener("click", function () { openPanel(l.id); });
      body.appendChild(tr);
    });
  }

  function updateCounts() {
    document.getElementById("count-open").textContent = customers().filter(function (l) { return OPEN_STAGES.indexOf(l.stage) > -1; }).length;
    document.getElementById("count-all").textContent = customers().length;
    var weekAhead = Date.now() + 7 * 864e5;
    document.getElementById("count-appts").textContent = realAppts().filter(function (a) {
      return a.status === "confirmed" && a.start >= new Date() && a.start.getTime() <= weekAhead;
    }).length;
  }

  function renderAll() {
    renderOwnerOptions();
    renderTiles(); renderMiniFunnel(); renderUpcoming(); renderRecent(); renderOrigins();
    renderStageFilter(); renderBoard(); renderAgendaChips(); renderAgenda(); renderTable(); updateCounts();
  }

  // ── detail panel ──────────────────────────────────────────────────────
  // Only a Meet address is made clickable: the value comes from Google, and a
  // link in the panel is worth following only if it is what it says it is.
  function isMeetUrl(url) {
    return /^https:\/\/meet\.google\.com\/[a-z0-9-]+(\?.*)?$/i.test(url || "");
  }

  function renderMeetings(contactId, focusId) {
    var field = document.getElementById("p-meetings-field");
    var box = document.getElementById("p-meetings"); box.innerHTML = "";
    var now = new Date();
    var mine = state.appts.filter(function (a) { return a.contactId === contactId; })
      .sort(function (a, b) {
        var fa = a.end >= now ? 0 : 1, fb = b.end >= now ? 0 : 1;
        return fa - fb || (fa === 0 ? a.start - b.start : b.start - a.start);
      });
    field.hidden = !mine.length;
    mine.forEach(function (a) {
      var card = el("div", "meeting" + (a.id === focusId ? " focus" : "") + (a.end < now ? " is-past" : ""));
      var head = el("div", "meeting-head");
      head.appendChild(el("span", "meeting-when mono", fmtDateTime(a.start)));
      var pill = el("span", "status-pill status-" + a.status, STATUS_LABEL[a.status] || a.status);
      head.appendChild(pill);
      card.appendChild(head);
      if (a.status === "cancelled") {
        card.appendChild(el("div", "meeting-note", "Compromisso cancelado."));
      } else if (isMeetUrl(a.meetingUrl)) {
        var actions = el("div", "meeting-actions");
        var join = el("a", "btn btn-accent meeting-join", "Entrar no Google Meet");
        join.href = a.meetingUrl; join.target = "_blank"; join.rel = "noopener noreferrer";
        var copy = el("button", "btn", "Copiar link"); copy.type = "button";
        copy.addEventListener("click", function () {
          var done = function () { copy.textContent = "Copiado"; setTimeout(function () { copy.textContent = "Copiar link"; }, 1500); };
          if (navigator.clipboard) navigator.clipboard.writeText(a.meetingUrl).then(done, function () {});
        });
        actions.appendChild(join); actions.appendChild(copy);
        card.appendChild(actions);
        card.appendChild(el("div", "meeting-url mono", a.meetingUrl.replace(/^https:\/\//, "")));
      } else {
        card.appendChild(el("div", "meeting-note", "Sem link do Meet neste compromisso."));
      }
      box.appendChild(card);
    });
  }

  function openPanel(id, appointmentId) {
    panelId = id; panelAppt = appointmentId == null ? null : appointmentId;
    document.getElementById("p-saved").hidden = true;
    document.getElementById("p-convo").innerHTML = '<span class="muted">Carregando…</span>';
    document.getElementById("panel").classList.add("show");
    document.getElementById("scrim").classList.add("show");
    api("/leads/" + id).then(fillPanel).catch(function (err) { setStatus(err.message, true); });
  }

  function fillPanel(row) {
    if (row.id !== panelId) return;
    var l = normalize(row);
    document.getElementById("p-avatar").textContent = initials(l.name);
    var pName = document.getElementById("p-name");
    pName.textContent = l.name;
    if (l.isTest) pName.insertAdjacentHTML("beforeend", " " + TEST_TAG);
    document.getElementById("p-org").textContent = l.org || "Empresa não informada";
    document.getElementById("p-stage").value = l.stage;
    document.getElementById("p-channel").textContent = channel(l.source).label;
    document.getElementById("p-appt").textContent = l.nextAt ? fmtDateTime(l.nextAt) : "Nenhuma marcada";
    renderMeetings(l.id, panelAppt);
    renderProposal(l.id);
    document.getElementById("p-phone").textContent = l.phone || "—";
    document.getElementById("p-email").textContent = l.email || "—";
    document.getElementById("p-owner").value = l.ownerId ? String(l.ownerId) : "";
    document.getElementById("p-interest").value = l.interest;
    document.getElementById("p-notes").value = l.notes;
    document.getElementById("p-origin").value = l.origin;
    document.getElementById("p-origin-detail").value = l.originDetail;
    var convo = document.getElementById("p-convo"); convo.innerHTML = "";
    if (!row.messages.length) convo.appendChild(el("span", "muted", "Sem mensagens."));
    row.messages.forEach(function (m) {
      var wrap = el("div", "msg " + (m.role === "assistant" ? "assistant" : "user"));
      var body = el("div", "msg-body"); body.textContent = m.message;
      var when = el("div", "msg-when"); when.textContent = (m.role === "assistant" ? "Agente · " : "") + fmtDateTime(toDate(m.created_at));
      wrap.appendChild(body); wrap.appendChild(when); convo.appendChild(wrap);
    });
    convo.scrollTop = convo.scrollHeight;
  }

  // "Proposta solicitada": the answers as written, the date and the status,
  // with a link to the request in its tab.
  var PROPOSAL_CLASS = { "novo": "prop-novo", "incompleto": "prop-incompleto", "proposta enviada": "prop-enviada" };
  function renderProposal(contactId) {
    var box = document.getElementById("p-proposal");
    var body = document.getElementById("p-proposal-body");
    var p = proposalOf(contactId);
    if (!p) { box.hidden = true; body.textContent = ""; return; }
    box.hidden = false;
    window.PanelProposals.detail(p.id).then(function (d) {
      if (panelId !== contactId) return;
      body.textContent = "";
      var line = el("div", "prop-card-line");
      var status = el("span", "prop-status " + (PROPOSAL_CLASS[d.status] || ""));
      status.textContent = d.status;
      line.appendChild(status);
      var when = el("span", "muted"); when.textContent = "Pedido em " + fmtDateTime(toDate(d.pedido_em));
      line.appendChild(when);
      var link = el("a", "prop-card-link"); link.textContent = "Ver pedido";
      link.href = window.PanelProposals.href(d.id);
      link.addEventListener("click", closePanel);
      line.appendChild(link);
      body.appendChild(line);
      var list = el("dl", "prop-answers");
      d.respostas.filter(function (a) { return a.resposta; }).forEach(function (a) {
        var dt = el("dt"); dt.textContent = a.rotulo;
        var dd = el("dd"); dd.textContent = a.resposta;
        list.appendChild(dt); list.appendChild(dd);
      });
      body.appendChild(list);
    }).catch(function () { box.hidden = true; });
  }

  function closePanel() {
    panelId = null; panelAppt = null;
    document.getElementById("panel").classList.remove("show");
    document.getElementById("scrim").classList.remove("show");
  }

  function saveStage(stage) {
    if (panelId == null) return;
    var id = panelId;
    patchLead(id, { stage: stage }).then(function (row) { if (panelId === id) fillPanel(row); });
  }

  document.getElementById("panel-close").addEventListener("click", closePanel);
  document.getElementById("scrim").addEventListener("click", closePanel);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closePanel(); });
  document.getElementById("p-stage").addEventListener("change", function (e) { saveStage(e.target.value); });
  document.getElementById("p-won").addEventListener("click", function () { saveStage("fechado"); });
  document.getElementById("p-lost").addEventListener("click", function () { saveStage("perdido"); });
  document.getElementById("p-save").addEventListener("click", function () {
    if (panelId == null) return;
    var id = panelId;
    var owner = document.getElementById("p-owner").value;
    var before = state.leads.find(function (l) { return l.id === id; }) || { origin: "", originDetail: "" };
    var origin = document.getElementById("p-origin").value;
    var detail = document.getElementById("p-origin-detail").value.trim();
    var originChanged = origin !== before.origin || detail !== before.originDetail;
    patchLead(id, {
      owner_staff_id: owner ? Number(owner) : null,
      interest: document.getElementById("p-interest").value.trim() || null,
      notes: document.getElementById("p-notes").value.trim() || null
    }).then(function (row) {
      if (!originChanged) return row;
      return api("/contacts/" + id + "/origin", { method: "PATCH", body: { origin: origin || null, detail: detail || null } })
        .then(function () { return api("/leads/" + id); })
        .then(function (fresh) {
          state.leads = state.leads.map(function (l) { return l.id === id ? normalize(fresh) : l; });
          renderAll();
          return fresh;
        });
    }).then(function (row) {
      if (panelId !== id) return;
      fillPanel(row);
      var flash = document.getElementById("p-saved"); flash.hidden = false;
      setTimeout(function () { flash.hidden = true; }, 2000);
    }).catch(function (err) { setStatus(err.message, true); });
  });

  // ── navigation, filters ───────────────────────────────────────────────
  var titles = {
    dashboard: ["Painel", "Como a operação comercial está hoje"],
    // Dragging needs a mouse; on a phone the stage changes from the card itself.
    funil: ["Funil", window.matchMedia("(pointer: coarse)").matches
      ? "Toque num card para mudar a etapa"
      : "Arraste um card para mudar a etapa"],
    inbox: ["Conversas", "Atendimento da equipe"],
    agenda: ["Agenda", "Todos os compromissos, e com quem cada um está"],
    leads: ["Leads & contatos", "Todo contato que já falou com o agente"],
    propostas: ["Pedidos de proposta", "Quem pediu proposta por e-mail, e até quando responder"],
    automacoes: ["Automações", "Mensagens que o agente inicia para quem sumiu"],
    admin: ["Administração", "Conversa privada da equipe e avisos do bot"],
    devices: ["Aparelhos conectados", "Acesso da equipe pelo celular e pelo navegador"]
  };
  function showView(view) {
    if (!titles[view]) return;
    document.querySelector(".app").classList.toggle("is-inbox", view === "inbox");
    document.querySelector(".app").classList.toggle("is-admin", view === "admin");
    document.querySelectorAll(".nav-item").forEach(function (i) {
      i.classList.toggle("active", i.dataset.view === view);
    });
    document.querySelectorAll(".view").forEach(function (v) { v.classList.remove("active"); });
    document.getElementById("view-" + view).classList.add("active");
    document.getElementById("view-title").textContent = titles[view][0];
    document.getElementById("view-sub").textContent = titles[view][1];
    setMenu(false);
    document.dispatchEvent(new CustomEvent("panel:view", {detail: view}));
    if (view === "devices") loadDevices();
    // The tab lives in the address, so a link to /painel#agenda opens there.
    // #inbox/<id> keeps its conversation; any other tab is just its name.
    if (location.hash.split("/")[0] !== "#" + view) history.replaceState(null, "", "#" + view);
  }
  document.querySelectorAll(".nav-item").forEach(function (item) {
    item.addEventListener("click", function () { showView(item.dataset.view); });
    item.addEventListener("keydown", function (e) { if (e.key === "Enter") showView(item.dataset.view); });
  });
  if (location.hash.length > 1) showView(location.hash.slice(1).split("/")[0]);
  function setMenu(open) {
    var sidebar = document.getElementById("sidebar");
    var menu = document.getElementById("menu-btn");
    var wasOpen = sidebar.classList.contains("open");
    sidebar.classList.toggle("open", open);
    document.querySelector(".main").inert = open;
    document.getElementById("sidebar-scrim").hidden = !open;
    menu.setAttribute("aria-expanded", String(open));
    if (open) sidebar.querySelector(".nav-item.active").focus();
    else if (wasOpen && menu.offsetParent !== null) menu.focus();
  }
  document.getElementById("menu-btn").addEventListener("click", function () {
    setMenu(!document.getElementById("sidebar").classList.contains("open"));
  });
  document.getElementById("sidebar-scrim").addEventListener("click", function () { setMenu(false); });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape") setMenu(false); });
  document.getElementById("search-input").addEventListener("input", function (e) { filters.q = e.target.value; renderTable(); });
  document.getElementById("f-stage").addEventListener("change", function (e) { filters.stage = e.target.value; renderTable(); });
  document.getElementById("f-owner").addEventListener("change", function (e) { filters.owner = e.target.value; renderTable(); });
  document.getElementById("f-channel").addEventListener("change", function (e) { filters.channel = e.target.value; renderTable(); });
  document.getElementById("f-origin").addEventListener("change", function (e) { filters.origin = e.target.value; renderTable(); });
  document.getElementById("refresh-btn").addEventListener("click", load);
  // "Atualizar agenda": ask for a pass now, then wait until every calendar
  // finished it (at most a minute) and reload.
  document.getElementById("agenda-sync-btn").addEventListener("click", function () {
    var btn = this, started = Date.now();
    btn.disabled = true; btn.textContent = "Atualizando…";
    function finish() { btn.disabled = false; btn.textContent = "Atualizar agenda"; load(); }
    function poll() {
      api("/agenda").then(function (data) {
        if ((data.sync && data.sync.done) || Date.now() - started > 60000) { finish(); return; }
        setTimeout(poll, 3000);
      }).catch(finish);
    }
    api("/agenda/sync", { method: "POST" }).then(function () { setTimeout(poll, 2000); }).catch(finish);
  });

  function loadDevices() {
    var box = document.getElementById("devices-list");
    var message = document.getElementById("devices-message");
    message.textContent = "Carregando…";
    api("/devices").then(function (data) {
      box.textContent = "";
      message.textContent = data.devices.length ? "" : "Nenhum aparelho mantido conectado.";
      data.devices.forEach(function (device) {
        var row = document.createElement("div"); row.className = "device-row";
        var info = document.createElement("div");
        var title = document.createElement("strong");
        title.textContent = device.name + " · " + (device.device_name || "Aparelho") +
          (device.is_current ? " · este aparelho" : "") +
          (device.push ? " · notificações ativas" : "");
        var last = document.createElement("p"); last.className = "hint";
        last.textContent = "Último uso: " + new Date(device.last_seen_at).toLocaleString("pt-BR");
        info.append(title, last);
        var button = document.createElement("button");
        button.type = "button"; button.className = "btn"; button.textContent = "Desconectar";
        button.addEventListener("click", function () {
          if (!window.confirm("Desconectar " + (device.device_name || "este aparelho") + " de " + device.name + "?")) return;
          button.disabled = true;
          api("/devices/" + device.id, { method: "DELETE" }).then(function (result) {
            if (result.current) window.PanelApp.signOut();
            else loadDevices();
          }).catch(function (error) { message.textContent = error.message; button.disabled = false; });
        });
        row.append(info, button); box.appendChild(row);
      });
    }).catch(function (error) { message.textContent = error.message; });
  }
  document.getElementById("devices-refresh").addEventListener("click", loadDevices);

  // ── Who is signed in, and signing out ──────────────────────────────────
  api("/me").then(function (me) {
    document.getElementById("me-name").textContent = me.name;
    document.getElementById("me").hidden = false;
    // Running low on recovery codes is worth saying before they run out.
    if (me.recovery_left <= 3) {
      var warn = document.getElementById("me-warn");
      warn.textContent = me.recovery_left === 0
        ? "Sem códigos de recuperação. Peça um novo convite para gerar outros."
        : "Restam " + me.recovery_left + " códigos de recuperação.";
      warn.hidden = false;
    }
  }).catch(function () {});
  document.getElementById("logout-btn").addEventListener("click", function () {
    fetch("/painel/api/auth/logout", { method: "POST", credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("Não foi possível sair. Tente novamente.");
        window.PanelApp.signOut();
      }).catch(function (error) { setStatus(error.message || "Reconecte-se para sair.", true); });
  });

  // propostas.js knows who asked for a proposal: tag the cards once it does,
  // and reload when one is marked sent (its card moved to "proposta").
  // The board is not rebuilt under a card being dragged (as on the 30 s reload).
  document.addEventListener("proposals:loaded", function () {
    if (!state.loadedAt) return;
    if (dragId == null) renderBoard();
    renderTable();
    if (panelId != null) renderProposal(panelId);
  });
  document.addEventListener("proposals:changed", load);

  populateFilters();
  load();
  // The "Novo lead" dialog (inbox.js) refreshes the table after adding one.
  window.PanelLeads = { reload: load };
  // New conversations arrive while the panel is open; keep it current without
  // pulling the rug out from someone mid-edit in the detail panel.
  // ...nor from under a card being dragged: the board is rebuilt on load.
  setInterval(function () { if (panelId == null && dragId == null && !document.hidden) load(); }, 30000);
})();
