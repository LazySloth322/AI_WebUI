/* Local AI Chat — frontend logic (vanilla JS, no dependencies) */
"use strict";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);

const state = {
  settings: {},
  conversations: [],
  currentId: null,
  messages: [],
  pendingAttachments: [],
  streaming: false,
  abort: null,
  theme: localStorage.getItem("theme") || "light",
};

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtTok(n) {
  if (n === null || n === undefined) return "0";
  return Number(n).toLocaleString("en-US");
}

function fmtTime(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return sameDay ? time : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + time;
}

function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function svgIcon(name) {
  const icons = {
    pencil: '<svg viewBox="0 0 24 24" class="ic"><path d="M4 20l4-1L20 7l-3-3L5 16l-1 4z"/></svg>',
    trash: '<svg viewBox="0 0 24 24" class="ic"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>',
    copy: '<svg viewBox="0 0 24 24" class="ic"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h9"/></svg>',
    refresh: '<svg viewBox="0 0 24 24" class="ic"><path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"/></svg>',
  };
  return icons[name] || "";
}

async function copyTextToClipboard(text) {
  if (!text) return false;

  // 1. Try modern Clipboard API
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) {
    console.warn("Modern clipboard failed:", e);
  }

  // 2. Fallback for HTTP / LAN access
  try {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    textarea.style.top = "0";
    textarea.style.opacity = "0";

    document.body.appendChild(textarea);

    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);

    const ok = document.execCommand("copy");

    textarea.remove();

    if (ok) return true;
  } catch (e) {
    console.warn("Fallback clipboard failed:", e);
  }

  return false;
}

const MAX_FILES = 5;
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const MAX_TEXT_BYTES = 512 * 1024;

const TEXT_FILE_RE = /\.(txt|md|markdown|csv|json|jsonl|xml|yaml|yml|toml|ini|log|py|js|ts|jsx|tsx|html|css|sql|sh|bat|ps1|go|rs|java|c|cpp|h|hpp|php|rb|swift)$/i;

function isTextFile(file) {
  return file.type.startsWith("text/") || TEXT_FILE_RE.test(file.name);
}

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ""));
    r.onerror = () => reject(new Error("Failed to read file"));
    r.readAsDataURL(file);
  });
}

async function fileToAttachment(file) {
  if (file.type.startsWith("image/")) {
    if (file.size > MAX_IMAGE_BYTES) {
      throw new Error(`${file.name}: image is too large`);
    }

    return {
      kind: "image",
      name: file.name,
      mime: file.type || "image/*",
      size: file.size,
      data: await readAsDataURL(file),
    };
  }

  if (isTextFile(file)) {
    if (file.size > MAX_TEXT_BYTES) {
      throw new Error(`${file.name}: text file is too large`);
    }

    return {
      kind: "text",
      name: file.name,
      mime: file.type || "text/plain",
      size: file.size,
      text: await file.text(),
    };
  }

  throw new Error(`${file.name}: unsupported file type`);
}

function renderPendingAttachments() {
  const box = $("attachments");
  if (!box) return;

  box.hidden = !state.pendingAttachments.length;
  box.innerHTML = "";

  state.pendingAttachments.forEach((a, i) => {
    const chip = document.createElement("span");
    chip.className = "attach-chip";
    chip.textContent = `${a.kind === "image" ? "📷" : "📄"} ${a.name}`;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.att = String(i);
    btn.textContent = "×";
    btn.title = "Remove";

    chip.appendChild(btn);
    box.appendChild(chip);
  });

  $("btn-send").disabled =
    state.streaming || !($("input").value.trim() || state.pendingAttachments.length);
}

// ---------------------------------------------------------------------------
// Markdown renderer (small, self-contained, XSS-safe: escapes first)
// ---------------------------------------------------------------------------
function inlineFmt(s) {
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, (m, c) => { codes.push(c); return "\u0001" + (codes.length - 1) + "\u0001"; });
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  s = s.replace(/\u0001(\d+)\u0001/g, (m, i) => "<code>" + codes[+i] + "</code>");
  return s;
}

function codeBlockHtml(fence) {
  return '<div class="codeblock"><div class="code-head"><span>' + esc(fence.lang) +
    '</span><button type="button" class="code-copy">Copy</button></div><pre><code>' +
    esc(fence.code.replace(/\n$/, "")) + "</code></pre></div>";
}

