/* The administrative room. Private messages never enter browser storage. */
(function () {
  "use strict";
  const nav = document.getElementById("admin-nav");
  const view = document.getElementById("view-admin");
  const list = document.getElementById("admin-messages");
  const form = document.getElementById("admin-form");
  const field = document.getElementById("admin-text");
  const note = document.getElementById("admin-note");
  const send = document.getElementById("admin-send");
  const older = document.getElementById("admin-older");
  const attachments = window.PanelAttachments.create(document.getElementById("admin-attachments"), {
    caption: () => field.value, url: () => "/painel/api/admin/attachments",
    sent: (_, caption) => { if (field.value.trim() === caption) field.value = ""; load(); }, refresh: () => load()
  });
  let allowed = false, loading = false, posting = false, pending = null;
  let first = 0, last = 0, own = 0;

  async function api(suffix, options = {}) {
    const response = await fetch("/painel/api/admin" + suffix, {
      credentials: "same-origin", cache: "no-store", ...options
    });
    if (response.status === 401) { window.PanelApp.signOut(); throw new Error("Entre novamente."); }
    if (response.status === 403) {
      allowed = false; attachments.setEnabled(false); nav.hidden = true; list.replaceChildren(); form.hidden = true;
      document.getElementById("admin-members").textContent = "";
      document.getElementById("admin-health").textContent = "";
      throw new Error("Você não participa do grupo administrativo.");
    }
    if (!response.ok) {
      let data = {}; try { data = await response.json(); } catch (_) { /* fixed fallback */ }
      throw new Error(typeof data.detail === "string" ? data.detail : "Não consegui atualizar o grupo. Tente de novo.");
    }
    return response.json();
  }

  function render(row, prepend) {
    if (document.getElementById("admin-message-" + row.id)) return;
    const item = document.createElement("li"); item.id = "admin-message-" + row.id;
    item.className = "admin-message " + (row.sender_id === own ? "mine" : "") +
      (row.kind === "alert" ? " alert" : "");
    const meta = document.createElement("p"); meta.className = "hint";
    meta.textContent = row.sender + " · " + new Date(row.created_at).toLocaleString("pt-BR");
    const text = document.createElement("p"); text.textContent = row.text;
    item.append(meta, text);
    if (row.media) item.append(window.PanelAttachments.renderMedia(row.media, "/painel/api/admin/media/" + row.media.id));
    if (prepend) list.prepend(item); else list.append(item);
    first = first ? Math.min(first, row.id) : row.id;
    last = Math.max(last, row.id);
  }

  async function load(previous = false) {
    if (!allowed || loading || document.hidden) return;
    loading = true;
    const nearBottom = list.scrollHeight - list.clientHeight - list.scrollTop < 90;
    const height = list.scrollHeight;
    try {
      const query = previous ? "?before=" + first : "?after=" + last;
      const data = await api(query);
      if (!allowed) return;
      own = data.user_id;
      attachments.setEnabled(allowed && data.uploads_enabled);
      document.getElementById("admin-members").textContent =
        data.members.map(person => person.name).join(", ") + " e Converge";
      document.getElementById("admin-health").textContent = data.status;
      const rows = previous ? [...data.messages].reverse() : data.messages;
      rows.forEach(row => render(row, previous));
      if (previous) list.scrollTop += list.scrollHeight - height;
      else if (nearBottom) list.scrollTop = list.scrollHeight;
      if (!last || previous || query === "?after=0") older.hidden = data.messages.length < 50;
      note.textContent = ""; form.hidden = false;
    } catch (error) { note.textContent = error.message; }
    finally { loading = false; }
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (attachments.hasFile()) { attachments.send(); return; }
    const text = field.value.trim();
    if (!allowed || posting || !text) return;
    // A lost response can be retried with the same nonce, without a duplicate.
    if (!pending || pending.text !== text) pending = {text, nonce: crypto.randomUUID()};
    posting = true; send.disabled = true; field.disabled = true;
    try {
      await api("", {method: "POST", headers: {"Content-Type": "application/json"},
                      body: JSON.stringify(pending)});
      field.value = ""; pending = null;
      await load(); list.scrollTop = list.scrollHeight;
    } catch (error) { note.textContent = error.message; }
    finally { posting = false; send.disabled = !allowed; field.disabled = !allowed; }
  });
  field.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault(); form.requestSubmit();
    }
  });
  older.addEventListener("click", () => load(true));
  document.addEventListener("panel:view", event => { if (event.detail === "admin") load(); });
  window.addEventListener("hashchange", () => {
    if (location.hash === "#admin" && allowed) nav.click();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && view.classList.contains("active")) load();
  });
  setInterval(() => { if (view.classList.contains("active")) load(); }, 5000);
  api("/access").then(data => {
    allowed = data.allowed; nav.hidden = !allowed;
    if (location.hash === "#admin" && allowed) nav.click();
    else if (view.classList.contains("active")) {
      if (allowed) load(); else note.textContent = "Você não participa do grupo administrativo.";
    }
  }).catch(error => { if (view.classList.contains("active")) note.textContent = error.message; });
})();
