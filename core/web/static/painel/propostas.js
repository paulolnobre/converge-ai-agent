/* Pedidos de proposta — the tab in /painel listing who asked for a proposal by
   e-mail, by when the team owes it, and "Marcar proposta como enviada".
   Answers are written by strangers, so everything here goes in as textContent.
   Also keeps window.PanelProposals: the request of each contact, for the
   "proposta solicitada" tag on the funnel, the contact card and the Inbox. */
(function () {
  "use strict";

  var FILTERS = [
    { id: "", label: "Todos" },
    { id: "novo", label: "Novos" },
    { id: "incompleto", label: "Incompletos" },
    { id: "proposta enviada", label: "Proposta enviada" }
  ];
  var STATUS_CLASS = { "novo": "prop-novo", "incompleto": "prop-incompleto", "proposta enviada": "prop-enviada" };

  var rows = [], filter = "", openId = null, loaded = false;
  var byContact = {};

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function api(path, options) {
    var opts = options || {};
    return fetch("/painel/api/proposals" + path, {
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
          throw new Error((data && typeof data.detail === "string" && data.detail) || "Erro " + r.status);
        }
        return data;
      });
    });
  }

  // The proposal made from the model (core/services/proposal_pdf.py), sent
  // the same way as an uploaded PDF.
  function sendGenerated(id) {
    return fetch("/painel/api/proposals/" + id + "/document/generated", {
      method: "POST", credentials: "same-origin"
    }).then(function (r) {
      if (r.status === 401) {
        if (window.PanelApp) window.PanelApp.signOut();
        throw new Error("Sessão expirada.");
      }
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error((data && data.detail) || "Erro " + r.status);
        return data;
      });
    });
  }

  // Ask the person for more information (core/services/proposal_info.py): one
  // message with "Conversar 30 min" and "Falar com a equipe".
  function sendToSign(id) { return postAction(id, "/contract/sign"); }

  function postAction(id, path) {
    return fetch("/painel/api/proposals/" + id + path, {
      method: "POST", credentials: "same-origin"
    }).then(function (r) {
      if (r.status === 401) {
        if (window.PanelApp) window.PanelApp.signOut();
        throw new Error("Sessão expirada.");
      }
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error((data && data.detail) || "Erro " + r.status);
        return data;
      });
    });
  }

  function askInfo(id) {
    return fetch("/painel/api/proposals/" + id + "/info", {
      method: "POST", credentials: "same-origin"
    }).then(function (r) {
      if (r.status === 401) {
        if (window.PanelApp) window.PanelApp.signOut();
        throw new Error("Sessão expirada.");
      }
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error((data && data.detail) || "Erro " + r.status);
        return data;
      });
    });
  }

  function sendDocument(id, file) {
    var meta = JSON.stringify({ nonce: crypto.randomUUID(), kind: "document",
                                filename: file.name.slice(-120) });
    var header = btoa(Array.from(new TextEncoder().encode(meta),
                                 function (n) { return String.fromCharCode(n); }).join(""));
    return fetch("/painel/api/proposals/" + id + "/document", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/octet-stream", "X-Upload-Metadata": header },
      body: file
    }).then(function (r) {
      if (r.status === 401) {
        if (window.PanelApp) window.PanelApp.signOut();
        throw new Error("Sessão expirada.");
      }
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error((data && data.detail) || "Erro " + r.status);
        return data;
      });
    });
  }

  // "2026-11-03" is a calendar day: read it as one, never as UTC midnight.
  function fmtDay(iso) {
    if (!iso) return "—";
    var p = String(iso).slice(0, 10).split("-");
    return p[2] + "/" + p[1] + "/" + p[0];
  }
  function fmtWhen(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    return d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric" }) + " às " +
      d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  }
  function dueText(r) {
    if (r.atrasado) return "Vencido em " + fmtDay(r.prazo);
    if (r.vence_hoje) return "Vence hoje";
    return "Até " + fmtDay(r.prazo);
  }
  function who(r) { return r.nome || "Sem nome"; }
  function company(r) { return [r.empresa, r.ramo].filter(Boolean).join(" · "); }
  function statusTag(status) { return el("span", "prop-status " + (STATUS_CLASS[status] || ""), status); }

  // ── Shared with the rest of the panel ───────────────────────────────────
  function index() {
    byContact = {};
    // Rows come open-first: the first one seen for a contact is the one that matters.
    rows.forEach(function (r) { if (!byContact[r.contact_id]) byContact[r.contact_id] = r; });
    var open = rows.filter(function (r) { return r.status !== "proposta enviada"; }).length;
    var count = $("count-propostas");
    count.textContent = open;
    count.hidden = open === 0;
    document.dispatchEvent(new CustomEvent("proposals:loaded"));
  }
  window.PanelProposals = {
    forContact: function (contactId) { return byContact[contactId] || null; },
    detail: function (id) { return api("/" + id); },
    // A link to the request in this tab.
    href: function (id) { return "#propostas/" + id; }
  };

  // ── The list ────────────────────────────────────────────────────────────
  function load() {
    return api("").then(function (data) {
      rows = data.proposals || [];
      loaded = true;
      index();
      render();
    }).catch(function (err) {
      var list = $("prop-list");
      list.textContent = "";
      list.appendChild(el("p", "empty-line", "Não foi possível carregar os pedidos: " + err.message));
    });
  }

  function renderFilters() {
    var wrap = $("prop-filter"); wrap.textContent = "";
    FILTERS.forEach(function (f) {
      var n = f.id ? rows.filter(function (r) { return r.status === f.id; }).length : rows.length;
      var chip = el("button", "agenda-chip " + (filter === f.id ? "on" : "off"));
      chip.type = "button";
      chip.appendChild(document.createTextNode(f.label + " "));
      chip.appendChild(el("span", "count", String(n)));
      chip.setAttribute("aria-pressed", filter === f.id ? "true" : "false");
      chip.addEventListener("click", function () { filter = f.id; render(); });
      wrap.appendChild(chip);
    });
  }

  function render() {
    renderFilters();
    var list = $("prop-list"); list.textContent = "";
    var shown = rows.filter(function (r) { return !filter || r.status === filter; });
    if (!shown.length) {
      list.appendChild(el("p", "empty-line", rows.length
        ? "Nenhum pedido com esse status."
        : "Nenhum pedido de proposta ainda. Aparecem aqui quando alguém envia o questionário pelo atendimento."));
      return;
    }
    shown.forEach(function (r) {
      var row = el("button", "prop-row" + (r.atrasado ? " late" : "") + (r.vence_hoje ? " today" : "") +
        (r.id === openId ? " active" : ""));
      row.type = "button";
      row.dataset.id = r.id;
      var main = el("div", "prop-main");
      main.appendChild(el("span", "prop-name", who(r)));
      main.appendChild(el("span", "prop-org", company(r) || "Empresa não informada"));
      var side = el("div", "prop-side");
      side.appendChild(statusTag(r.status));
      side.appendChild(el("span", "prop-due", r.status === "proposta enviada"
        ? "Pedido em " + fmtDay(r.pedido_em) : dueText(r)));
      side.appendChild(el("span", "prop-when", "Pedido em " + fmtWhen(r.pedido_em)));
      row.appendChild(main); row.appendChild(side);
      row.addEventListener("click", function () { open(r.id); });
      list.appendChild(row);
    });
  }

  // ── One request ─────────────────────────────────────────────────────────
  function open(id) {
    openId = id;
    history.replaceState(null, "", "#propostas/" + id);
    $("view-propostas").classList.add("prop-open");
    Array.prototype.forEach.call(document.querySelectorAll(".prop-row"), function (r) {
      r.classList.toggle("active", r.dataset.id === String(id));
    });
    var box = $("prop-detail"); box.hidden = false; box.textContent = "";
    box.appendChild(el("p", "empty-line", "Carregando…"));
    api("/" + id).then(function (data) { if (openId === id) renderDetail(data); })
      .catch(function (err) {
        if (openId !== id) return;
        box.textContent = "";
        box.appendChild(el("p", "empty-line", err.message));
      });
  }

  function close() {
    openId = null;
    history.replaceState(null, "", "#propostas");
    $("view-propostas").classList.remove("prop-open");
    $("prop-detail").hidden = true;
    render();
  }

  var BRL = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
  function money(cents) { return BRL.format((cents || 0) / 100); }
  // "1500,50", "1.500,50", "3.300" (a thousand separator) or "1500.50" typed;
  // reais with a decimal point sent.
  function toReais(text) {
    var t = String(text || "").trim().replace(/\s|R\$/g, "");
    if (t.indexOf(",") >= 0) return t.replace(/\./g, "").replace(",", ".");
    if (/^\d{1,3}(\.\d{3})+$/.test(t)) return t.replace(/\./g, "");
    return t;
  }
  var DETAIL_LABEL = {
    base: "Base", canais_e_integracoes: "Canais e integrações", documentos: "Documentos",
    fator: "Fator (infra × processo)", sustentacao: "Sustentação", trafego: "Tráfego",
    usuarios: "Usuários", sla: "SLA"
  };

  // The preliminary quote: only the team sees it. Values in cents.
  function renderQuote(r, body) {
    var q = r.orcamento;
    if (!q) return;
    var box = el("section", "prop-quote");
    box.appendChild(el("h3", null, "Orçamento preliminar"));
    if (q.indisponivel) {
      box.appendChild(el("p", "hint", "Orçamento indisponível no momento. As respostas acima continuam valendo."));
      body.appendChild(box);
      return;
    }
    if (q.faltam) {
      box.appendChild(el("p", "hint", "Sem cálculo: faltam " + q.faltam.join(", ") + "."));
      body.appendChild(box);
      return;
    }
    var kv = el("div", "kv");
    kv.appendChild(field("Implantação", money(q.atual.implantacao) + " (faixa " +
      money(q.calculado.faixa_min) + " a " + money(q.calculado.faixa_max) + ")"));
    // How the setup is paid, as the team chose: half and half, or monthly.
    var plan = q.plano || {};
    kv.appendChild(field("Pagamento da implantação", plan.count <= 1
      ? "À vista, na assinatura."
      : plan.forma === "metade"
        ? "50% na assinatura (" + money(plan.first) + ") e 50% na entrega (" + money(plan.each) + ")."
        : plan.count + "x: " + money(plan.first) + " na assinatura e " + (plan.count - 1) +
          "x mensais de " + money(plan.each) + ". No cartão, mais a taxa do cartão."));
    kv.appendChild(field("Mensalidade", money(q.atual.mensalidade)));
    box.appendChild(kv);
    (q.avisos || []).forEach(function (a) { box.appendChild(el("p", "prop-quote-warn", a)); });
    if (q.editado_por || q.editado_em) {
      box.appendChild(el("p", "hint", "Editado" + (q.editado_por ? " por " + q.editado_por : "") +
        (q.editado_em ? " em " + fmtWhen(q.editado_em) : "") + ". Calculado: implantação " +
        money(q.calculado.implantacao) + ", mensalidade " + money(q.calculado.mensalidade) + "."));
    }
    if (q.observacao) box.appendChild(el("p", null, "Observação: " + q.observacao));
    var how = el("details");
    how.appendChild(el("summary", null, "Como chegamos"));
    var dl = el("dl", "prop-answers");
    Object.keys(q.detalhe || {}).forEach(function (k) {
      dl.appendChild(el("dt", null, DETAIL_LABEL[k] || k));
      dl.appendChild(el("dd", null, k === "fator" ? "× " + String(q.detalhe[k]).replace(".", ",") : money(q.detalhe[k])));
    });
    how.appendChild(dl);
    if ((q.premissas || []).length) {
      how.appendChild(el("p", "hint", "Premissas: " + q.premissas.join("; ") + "."));
    }
    box.appendChild(how);

    var form = el("form", "prop-quote-form");
    form.hidden = true;
    function input(label, value, type) {
      var wrap = el("label", "field");
      wrap.appendChild(el("span", "field-label", label));
      var i = el(type === "area" ? "textarea" : "input");
      if (type !== "area") { i.type = "text"; i.inputMode = "decimal"; }
      i.value = value;
      wrap.appendChild(i);
      form.appendChild(wrap);
      return i;
    }
    var setup = input("Implantação (R$)", (q.atual.implantacao / 100).toFixed(2).replace(".", ","));
    var monthly = input("Mensalidade (R$)", (q.atual.mensalidade / 100).toFixed(2).replace(".", ","));
    var termsWrap = el("label", "field");
    termsWrap.appendChild(el("span", "field-label", "Pagamento da implantação"));
    var terms = el("select");
    [["metade", "50% na assinatura e 50% na entrega"],
     ["parcelado", "Parcelado em " + q.parcelas_max + "x, a 1ª na assinatura"]].forEach(function (o) {
      var opt = el("option", null, o[1]);
      opt.value = o[0];
      terms.appendChild(opt);
    });
    terms.value = q.atual.forma || "metade";
    termsWrap.appendChild(terms);
    form.appendChild(termsWrap);
    var note = input("Observação", q.observacao || "", "area");
    note.maxLength = 500;
    var save = el("button", "btn btn-accent", "Salvar valores");
    save.type = "submit";
    var msg = el("span", "auto-msg");
    msg.setAttribute("role", "status");
    form.appendChild(save); form.appendChild(msg);
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      save.disabled = true;
      api("/" + r.id + "/quote", { method: "PATCH", body: {
        implantacao: toReais(setup.value), mensalidade: toReais(monthly.value),
        forma: terms.value,
        observacao: note.value.trim() || null
      } }).then(function (fresh) {
        if (openId === r.id) renderDetail(fresh);
      }).catch(function (err) {
        save.disabled = false;
        msg.className = "auto-msg err";
        msg.textContent = err.message === "Erro 422"
          ? "Confira os valores: números maiores que zero, com até 2 casas." : err.message;
      });
    });
    var editBtn = el("button", "btn", "Editar valores");
    editBtn.type = "button";
    editBtn.addEventListener("click", function () { form.hidden = false; editBtn.hidden = true; });
    box.appendChild(editBtn);
    box.appendChild(form);
    body.appendChild(box);
  }

  function field(label, value) {
    var f = el("div", "field");
    f.appendChild(el("span", "field-label", label));
    f.appendChild(el("span", "field-value", value || "—"));
    return f;
  }

  function renderDetail(r) {
    var box = $("prop-detail"); box.textContent = "";
    var head = el("div", "card-head");
    var title = el("div");
    var back = el("button", "btn prop-back", "← Pedidos");
    back.type = "button";
    back.addEventListener("click", close);
    title.appendChild(el("h2", null, who(r)));
    title.appendChild(el("span", "hint", company(r) || "Empresa não informada"));
    head.appendChild(back); head.appendChild(title); head.appendChild(statusTag(r.status));
    box.appendChild(head);

    var body = el("div", "card-body prop-body");
    var kv = el("div", "kv");
    kv.appendChild(field("E-mail", r.email));
    kv.appendChild(field("Telefone", r.telefone));
    kv.appendChild(field("Pedido em", fmtWhen(r.pedido_em)));
    var due = field("Prazo", r.status === "proposta enviada" ? fmtDay(r.prazo) : dueText(r));
    if (r.atrasado) due.classList.add("prop-late");
    if (r.vence_hoje) due.classList.add("prop-today");
    kv.appendChild(due);
    body.appendChild(kv);

    var answers = el("dl", "prop-answers");
    (r.respostas || []).forEach(function (a) {
      answers.appendChild(el("dt", null, a.rotulo));
      var dd = el("dd", a.resposta ? null : "muted", a.resposta || "Sem resposta");
      if (a.do_preenchimento) dd.appendChild(el("span", "prop-prefilled", "do que a pessoa já tinha dito"));
      answers.appendChild(dd);
    });
    body.appendChild(answers);
    renderQuote(r, body);
    if (r.contrato) {
      // What the customer sent for the contract (after "Aceitar proposta").
      var contract = el("section", "prop-quote");
      contract.appendChild(el("h3", null, "Dados do contrato"));
      var list = el("dl", "prop-answers");
      r.contrato.forEach(function (item) {
        list.appendChild(el("dt", null, item.rotulo));
        list.appendChild(el("dd", null, item.valor));
      });
      contract.appendChild(list);
      // The contract made from the model with these data and the quote.
      var checkContract = el("a", "btn", "Conferir contrato");
      checkContract.href = "/painel/api/proposals/" + r.id + "/contract.pdf";
      checkContract.target = "_blank"; checkContract.rel = "noopener";
      var contractActions = el("div", "prop-actions");
      contractActions.appendChild(checkContract);
      contract.appendChild(contractActions);
      if (r.assinatura) {
        var s = r.assinatura;
        if (s.status === "assinado") {
          contract.appendChild(el("p", "hint", "Assinado pelas duas partes em " + fmtWhen(s.assinado_em) +
            (s.sandbox ? " (sandbox da Autentique, sem valor jurídico)." : ".")));
          if (s.arquivo_assinado) {
            var signedCopy = el("a", "btn", "Baixar contrato assinado");
            signedCopy.href = s.arquivo_assinado;
            signedCopy.target = "_blank"; signedCopy.rel = "noopener noreferrer";
            contractActions.appendChild(signedCopy);
          }
        } else if (s.status === "enviado") {
          contract.appendChild(el("p", "hint", "Enviado para assinatura em " + fmtWhen(s.enviado_em) +
            (s.enviado_por ? " por " + s.enviado_por : "") +
            (s.sandbox ? " (sandbox da Autentique, sem valor jurídico)." : ".")));
          var parties = [];
          if (s.cliente_assinou_em) parties.push("o cliente assinou em " + fmtWhen(s.cliente_assinou_em));
          if (s.converge_assinou_em) parties.push("a Converge assinou em " + fmtWhen(s.converge_assinou_em));
          contract.appendChild(el("p", "hint", parties.length
            ? parties.join("; ") + ". Falta a outra assinatura."
            : "Ninguém assinou ainda."));
        } else if (s.status === "enviando") {
          contract.appendChild(el("p", "hint", "Enviando para a Autentique…"));
        } else {
          // Never confirmed: it may be on Autentique, e-mailed already.
          contract.appendChild(el("p", "hint prop-late",
            "A Autentique não confirmou o envio. Confira no painel da Autentique: " +
            "se o contrato não estiver lá, libere para enviar de novo."));
          var free = el("button", "btn", "Liberar para reenviar");
          free.type = "button";
          var freeNote = el("span", "auto-msg");
          freeNote.setAttribute("role", "status");
          free.addEventListener("click", function () {
            if (!window.confirm("Você conferiu no painel da Autentique que este contrato não está lá? " +
                                "Se estiver, enviar de novo cria um segundo documento.")) return;
            free.disabled = true;
            postAction(r.id, "/contract/release").then(function () {
              return api("/" + r.id).then(function (fresh) { if (openId === r.id) renderDetail(fresh); });
            }).catch(function (err) {
              free.disabled = false;
              freeNote.className = "auto-msg err"; freeNote.textContent = err.message;
            });
          });
          contractActions.appendChild(free);
          contractActions.appendChild(freeNote);
        }
      } else if (r.assinatura_pronta) {
        // The same contract "Conferir contrato" shows, to Autentique: first checked, then sent.
        var sign = el("button", "btn btn-accent", "Enviar para assinatura");
        sign.type = "button";
        var signNote = el("span", "auto-msg");
        signNote.setAttribute("role", "status");
        sign.addEventListener("click", function () {
          if (!window.confirm("Enviar o contrato para " + who(r) +
                              " assinar? Confira o PDF antes, em Conferir contrato.")) return;
          sign.disabled = true;
          signNote.className = "auto-msg"; signNote.textContent = "Enviando…";
          sendToSign(r.id).then(function (result) {
            signNote.className = "auto-msg ok"; signNote.textContent = result.notice || "Enviado.";
            return api("/" + r.id).then(function (fresh) { if (openId === r.id) renderDetail(fresh); });
          }).catch(function (err) {
            sign.disabled = false;
            signNote.className = "auto-msg err"; signNote.textContent = err.message;
            // An unconfirmed send leaves the card in a new state: shown after the message is read.
            setTimeout(function () {
              api("/" + r.id).then(function (fresh) { if (openId === r.id) renderDetail(fresh); });
            }, 4000);
          });
        });
        contractActions.appendChild(sign);
        contractActions.appendChild(signNote);
      }
      body.appendChild(contract);
    }

    if (r.status === "proposta enviada") {
      body.appendChild(el("p", "hint", "Proposta marcada como enviada em " + fmtWhen(r.proposta_enviada_em) +
        (r.proposta_enviada_por ? " por " + r.proposta_enviada_por : "") + "."));
    }

    var actions = el("div", "prop-actions");
    var convo = el("a", "btn", "Abrir conversa");
    convo.href = "#inbox/" + r.contact_id;
    actions.appendChild(convo);
    var note = el("span", "auto-msg");
    note.setAttribute("role", "status");
    if (r.status !== "proposta enviada") {
      var done = function (result) {
        note.className = "auto-msg ok"; note.textContent = result.notice || "Proposta enviada.";
        document.dispatchEvent(new CustomEvent("proposals:changed"));
        load();
        return api("/" + r.id).then(function (fresh) { if (openId === r.id) renderDetail(fresh); });
      };
      // The proposal made from the model with this request's answers and quote:
      // first checked (it opens in a new tab), then sent.
      var hasQuote = !!(r.orcamento && r.orcamento.atual);
      if (hasQuote) {
        var check = el("a", "btn", "Conferir proposta");
        check.href = "/painel/api/proposals/" + r.id + "/pdf";
        check.target = "_blank"; check.rel = "noopener";
        actions.appendChild(check);
        var generated = el("button", "btn btn-accent", "Enviar proposta");
        generated.type = "button";
        generated.addEventListener("click", function () {
          if (!window.confirm("Enviar a proposta gerada para " + who(r) +
                              "? Confira o PDF antes, em Conferir proposta.")) return;
          generated.disabled = true;
          note.className = "auto-msg"; note.textContent = "Enviando…";
          sendGenerated(r.id).then(done).catch(function (err) {
            generated.disabled = false;
            note.className = "auto-msg err"; note.textContent = err.message;
          });
        });
        actions.appendChild(generated);
      }
      // A PDF of your own, to the person on WhatsApp or Telegram, with the decision buttons.
      var pick = el("input");
      pick.type = "file"; pick.accept = ".pdf,application/pdf"; pick.hidden = true;
      var send = el("button", hasQuote ? "btn" : "btn btn-accent",
                    hasQuote ? "Enviar outro PDF" : "Enviar proposta (PDF)");
      send.type = "button";
      send.addEventListener("click", function () { pick.click(); });
      pick.addEventListener("change", function () {
        var file = pick.files && pick.files[0];
        pick.value = "";
        if (!file) return;
        if (!window.confirm("Enviar " + file.name + " para " + who(r) + "?")) return;
        send.disabled = true;
        note.className = "auto-msg"; note.textContent = "Enviando…";
        sendDocument(r.id, file).then(done).catch(function (err) {
          send.disabled = false;
          note.className = "auto-msg err"; note.textContent = err.message;
        });
      });
      actions.appendChild(send);
      actions.appendChild(pick);
      body.appendChild(el("p", "hint",
        "Para enviar o PDF, assuma a conversa no Inbox (Abrir conversa → Assumir)."));
      var more = el("button", "btn", "Pedir mais informações");
      more.type = "button";
      more.addEventListener("click", function () {
        if (!window.confirm("Pedir mais informações para " + who(r) +
                            "? Ela recebe dois botões: conversar por aqui ou agendar uma conversa.")) return;
        more.disabled = true;
        note.className = "auto-msg"; note.textContent = "Enviando…";
        askInfo(r.id).then(function (result) {
          more.disabled = false;
          note.className = "auto-msg ok"; note.textContent = result.notice || "Pedido enviado.";
        }).catch(function (err) {
          more.disabled = false;
          note.className = "auto-msg err"; note.textContent = err.message;
        });
      });
      actions.appendChild(more);
      var mark = el("button", "btn", "Marcar como enviada (por e-mail)");
      mark.type = "button";
      mark.addEventListener("click", function () {
        mark.disabled = true;
        api("/" + r.id + "/sent", { method: "POST" }).then(function (fresh) {
          if (openId === r.id) renderDetail(fresh);
          // The card moved to "proposta": the funnel reloads too.
          document.dispatchEvent(new CustomEvent("proposals:changed"));
          load();
        }).catch(function (err) {
          mark.disabled = false;
          note.className = "auto-msg err"; note.textContent = err.message;
          load();
        });
      });
      actions.appendChild(mark);
    }
    actions.appendChild(note);
    body.appendChild(actions);
    box.appendChild(body);
  }

  // ── Opened from the menu, from a link, and kept fresh ───────────────────
  var nav = document.querySelector('.nav-item[data-view="propostas"]');
  function onOpen() {
    if (openId != null) history.replaceState(null, "", "#propostas/" + openId);
    load();
  }
  nav.addEventListener("click", onOpen);
  nav.addEventListener("keydown", function (e) { if (e.key === "Enter") onOpen(); });

  function fromHash() {
    var m = location.hash.match(/^#propostas\/(\d+)$/);
    if (!m) return;
    if (!$("view-propostas").classList.contains("active")) nav.click();
    open(Number(m[1]));
  }
  window.addEventListener("hashchange", fromHash);

  if (location.hash.split("/")[0] === "#propostas") { load(); fromHash(); }
  else load();

  setInterval(function () {
    if (document.hidden) return;
    var busy = document.activeElement && document.activeElement.tagName === "BUTTON" &&
      $("prop-detail").contains(document.activeElement);
    if (loaded && !busy) load();
  }, 60000);
})();