function buildList(items, idx) {
  const ordered = items[idx].ordered;
  const indent = items[idx].indent;
  let html = ordered ? "<ol>" : "<ul>";
  let i = idx;
  while (i < items.length) {
    if (items[i].indent < indent) break;
    if (items[i].indent === indent && items[i].ordered !== ordered) break; // marker type changed -> new list
    if (items[i].indent > indent) {
      const res = buildList(items, i);
      html += res[0];
      i = res[1];
      continue;
    }
    html += "<li>" + inlineFmt(items[i].text);
    const j = i + 1;
    if (j < items.length && items[j].indent > indent) {
      const res = buildList(items, j);
      html += res[0];
      i = res[1];
    } else {
      i = j;
    }
    html += "</li>";
  }
  return [html + (ordered ? "</ol>" : "</ul>"), i];
}

function md(src) {
  if (!src) return "";
  const fences = [];
  src = src.replace(/```([\w+#.\-]*)\n?([\s\S]*?)(?:```|$)/g, (m, lang, code) => {
    fences.push({ lang: lang || "text", code });
    return "\u0002" + (fences.length - 1) + "\u0002";
  });

  const lines = esc(src).split("\n");
  let html = "";
  let i = 0;
  let para = [];
  const flushP = () => {
    if (para.length) html += "<p>" + para.map(inlineFmt).join("<br>") + "</p>";
    para = [];
  };

  while (i < lines.length) {
    const line = lines[i];

    const fence = line.match(/^\u0002(\d+)\u0002\s*$/);
    if (fence) { flushP(); html += codeBlockHtml(fences[+fence[1]]); i++; continue; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushP();
      const lvl = h[1].length;
      html += "<h" + lvl + ">" + inlineFmt(h[2]) + "</h" + lvl + ">";
      i++; continue;
    }

    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flushP(); html += "<hr>"; i++; continue; }

    if (/^\s*&gt;\s?/.test(line)) {
      flushP();
      const q = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
        q.push(lines[i].replace(/^\s*&gt;\s?/, ""));
        i++;
      }
      html += "<blockquote>" + q.map(inlineFmt).join("<br>") + "</blockquote>";
      continue;
    }

    // pipe table (header row + separator row)
    if (line.includes("|") && i + 1 < lines.length &&
        /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      flushP();
      const parseRow = (l) => {
        l = l.trim();
        if (l.startsWith("|")) l = l.slice(1);
        if (l.endsWith("|")) l = l.slice(0, -1);
        return l.split("|").map((c) => c.trim());
      };
      const head = parseRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
        rows.push(parseRow(lines[i]));
        i++;
      }
      html += '<div class="tbl-wrap"><table><thead><tr>' +
        head.map((c) => "<th>" + inlineFmt(c) + "</th>").join("") +
        "</tr></thead><tbody>" +
        rows.map((r) => "<tr>" + r.map((c) => "<td>" + inlineFmt(c) + "</td>").join("") + "</tr>").join("") +
        "</tbody></table></div>";
      continue;
    }

    const li = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (li) {
      flushP();
      const items = [];
      while (i < lines.length) {
        const m2 = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (!m2) break;
        items.push({ indent: m2[1].length, ordered: /\d/.test(m2[2][0]), text: m2[3] });
        i++;
        while (i < lines.length && lines[i].trim() !== "" &&
               !/^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i]) &&
               !/^\u0002\d+\u0002\s*$/.test(lines[i])) {
          items[items.length - 1].text += "<br>" + lines[i].trim();
          i++;
        }
      }
      let k = 0;
      while (k < items.length) {
        const built = buildList(items, k);
        html += built[0];
        k = built[1];
      }
      continue;
    }

    if (line.trim() === "") { flushP(); i++; continue; }
    para.push(line);
    i++;
  }
  flushP();
  return html;
}

// ---------------------------------------------------------------------------
// Sidebar / conversations
// ---------------------------------------------------------------------------
async function api(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = "HTTP " + res.status;
    try {
      const j = await res.json();
      detail = j.error || j.detail || detail;
    } catch (e) { /* not json */ }
    throw new Error(detail);
  }
  return res.json();
}

async function refreshConversations() {
  try {
    state.conversations = await api("/api/conversations");
  } catch (e) {
    toast("Failed to load history: " + e.message, "error");
    state.conversations = [];
  }
  renderSidebar();
}

function convSubText(c) {
  const parts = [];
  if (c.prompt_tokens || c.completion_tokens) {
    parts.push("\u2191 " + fmtTok(c.prompt_tokens), "\u2193 " + fmtTok(c.completion_tokens));
  } else if (c.user_est_tokens) {
    parts.push("\u2248 " + fmtTok(c.user_est_tokens) + " tok");
  }
  parts.push(fmtTime(c.updated_at));
  return parts.join(" \u00b7 ");
}

