/* Charges in the Inbox: the "Cobrar" dialog and the conversation's list.
   Descriptions come from the team, but everything is still written with
   textContent. The payment link is never shown here: it goes to the customer. */
(function () {
  "use strict";

  var MAX_CENTS = 10000000;
  var METHODS = { link: "Link", boleto: "Boleto", pix: "Pix", cartao: "Cartão" };
  var STATUS = { pendente: "Em aberto", pago: "Pago", vencido: "Vencido", cancelado: "Cancelado",
                 estornado: "Estornado", aguardando_aprovacao: "Aguardando aprovação",
                 recusada: "Recusada" };

  // "1.234,56", "1234,56", "1234.56", "12,5", "19" → cents; anything else → null.
  function parseReais(text) {
    var s = String(text == null ? "" : text).replace(/R\$/i, "").replace(/\s+/g, "");
    var whole, frac;
    var m = /^(\d{1,3}(?:\.\d{3})+)(?:,(\d{1,2}))?$/.exec(s);
    if (m) { whole = m[1].replace(/\./g, ""); frac = m[2] || ""; }
    else if ((m = /^(\d+)(?:,(\d{1,2}))?$/.exec(s))) { whole = m[1]; frac = m[2] || ""; }
    else if ((m = /^(\d+)\.(\d{1,2})$/.exec(s))) { whole = m[1]; frac = m[2]; }
    else return null;
    var cents = parseInt(whole, 10) * 100 + parseInt((frac + "00").slice(0, 2), 10);
    return cents > 0 && cents <= MAX_CENTS ? cents : null;
  }

  function formatBrl(cents) {
    var reais = Math.floor(cents / 100), rest = cents % 100;
    return "R$ " + String(reais).replace(/\B(?=(\d{3})+(?!\d))/g, ".") + "," + (rest < 10 ? "0" : "") + rest;
  }

  function formatDate(iso) {
    var p = String(iso || "").split("-");
    return p.length === 3 ? p[2] + "/" + p[1] + "/" + p[0] : "";
  }

  // Wired to the page only in a browser; the tests load just the helpers.
  function create(options) {
    var $ = function (id) { return document.getElementById(id); };
    var dialog = $("billing-dialog"), list = $("thread-billing");
    var contactId = null, holder = null, summary = null, busy = false;

    function el(tag, cls, text) {
      var n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    }
    function button(label, cls, onClick) {
      var b = el("button", cls, label); b.type = "button"; b.addEventListener("click", onClick); return b;
    }
    function status(text, isError) {
      var n = $("billing-msg"); n.className = "auto-msg" + (isError ? " err" : ""); n.textContent = text;
    }

    function render() {
      list.textContent = "";
      if (!summary) { list.hidden = true; return; }
      list.hidden = false;
      list.appendChild(el("h3", "billing-title", "Cobranças"));
      if (!summary.charges.length) { list.appendChild(el("p", "billing-empty", "Nenhuma cobrança.")); return; }
      summary.charges.forEach(function (c) {
        var row = el("div", "billing-row");
        var what = el("div", "billing-what");
        what.appendChild(el("strong", null, c.description));
        var state = STATUS[c.status] || c.status;
        if (c.status === "pendente" && !c.sent) state = "Não enviada";
        what.appendChild(el("span", "billing-sub", formatBrl(c.value_cents) + " · " +
          METHODS[c.method] + " · vence " + formatDate(c.due_date) + " · " + state));
        row.appendChild(what);
        if (c.status === "pendente") {
          var tools = el("div", "billing-tools");
          if (holder && holder.mine && (holder.window_open !== false || holder.charge_after_window)) {
            tools.appendChild(button("Reenviar", "btn", function () { act(c.id, "resend"); }));
          }
          tools.appendChild(button("Cancelar", "btn", function () {
            if (window.confirm("Cancelar a cobrança de " + formatBrl(c.value_cents) + "? O cliente não é avisado.")) {
              act(c.id, "cancel");
            }
          }));
          row.appendChild(tools);
        }
        list.appendChild(row);
      });
    }

    function load(id, contact) {
      contactId = id; holder = contact;
      if (!contact || !contact.billing_enabled) { summary = null; render(); return; }
      options.api("/inbox/" + id + "/billing").then(function (data) {
        if (id !== contactId) return;
        summary = data; render();
      }).catch(function () { if (id === contactId) { summary = null; render(); } });
    }

    function act(chargeId, kind) {
      var id = contactId;
      options.note("", false);
      options.api("/inbox/" + id + "/billing/" + chargeId + "/" + kind, { method: "POST" })
        .then(function (data) { if (id === contactId) { options.applied(data); load(id, data.contact); } })
        .catch(function (err) { options.note(err.message, true); load(id, holder); });
    }

    function open() {
      if (busy) return;
      if (!summary) { options.note("Carregando as cobranças… tente de novo.", false); load(contactId, holder); return; }
      $("billing-description").value = "";
      $("billing-value").value = "";
      $("billing-due").value = summary.default_due;
      $("billing-due").min = summary.min_due || "";
      $("billing-due").max = summary.max_due || "";
      $("billing-method").value = "link";
      var needsTaxId = !summary.tax_id;
      $("billing-taxid-field").hidden = !needsTaxId;
      $("billing-taxid").value = "";
      $("billing-taxid-known").hidden = needsTaxId;
      $("billing-taxid-known").textContent = needsTaxId ? "" : "CPF/CNPJ: " + summary.tax_id;
      status("", false);
      $("billing-submit").disabled = false;
      dialog.showModal();
      $("billing-description").focus();
    }

    function submit(event) {
      event.preventDefault();
      if (busy) return;
      var cents = parseReais($("billing-value").value);
      if (cents == null) { status("Valor inválido. Ex.: 1.234,56 (até R$ 100.000,00).", true); return; }
      var body = {
        description: $("billing-description").value.trim(),
        value_cents: cents,
        due_date: $("billing-due").value,
        method: $("billing-method").value,
        tax_id: summary && !summary.tax_id ? $("billing-taxid").value.trim() : null
      };
      if (!body.description) { status("Escreva a descrição.", true); return; }
      var id = contactId;
      busy = true; $("billing-submit").disabled = true; status("Criando a cobrança…", false);
      options.api("/inbox/" + id + "/billing", { method: "POST", body: body }).then(function (data) {
        dialog.close();
        if (id === contactId) { options.applied(data); load(id, data.contact); }
      }).catch(function (err) {
        status(err.message, true);
        load(id, holder);
      }).then(function () { busy = false; $("billing-submit").disabled = false; });
    }

    $("billing-form").addEventListener("submit", submit);
    $("billing-cancel").addEventListener("click", function () { dialog.close(); });

    return {
      load: load,
      open: open,
      reset: function () { contactId = null; holder = null; summary = null; render(); }
    };
  }

  window.PanelBilling = { parseReais: parseReais, formatBrl: formatBrl, create: create };
})();