function renderSidebar() {
  const list = $("conv-list");
  list.innerHTML = "";
  if (!state.conversations.length) {
    const d = document.createElement("div");
    d.className = "side-empty";
    d.textContent = "No conversations yet";
    list.appendChild(d);
    return;
  }
  for (const c of state.conversations) {
    const item = document.createElement("div");
    item.className = "conv-item" + (c.id === state.currentId ? " active" : "");
    item.dataset.id = c.id;
    item.innerHTML =
      '<div class="conv-main">' +
        '<div class="conv-title">' + esc(c.title) + "</div>" +
        '<div class="conv-sub">' + esc(convSubText(c)) + "</div>" +
      "</div>" +
      '<div class="conv-actions">' +
        '<button class="icon-btn conv-rename" title="Rename">' + svgIcon("pencil") + "</button>" +
        '<button class="icon-btn conv-del" title="Delete">' + svgIcon("trash") + "</button>" +
      "</div>";
    list.appendChild(item);
  }
}

$("conv-list").addEventListener("click", async (e) => {
  const renameBtn = e.target.closest(".conv-rename");
  const delBtn = e.target.closest(".conv-del");
  const item = e.target.closest(".conv-item");
  if (!item) return;
  const id = item.dataset.id;
  if (renameBtn) {
    const conv = state.conversations.find((c) => c.id === id);
    const title = prompt("Rename conversation:", conv ? conv.title : "");
    if (title && title.trim()) {
      try {
        await api("/api/conversations/" + id, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: title.trim() }),
        });
        await refreshConversations();
        if (id === state.currentId) $("conv-title").textContent = title.trim();
      } catch (err) { toast(err.message, "error"); }
    }
    return;
  }
  if (delBtn) {
    if (!confirm("Delete this conversation?")) return;
    try {
      await api("/api/conversations/" + id, { method: "DELETE" });
      if (id === state.currentId) newChat();
      await refreshConversations();
    } catch (err) { toast(err.message, "error"); }
    return;
  }
  loadConversation(id);
});

async function loadConversation(id) {
  try {
    const data = await api("/api/conversations/" + id);
    state.currentId = id;
    state.messages = data.messages;
    $("conv-title").textContent = data.title;
    renderMessages();
    updateTotals();
    renderSidebar();
    closeMobileSidebar();
  } catch (e) {
    toast(e.message, "error");
  }
}

function newChat() {
  state.currentId = null;
  state.messages = [];
  $("conv-title").textContent = "New chat";
  renderMessages();
  updateTotals();
  renderSidebar();
  closeMobileSidebar();
  $("input").focus();
}

// ---------------------------------------------------------------------------
// Messages rendering
// ---------------------------------------------------------------------------
function copyButtonHtml() {
  return (
    '<button type="button" class="icon-btn msg-copy" ' +
    'title="Copy message" aria-label="Copy message">' +
    svgIcon("copy") +
    "</button>"
  );
}

function regenerateButtonHtml() {
  return (
    '<button type="button" class="icon-btn msg-regen" ' +
    'title="Regenerate" aria-label="Regenerate">' +
    svgIcon("refresh") +
    "</button>"
  );
}

function actionsHtml(m, isLast) {
  // Always show Copy.
  let html = '<span class="meta-actions">' + copyButtonHtml();

  // Show Regenerate only for the last assistant message,
  // and only if that message is not currently streaming.
  if (m.role === "assistant" && isLast && !m.streaming) {
    html += regenerateButtonHtml();
  }

  html += "</span>";
  return html;
}

function assistantMetaHtml(m, isLast) {
  const parts = [];

  if (m.model) {
    parts.push('<span class="meta-model">' + esc(m.model) + "</span>");
  }

  const tokBits = [];
  tokBits.push("\u2191 " + fmtTok(m.prompt_tokens));
  tokBits.push("\u2193 " + fmtTok(m.completion_tokens));

  if (m.reasoning_tokens) {
    tokBits.push("thought " + fmtTok(m.reasoning_tokens));
  }

  let tok = tokBits.join(" \u00b7 ");
  if (m.estimated) {
    tok = "\u2248 " + tok;
  }

  parts.push("<span>" + tok + "</span>");

  if (m.elapsed_ms) {
    parts.push("<span>" + (m.elapsed_ms / 1000).toFixed(1) + "s</span>");
  }

  return parts.join(" ") + actionsHtml(m, isLast);
}

function insertCtxPill(wrapEl, ctx) {
  if (!wrapEl || !ctx || !ctx.dropped) return;
  const pill = document.createElement("div");
  pill.className = "ctx-pill";
  pill.textContent = "Context window: " +
    (ctx.limit ? fmtTok(ctx.limit) + " tok limit - " : "") +
    "sent the last " + ctx.sent + " message(s); " + ctx.dropped +
    " older stayed in the saved history but were not sent to the API";
  wrapEl.insertBefore(pill, wrapEl.firstChild);
}

function messageEl(m, idx) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + m.role + (m.error ? " error" : "");
  wrap.dataset.id = m.id || "";
  wrap.dataset.idx = idx;

  if (m.error) {
    const box = document.createElement("div");
    box.className = "errbox";
    box.textContent = m.errorText || "Error";
    wrap.appendChild(box);
    return wrap;
  }

  if (m.role === "user") {
    const b = document.createElement("div");
    b.className = "bubble";
    b.innerHTML = esc(m.content).replace(/\n/g, "<br>");
    wrap.appendChild(b);
	
	if (m.attachments && m.attachments.length) {
	  const files = document.createElement("div");
	  files.className = "msg-attachments";

	  for (const a of m.attachments) {
		const chip = document.createElement("span");
		chip.className = "attach-chip readonly";
		chip.textContent = `${a.kind === "image" ? "📷" : "📄"} ${a.name}`;
		files.appendChild(chip);
	  }

	  wrap.appendChild(files);
	}

    const meta = document.createElement("div");
    meta.className = "msg-meta user-meta";

    if (m.prompt_tokens) {
      const tok = document.createElement("span");
      tok.textContent = "\u2248 " + fmtTok(m.prompt_tokens) + " tok (est.)";
      meta.appendChild(tok);
    }

    const actions = document.createElement("span");
    actions.className = "meta-actions";
    actions.innerHTML =
      '<button type="button" class="icon-btn msg-copy" title="Copy message" aria-label="Copy message">' +
      svgIcon("copy") +
      "</button>";

    meta.appendChild(actions);
    wrap.appendChild(meta);

    return wrap;
  }

  const card = document.createElement("div");
  card.className = "acard";
  if (m.reasoning || m.streaming) {
    const det = document.createElement("details");
    det.className = "reasoning";
    if (m.streaming && !m.reasoning) det.hidden = true;
    else det.open = !!m.streaming;
    det.innerHTML = "<summary>Reasoning</summary>" +
      '<div class="reasoning-body">' + md(m.reasoning || "") + "</div>";
    card.appendChild(det);
    wrap._reasoning = det;
  }
  const body = document.createElement("div");
  body.className = "md";
  if (m.streaming && !m.content) {
    body.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
  } else {
    body.innerHTML = md(m.content);
  }
  card.appendChild(body);
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  if (!m.streaming) {
    meta.innerHTML = assistantMetaHtml(m, idx === state.messages.length - 1);
  } else {
    meta.innerHTML = "<span>" + esc(state.settings.model || "") + "</span><span>generating...</span>";
  }
  card.appendChild(meta);
  wrap.appendChild(card);
  wrap._body = body;
  wrap._meta = meta;
  return wrap;
}

function renderMessages() {
  const box = $("messages");
  box.querySelectorAll(".msg, .empty-state").forEach((n) => n.remove());
  if (!state.messages.length) {
    box.appendChild($("empty-state-tpl") || buildEmptyState());
  }
  const inner = document.createElement("div");
  inner.className = "msg-inner";
  box.appendChild(inner);
  state.messages.forEach((m, i) => inner.appendChild(messageEl(m, i)));
  box._inner = inner;
  scrollToBottom(true);
}

function buildEmptyState() {
  // clone of the static empty-state markup, rebuilt so it can be re-added
  const div = document.createElement("div");
  div.className = "empty-state";
  div.id = "empty-state";
  div.innerHTML =
    '<div class="empty-icon"><svg viewBox="0 0 24 24" class="ic"><path d="M12 3C6.9 3 3 6.5 3 10.8c0 2.3 1.1 4.3 2.9 5.7-.1 1-.5 2.3-1.6 3.5 0 0 2.4-.2 4.3-1.5 1 .3 2.2.5 3.4.5 5.1 0 9-3.5 9-7.9S17.1 3 12 3z"/></svg></div>' +
    "<h2>Local AI Chat</h2>" +
    "<p>A minimal OpenAI-compatible chat client with history and token usage tracking.</p>";
  return div;
}

function appendMessageEl(m) {
  const inner = $("messages")._inner;
  if (!inner) { renderMessages(); return $("messages")._inner.lastElementChild; }
  const el = messageEl(m, state.messages.length - 1);
  inner.appendChild(el);
  return el;
}

function hideEmptyState() {
  const es = $("empty-state");
  if (es) es.remove();
}

function nearBottom() {
  const box = $("messages");
  return box.scrollHeight - box.scrollTop - box.clientHeight < 120;
}

function scrollToBottom(force) {
  const box = $("messages");
  if (force || nearBottom()) box.scrollTop = box.scrollHeight;
}

function updateTotals() {
  const chip = $("total-chip");
  let p = 0, c = 0, r = 0, est = false, userEst = 0, n = 0;
  for (const m of state.messages) {
    if (m.role === "assistant" && !m.error) {
      n++;
      p += m.prompt_tokens || 0;
      c += m.completion_tokens || 0;
      r += m.reasoning_tokens || 0;
      est = est || !!m.estimated;
    } else if (m.role === "user") {
      userEst += m.prompt_tokens || 0;
    }
  }
  if (!n) { chip.textContent = ""; chip.hidden = true; return; }
  chip.hidden = false;
  let text = "\u2191 " + fmtTok(p) + " \u2193 " + fmtTok(c);
  if (r) text += " \u00b7 thought " + fmtTok(r);
  if (est) text = "\u2248 " + text;
  chip.textContent = text;
  chip.title = "This conversation: " + n + " answer(s)\n" +
    "Prompt tokens (billed, includes resent history): " + fmtTok(p) + "\n" +
    "Completion tokens: " + fmtTok(c) +
    (r ? "\nReasoning tokens: " + fmtTok(r) : "") +
    (userEst ? "\nUser input (est.): " + fmtTok(userEst) : "") +
    (est ? "\n~ = estimated by the client (API did not report usage)" : "");
}

// copy / regenerate delegation
$("messages").addEventListener("click", async (e) => {
  const codeBtn = e.target.closest(".code-copy");
  if (codeBtn) {
    const code = codeBtn.closest(".codeblock").querySelector("code").innerText;

    const ok = await copyTextToClipboard(code);

    if (ok) {
      codeBtn.textContent = "Copied";
    } else {
      toast("Copy failed", "error");
    }

    setTimeout(() => {
      codeBtn.textContent = "Copy";
    }, 1400);

    return;
  }
  const msgCopy = e.target.closest(".msg-copy");
  if (msgCopy) {
    const wrap = msgCopy.closest(".msg");
    const idx = parseInt(wrap ? wrap.dataset.idx : "", 10);

    let msg = null;

    if (Number.isFinite(idx)) {
      msg = state.messages[idx];
    }

    if (!msg && wrap && wrap.dataset.id) {
      msg = state.messages.find((m) => m.id === wrap.dataset.id);
    }

    if (!msg) {
      msg = state.messages.filter((m) => m.role === "assistant").pop();
    }

    const text = msg
      ? msg.error
        ? msg.errorText || ""
        : msg.content || ""
      : "";

    const ok = await copyTextToClipboard(text);

    if (ok) {
      toast("Copied");
    } else {
      toast("Copy failed. Try localhost/HTTPS or allow clipboard access.", "error");
    }

    return;
  }
  const regen = e.target.closest(".msg-regen");
  if (regen) regenerate();
});

// ---------------------------------------------------------------------------
// Chat streaming
// ---------------------------------------------------------------------------
function setStreaming(on) {
  state.streaming = on;
  $("btn-stop").hidden = !on;
  $("btn-send").hidden = on;
  $("btn-send").disabled = on || !($("input").value.trim() || state.pendingAttachments.length);
}

async function streamChat(bodyOverrides) {
  setStreaming(true);
  state.abort = new AbortController();
  let assistantEl = null;
  let assistantMsg = null;
  const contentParts = [];
  const reasoningParts = [];
  let errText = null;
  let renderQueued = false;

  const renderAssistant = () => {
    if (!assistantEl) return;
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => {
      renderQueued = false;
      if (!assistantEl) return;
      const content = contentParts.join("");
      const reasoning = reasoningParts.join("");
      const det = assistantEl._reasoning;
      if (det) {
        det.hidden = !reasoning;
        det.open = true;
        det.querySelector(".reasoning-body").innerHTML = md(reasoning);
      }
      assistantEl._body.innerHTML = content ? md(content) :
        '<div class="typing-dots"><span></span><span></span><span></span></div>';
      scrollToBottom(false);
    });
  };

  const handleEvent = (ev) => {
    if (ev.type === "start") {
      state.currentId = ev.conversation_id;
      if (ev.user_message) {
        const tempIdx = state.messages.findIndex((m) => m.pending);
        if (tempIdx >= 0) {
          state.messages[tempIdx] = ev.user_message;
          const tempEl = $("messages")._inner.children[tempIdx];
          if (tempEl) tempEl.replaceWith(messageEl(ev.user_message, tempIdx));
        }
      }
      assistantMsg = { role: "assistant", content: "", reasoning: "", streaming: true,
                       model: state.settings.model || "" };
      state.messages.push(assistantMsg);
      assistantEl = appendMessageEl(assistantMsg);
      assistantEl._meta.innerHTML = "<span>" + esc(assistantMsg.model) + "</span>" +
        '<span class="typing-dots"><span></span><span></span><span></span></span>';
      insertCtxPill(assistantEl, ev.context);
    } else if (ev.type === "delta") {
      contentParts.push(ev.text);
      renderAssistant();
    } else if (ev.type === "reasoning") {
      reasoningParts.push(ev.text);
      renderAssistant();
    } else if (ev.type === "error") {
      errText = ev.error;
	} else if (ev.type === "warning") {
	  toast(ev.warning || "Warning", "warn");
    } else if (ev.type === "done") {
      if (ev.message) {
        Object.assign(assistantMsg, ev.message, { streaming: false });
        if (assistantEl) {
          if (assistantEl._reasoning) {
            assistantEl._reasoning.hidden = !assistantMsg.reasoning;
            assistantEl._reasoning.querySelector(".reasoning-body").innerHTML = md(assistantMsg.reasoning);
          }
          assistantEl._body.innerHTML = md(assistantMsg.content);
          assistantEl._meta.innerHTML = assistantMetaHtml(assistantMsg, true);
        }
      } else if (errText && assistantEl) {
        assistantEl.remove();
        state.messages.pop();
        state.messages.push({ role: "assistant", error: true, errorText: errText });
        appendMessageEl(state.messages[state.messages.length - 1]);
      }
      if (ev.usage && ev.usage.estimated !== undefined) { /* totals recompute below */ }
    }
  };

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(bodyOverrides),
      signal: state.abort.signal,
    });
    if (!res.ok) {
      let detail = "HTTP " + res.status;
      try { const j = await res.json(); detail = j.error || j.detail || detail; } catch (e) { /* */ }
      throw new Error(detail);
    }
    if (!(res.headers.get("content-type") || "").includes("text/event-stream")) {
      // Non-streaming mode: the server replied with a single JSON object.
      const data = await res.json();
      state.currentId = data.conversation_id || state.currentId;
      if (data.user_message) {
        const tempIdx = state.messages.findIndex((m) => m.pending);
        if (tempIdx >= 0) state.messages[tempIdx] = data.user_message;
      }
      let assistant = null;
      if (data.message) {
        assistant = Object.assign({}, data.message, { streaming: false });
        state.messages.push(assistant);
      }
      state.messages = state.messages.filter((m) => !m.pending);
      renderMessages();
      if (assistant) {
        const inner = $("messages")._inner;
        insertCtxPill(inner && inner.lastElementChild, data.context);
      }
      return;
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        if (!raw.startsWith("data:")) continue;
        let ev;
        try { ev = JSON.parse(raw.slice(5).trim()); } catch (e) { continue; }
        handleEvent(ev);
      }
    }
    if (errText && !assistantMsg) {
      state.messages.push({ role: "assistant", error: true, errorText: errText });
      appendMessageEl(state.messages[state.messages.length - 1]);
      toast(errText, "error");
    } else if (errText) {
      toast(errText, "error");
    }
  } catch (e) {
    if (e.name === "AbortError") {
      if (assistantMsg) {
        assistantMsg.streaming = false;
        if (assistantEl && assistantEl._meta) {
          assistantEl._meta.innerHTML = assistantMetaHtml(assistantMsg, true);
        }
        toast("Stopped - partial answer kept");
        if (state.currentId) {
          try {
            const data = await api("/api/conversations/" + state.currentId);
            state.messages = data.messages;
            renderMessages();
          } catch (err) { /* keep local state */ }
        }
      } else {
        state.messages = state.messages.filter((m) => !m.pending);
        renderMessages();
      }
    } else {
      if (assistantEl) assistantEl.remove();
      if (assistantMsg) state.messages.pop();
      state.messages.push({ role: "assistant", error: true, errorText: e.message });
      appendMessageEl(state.messages[state.messages.length - 1]);
      toast(e.message, "error");
    }
  } finally {
    if (assistantMsg) assistantMsg.streaming = false;
    setStreaming(false);
    state.abort = null;
    $("input").focus();
    await refreshConversations();
    updateTotals();
  }
}

async function sendMessage() {
  if (state.streaming) return;

  const input = $("input");
  const text = input.value.trim();
  const attachments = state.pendingAttachments.slice();

  if (!text && !attachments.length) return;

  input.value = "";
  state.pendingAttachments = [];
  renderPendingAttachments();

  autoGrow(input);
  hideEmptyState();

  const temp = {
    role: "user",
    content: text,
    attachments,
    pending: true,
  };

  state.messages.push(temp);

  if ($("messages")._inner) appendMessageEl(temp);
  else renderMessages();

  scrollToBottom(true);

  await streamChat({
    conversation_id: state.currentId,
    content: text,
    attachments,
  });
}

async function regenerate() {
  if (state.streaming || !state.currentId) return;
  while (state.messages.length && state.messages[state.messages.length - 1].role === "assistant") {
    state.messages.pop();
  }
  renderMessages();
  await streamChat({ conversation_id: state.currentId, regenerate: true });
}

// ---------------------------------------------------------------------------
// Composer
// ---------------------------------------------------------------------------
function autoGrow(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 220) + "px";
}

$("composer").addEventListener("submit", (e) => { e.preventDefault(); sendMessage(); });
$("btn-stop").addEventListener("click", () => { if (state.abort) state.abort.abort(); });
$("input").addEventListener("input", (e) => {
  autoGrow(e.target);
  $("btn-send").disabled = state.streaming || !(e.target.value.trim() || state.pendingAttachments.length);
});
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (!state.streaming) sendMessage();
  }
});

$("btn-attach").addEventListener("click", () => {
  $("file-input").click();
});

$("file-input").addEventListener("change", async (e) => {
  const files = Array.from(e.target.files || []);

  for (const file of files) {
    if (state.pendingAttachments.length >= MAX_FILES) {
      toast(`Max ${MAX_FILES} files`, "warn");
      break;
    }

    try {
      state.pendingAttachments.push(await fileToAttachment(file));
    } catch (err) {
      toast(err.message, "error");
    }
  }

  e.target.value = "";
  renderPendingAttachments();
});

$("attachments").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-att]");
  if (!btn) return;

  const idx = parseInt(btn.dataset.att, 10);
  if (Number.isFinite(idx)) {
    state.pendingAttachments.splice(idx, 1);
    renderPendingAttachments();
  }
});

// ---------------------------------------------------------------------------
// Settings modal
// ---------------------------------------------------------------------------
function openModal(id) { $(id).hidden = false; }
function closeModal(id) { $(id).hidden = true; }
document.querySelectorAll(".modal-overlay").forEach((ov) => {
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.hidden = true; });
  ov.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => { ov.hidden = true; }));
});

function fillSettingsForm() {
  const s = state.settings;
  $("s-base").value = s.base_url || "";
  $("s-key").value = s.api_key || "";
  $("s-model").value = s.model || "";
  $("s-sys").value = s.system_prompt || "";
  $("s-temp").value = s.temperature ?? 0.7;
  $("s-hist").value = s.history_limit ?? 50;
  $("s-ctx").value = s.max_context_tokens ?? 0;
  $("s-max-out").value = s.max_tokens ?? 0;
  $("s-extra").value = s.extra_params || "";
  $("s-stream").checked = !!s.stream;
  $("test-result").textContent = "";
  $("test-result").className = "test-result";
}

function collectSettings() {
  return {
    base_url: $("s-base").value.trim(),
    api_key: $("s-key").value.trim(),
    model: $("s-model").value.trim(),
    system_prompt: $("s-sys").value,
    temperature: parseFloat($("s-temp").value) || 0,
    history_limit: parseInt($("s-hist").value, 10) || 50,
    max_context_tokens: parseInt($("s-ctx").value, 10) || 0,
    max_tokens: parseInt($("s-max-out").value, 10) || 0,
    extra_params: $("s-extra").value.trim(),
    stream: $("s-stream").checked,
  };
}

function applySettings() {
  $("model-chip").textContent = state.settings.model || "no model";
  const warn = $("empty-warn");
  if (warn) warn.hidden = !!state.settings.api_key;
}

$("btn-settings").addEventListener("click", () => { fillSettingsForm(); openModal("settings-modal"); });

$("btn-save-settings").addEventListener("click", async () => {
  try {
    state.settings = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectSettings()),
    });
    applySettings();
    closeModal("settings-modal");
    toast("Settings saved");
  } catch (e) { toast(e.message, "error"); }
});

$("btn-test").addEventListener("click", async () => {
  const out = $("test-result");
  out.className = "test-result";
  out.textContent = "Testing...";
  try {
    const r = await api("/api/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectSettings()),
    });
    if (r.ok) {
      out.className = "test-result ok";
      out.textContent = "OK - " + r.models.length + " models - " + r.latency_ms + " ms";
      const dl = $("model-list");
      dl.innerHTML = "";
      for (const m of r.models) {
        const opt = document.createElement("option");
        opt.value = m;
        dl.appendChild(opt);
      }
    } else {
      out.className = "test-result fail";
      out.textContent = r.error || "Failed";
    }
  } catch (e) {
    out.className = "test-result fail";
    out.textContent = e.message;
  }
});

// ---------------------------------------------------------------------------
// Usage modal
// ---------------------------------------------------------------------------
$("btn-usage").addEventListener("click", async () => {
  openModal("usage-modal");
  const body = $("usage-body");
  let u;
  try { u = await api("/api/usage"); }
  catch (e) { body.innerHTML = '<p class="muted">' + esc(e.message) + "</p>"; return; }

  const o = u.overall || {};
  let html = '<p class="usage-total">All time: <b>' + fmtTok(o.prompt_tokens) + "</b> prompt &uarr; / <b>" +
    fmtTok(o.completion_tokens) + "</b> completion &darr; tokens across <b>" + fmtTok(o.calls) +
    "</b> answer(s)" + (o.reasoning_tokens ? ' (incl. <b>' + fmtTok(o.reasoning_tokens) + "</b> reasoning)" : "") +
    ".</p>";

  if (u.by_model && u.by_model.length) {
    html += '<h3>By model</h3><table class="usage-table"><thead><tr><th>Model</th><th>Calls</th><th>&uarr; Prompt</th><th>&darr; Completion</th><th>Thought</th></tr></thead><tbody>';
    for (const m of u.by_model) {
      html += "<tr><td>" + esc(m.model) + "</td><td>" + fmtTok(m.calls) + "</td><td>" +
        fmtTok(m.prompt_tokens) + "</td><td>" + fmtTok(m.completion_tokens) + "</td><td>" +
        fmtTok(m.reasoning_tokens) + "</td></tr>";
    }
    html += "</tbody></table>";
  }

  if (u.by_day && u.by_day.length) {
    const maxVal = Math.max(...u.by_day.map((d) => (d.prompt_tokens || 0) + (d.completion_tokens || 0)), 1);
    html += "<h3>Last 14 days</h3>";
    for (const d of [...u.by_day].reverse()) {
      const total = (d.prompt_tokens || 0) + (d.completion_tokens || 0);
      const w = Math.max(2, Math.round((total / maxVal) * 260));
      html += '<div class="day-bar"><span class="day">' + esc(d.day) + '</span>' +
        '<span class="bar" style="width:' + w + 'px"></span>' +
        '<span class="val">' + fmtTok(total) + " tok / " + fmtTok(d.calls) + " calls</span></div>";
    }
  }
  if (!u.by_model || !u.by_model.length) {
    html += '<p class="muted">No usage recorded yet.</p>';
  }
  body.innerHTML = html;
});

// ---------------------------------------------------------------------------
// Theme / sidebar mobile / init
// ---------------------------------------------------------------------------
function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
}
$("btn-theme").addEventListener("click", () => {
  state.theme = state.theme === "light" ? "dark" : "light";
  localStorage.setItem("theme", state.theme);
  applyTheme();
});

function closeMobileSidebar() {
  $("sidebar").classList.remove("open");
  $("scrim").classList.remove("show");
}
$("btn-menu").addEventListener("click", () => {
  $("sidebar").classList.toggle("open");
  $("scrim").classList.toggle("show");
});
$("scrim").addEventListener("click", closeMobileSidebar);
$("btn-new").addEventListener("click", newChat);

// double-click header title to rename current conversation
$("conv-title").addEventListener("dblclick", () => {
  if (!state.currentId) return;
  const conv = state.conversations.find((c) => c.id === state.currentId);
  const title = prompt("Rename conversation:", conv ? conv.title : $("conv-title").textContent);
  if (title && title.trim()) {
    api("/api/conversations/" + state.currentId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title.trim() }),
    }).then(async () => {
      $("conv-title").textContent = title.trim();
      await refreshConversations();
    }).catch((e) => toast(e.message, "error"));
  }
});

async function init() {
  applyTheme();
  autoGrow($("input"));
  renderMessages();
  try {
    const [s] = await Promise.all([
      api("/api/settings"),
      refreshConversations(),
    ]);
    state.settings = s;
  } catch (e) {
    toast("Failed to load settings: " + e.message, "error");
  }
  applySettings();
  updateTotals();
  $("input").focus();
}

init();

