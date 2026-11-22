"use strict";

/* ------------------------------------------------------------------ *
 * Helpers
 * ------------------------------------------------------------------ */
const state = { session: null, bot: null, section: "overview", arg: null, guild: null };
const content = () => document.getElementById("content");
const fill = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else if (k === "disabled") el.disabled = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function http(method, url, body) {
  const opts = { method, headers: {} };
  if (state.session) opts.headers["X-CSRF"] = state.session.csrf;
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  if (r.status === 401) { location.href = "/"; throw new Error("Signed out."); }
  let data = null;
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok) throw new Error((data && data.error) || `Something went wrong (${r.status}).`);
  return data;
}
const api = (method, path, body) => http(method, `/api/bot/${encodeURIComponent(state.bot.id)}/${path}`, body);

function toast(message, isError) {
  const t = h("div", { class: "toast" + (isError ? " error" : ""), text: message });
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), isError ? 7000 : 3500);
}

async function busy(btn, fn) {
  const label = btn.textContent;
  btn.disabled = true;
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.textContent = label; }
}

const humanize = (k) => (k.charAt(0).toUpperCase() + k.slice(1)).replace(/_/g, " ").replace(/\bid\b/gi, "ID").replace(/\burl\b/gi, "URL").replace(/\bdm\b/gi, "DM");
const plain = (s) => String(s || "").replace(/\{prefix\}/g, "?").replace(/`/g, "");
function when(s) {
  if (!s) return "";
  const d = new Date(String(s).replace(" ", "T"));
  if (isNaN(d)) return String(s);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
function ago(s) {
  const d = new Date(String(s).replace(" ", "T"));
  if (isNaN(d)) return "";
  const m = Math.round((Date.now() - d) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  if (m < 1440) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} days ago`;
}
function duration(s) {
  const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${hh}h` : hh ? `${hh}h ${m}m` : `${m}m`;
}
const LEVEL_NAMES = { OWNER: "an owner", ADMINISTRATOR: "an administrator", MODERATOR: "a moderator", SUPPORTER: "a supporter" };

function pageHead(title, intro, ...extra) {
  return h("div", { class: "page-head" }, h("div", {}, h("h1", { text: title }), intro ? h("p", { text: intro }) : null), ...extra);
}
function field(label, input, hint) {
  return h("div", { class: "field" }, h("label", { text: label }), input, hint ? h("div", { class: "hint", text: hint }) : null);
}
function switchEl(checked, onchange, label) {
  return h("span", { class: "switch" }, h("input", { type: "checkbox", checked, onchange, "aria-label": label }), h("span"));
}
function confirmDo(question) { return window.confirm(question); }

async function guild() {
  if (!state.guild) state.guild = await api("GET", "guild");
  return state.guild;
}
const mainGuild = (g) => g.main || g.staff;
function roleSelect(roles, value, noneLabel) {
  const sel = h("select", {}, noneLabel ? h("option", { value: "", text: noneLabel }) : null);
  for (const r of roles) sel.append(h("option", { value: r.id, text: "@" + r.name }));
  sel.value = value ? String(value) : "";
  return sel;
}
function channelSelect(channels, value, noneLabel) {
  const sel = h("select", {}, noneLabel ? h("option", { value: "", text: noneLabel }) : null);
  for (const c of channels) sel.append(h("option", { value: c.id, text: "#" + c.name + (c.category ? ` (${c.category})` : "") }));
  sel.value = value ? String(value) : "";
  return sel;
}

/* A search box for picking a member. */
function memberPicker(onPick, buttonLabel) {
  const input = h("input", { type: "search", placeholder: "Search by name, or paste a user ID" });
  const results = h("ul", { class: "list" });
  const find = async () => {
    const q = input.value.trim();
    fill(results, );
    if (/^\d{15,20}$/.test(q)) {
      results.append(h("li", {}, h("div", { class: "main" }, h("div", { class: "title", text: `User ID ${q}` })),
        h("div", { class: "actions" }, h("button", { class: "btn", text: buttonLabel, onclick: () => onPick(q, `User ${q}`) }))));
    }
    const found = await api("GET", "members?q=" + encodeURIComponent(q)).catch((e) => { toast(e.message, true); return []; });
    for (const m of found) {
      results.append(h("li", {},
        h("img", { class: "av-sm", src: m.avatar, alt: "", width: 28, height: 28 }),
        h("div", { class: "main" }, h("div", { class: "title", text: m.display }), h("div", { class: "sub", text: m.name })),
        h("div", { class: "actions" }, h("button", { class: "btn", text: buttonLabel, onclick: () => onPick(m.id, m.display) }))));
    }
    if (!results.children.length) results.append(h("li", { class: "empty", text: "Nobody matches that." }));
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); find(); } });
  return h("div", {}, h("div", { class: "row" }, h("div", { class: "grow" }, input), h("button", { class: "btn", text: "Find", onclick: find })), results);
}

/* ------------------------------------------------------------------ *
 * Navigation
 * ------------------------------------------------------------------ */
const SECTIONS = [
  { id: "overview", label: "Overview", cap: "_staff" },
  { id: "shared", label: "Shared with you", cap: "_shared" },
  { id: "tickets", label: "Open tickets", cap: "logs" },
  { id: "logs", label: "Logs", cap: "logs" },
  { group: "Tickets" },
  { id: "threadmenu", label: "Thread menu", cap: "threadmenu" },
  { id: "snippets", label: "Snippets", cap: "snippets" },
  { id: "aliases", label: "Aliases", cap: "aliases" },
  { id: "autotriggers", label: "Autotriggers", cap: "autotriggers" },
  { id: "msgcmd", label: "Message commands", cap: "msgcmd" },
  { id: "verification", label: "Verification", cap: "verification" },
  { group: "Staff and safety" },
  { id: "permissions", label: "Staff access", cap: "permissions" },
  { id: "blocks", label: "Blocked users", cap: "blocks" },
  { group: "Bot" },
  { id: "settings", label: "Settings", cap: "settings" },
  { id: "plugins", label: "Plugins", cap: "plugins" },
];
const can = (cap) => {
  const b = state.bot;
  if (!cap) return true;
  if (!b || !b.can) return false;
  if (cap === "_staff") return Object.values(b.can).some(Boolean);
  if (cap === "_shared") return (b.shared || 0) > 0;
  return !!b.can[cap];
};

function go(section, arg) {
  location.hash = `#/${encodeURIComponent(state.bot.id)}/${section}${arg ? "/" + encodeURIComponent(arg) : ""}`;
}

async function loadSession() {
  state.session = await http("GET", "/api/session");
}

const initials = (name) => String(name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");

function renderRail() {
  const rail = document.getElementById("rail");
  fill(rail, );
  for (const b of state.session.bots) {
    const lampClass = !b.online ? "lamp" : ["OWNER", "ADMINISTRATOR"].includes(b.level) ? "lamp on" : "lamp amber";  // amber: limited access
    const avatar = (b.bot && (b.bot.icon || b.bot.avatar)) || "https://cdn.discordapp.com/embed/avatars/0.png";
    rail.append(h("button", {
      class: "line", title: `${b.name}${b.online ? "" : " (offline)"}`, "aria-label": b.name,
      "aria-current": state.bot && state.bot.id === b.id ? "true" : "false",
      onclick: () => { location.hash = `#/${encodeURIComponent(b.id)}/overview`; },
    }, h("span", { class: "initials", "aria-hidden": "true", text: initials(b.name) }),
       h("img", { src: avatar, alt: "", onerror: (e) => e.target.remove() }), h("span", { class: lampClass })));
  }
  const u = state.session.user;
  rail.append(h("div", { class: "me" }, h("img", { src: u.avatar, alt: "", title: u.name, onerror: (e) => { e.target.style.visibility = "hidden"; } }),
    h("button", { text: "Sign out", onclick: async () => {
      try { await http("POST", "/logout"); } catch (e) { /* signed out either way */ }
      state.session = null;
      location.replace("/");
    } })));
}

function renderSections() {
  const nav = document.getElementById("sections");
  fill(nav, );
  if (!state.bot) return;
  nav.append(h("div", { class: "bot-name", text: state.bot.name },
    h("small", { text: !state.bot.online ? "Offline" : can("_staff") ? `You're ${LEVEL_NAMES[state.bot.level] || "on staff"} here` : "Logs shared with you" })));
  let pendingGroup = null;
  for (const s of SECTIONS) {
    if (s.group) { pendingGroup = s.group; continue; }
    if (!can(s.cap)) continue;
    if (pendingGroup) { nav.append(h("div", { class: "group", text: pendingGroup })); pendingGroup = null; }
    nav.append(h("a", { href: `#/${encodeURIComponent(state.bot.id)}/${s.id}`, text: s.label, "aria-current": state.section === s.id ? "page" : null }));
  }
}

async function route() {
  if (!state.session) await loadSession();
  const [, bid, section, arg] = location.hash.split("/").map((x) => x && decodeURIComponent(x));
  const bots = state.session.bots;
  const el = content();
  if (!bots.length) {
    renderRail();
    fill(document.getElementById("sections"), );
    fill(el, pageHead("No bots yet", null),
      h("p", { text: "You're not on staff for any of the bots in this dashboard. Ask a bot owner to give you a staff level, then sign in again." }));
    return;
  }
  const bot = bots.find((b) => b.id === bid) || bots[0];
  if (!state.bot || state.bot.id !== bot.id) state.guild = null;
  state.bot = bot;
  const home = can("_staff") ? "overview" : "shared";
  let sec = section && SECTIONS.find((s) => s.id && s.id === section && can(s.cap)) ? section : home;
  // A log link opened by someone it was shared with (not staff): show it in "Shared with you".
  if (section === "logs" && arg && !can("logs") && can("_shared")) sec = "shared";
  state.section = sec;
  state.arg = arg || null;
  renderRail();
  renderSections();
  fill(el, h("p", { class: "muted", text: "Loading…" }));
  if (!bot.online) {
    fill(el, pageHead(bot.name, null), h("div", { class: "alert", text: "This bot isn't answering. Check it's running (pm2 list on the server) and that its dashboard settings match." }));
    return;
  }
  try {
    await VIEWS[sec](el);
  } catch (e) {
    fill(el, pageHead("Couldn't load this", null), h("div", { class: "alert", text: e.message }));
  }
  el.focus({ preventScroll: true });
}

async function refreshCaps() {
  const id = state.bot && state.bot.id;
  await loadSession();
  state.bot = state.session.bots.find((b) => b.id === id) || state.session.bots[0];
  renderSections();
}

/* ------------------------------------------------------------------ *
 * Views
 * ------------------------------------------------------------------ */
const VIEWS = {};

VIEWS.overview = async (el) => {
  const o = await api("GET", "overview");
  const dm = {
    NONE: ["lamp on", "Anyone can open a ticket by messaging the bot."],
    NEW_THREADS: ["lamp amber", "New tickets are paused. People with an open ticket can still reply."],
    ALL_THREADS: ["lamp", "Tickets are paused. Nobody can message the bot."],
  }[o.dm_disabled] || ["lamp", o.dm_disabled];
  const lines = h("ul", { class: "status-lines" },
    h("li", {}, h("span", { class: dm[0] }), dm[1]),
    h("li", {}, h("span", { class: "lamp on" }), o.open_threads === 1 ? "1 ticket is open right now." : `${o.open_threads} tickets are open right now.`,
      can("logs") ? h("a", { href: `#/${state.bot.id}/tickets`, text: "See them" }) : null),
    h("li", {}, h("span", { class: "lamp on" }), `Members message the bot from ${o.main_guild || "your server"}` + (o.staff_guild ? `; tickets open in ${o.staff_guild}.` : ".")),
    h("li", {}, h("span", { class: "lamp on" }), `Answering in ${o.latency_ms ?? "?"} ms and running for ${duration(o.uptime_s)} on version ${o.version}, with ${o.plugins} plugin${o.plugins === 1 ? "" : "s"}.`),
  );
  fill(el, pageHead(o.name, `${o.bot_name}. You're ${LEVEL_NAMES[o.level] || "on staff"} here.`), lines);

  if (can("dm")) {
    const seg = h("div", { class: "seg", role: "group", "aria-label": "Who can open tickets" });
    const modes = [["enable", "Everyone", "NONE"], ["disable_new", "Only existing tickets", "NEW_THREADS"], ["disable_all", "Nobody", "ALL_THREADS"]];
    for (const [mode, label, value] of modes) {
      seg.append(h("button", {
        text: label, "aria-pressed": o.dm_disabled === value ? "true" : "false",
        onclick: async (e) => busy(e.target, async () => { await api("POST", "dm", { mode }); toast("Updated who can open tickets."); route(); }),
      }));
    }
    el.append(h("h2", { text: "Who can open tickets" }), seg);
  }
};

VIEWS.tickets = async (el) => {
  const rows = await api("GET", "threads");
  const list = h("ul", { class: "list" });
  for (const t of rows) {
    list.append(h("li", {},
      h("div", { class: "main" }, h("div", { class: "title", text: t.user.name }),
        h("div", { class: "sub", text: `Opened ${ago(t.created_at)}${t.snoozed ? ", snoozed" : ""} in #${t.channel.name}` })),
      t.channel.url ? h("div", { class: "actions" }, h("a", { class: "btn", href: t.channel.url, target: "_blank", rel: "noopener", text: "Open in Discord" })) : null));
  }
  if (!rows.length) list.append(h("li", { class: "empty", text: "No open tickets. Nice and quiet." }));
  fill(el, pageHead("Open tickets", "Tickets that are open right now. Reply to them in Discord."), list);
};

VIEWS.logs = async (el) => {
  if (state.arg) return renderLog(el, state.arg, "logs");
  const params = new URLSearchParams(location.search);
  let page = 0, q = "";
  const input = h("input", { type: "search", placeholder: "Search by name, user ID, log key or message text" });
  const body = h("div", {});
  const load = async () => {
    fill(body, h("p", { class: "muted", text: "Loading…" }));
    const data = await api("GET", `logs?page=${page}&q=${encodeURIComponent(q)}`);
    const table = h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "Member" }), h("th", { text: "Opened" }), h("th", { class: "hide-sm", text: "Closed by" }), h("th", { class: "hide-sm", text: "Messages" }))));
    const tb = h("tbody");
    for (const l of data.logs) {
      tb.append(h("tr", { class: "click", tabindex: "0", onclick: () => go("logs", l.key), onkeydown: (e) => { if (e.key === "Enter") go("logs", l.key); } },
        h("td", {}, h("div", { text: l.recipient.name }), h("div", { class: "small muted mono", text: l.key })),
        h("td", { text: when(l.created_at) }),
        h("td", { class: "hide-sm", text: l.open ? "Still open" : (l.closer && l.closer.name) || "" }),
        h("td", { class: "hide-sm", text: l.messages })));
    }
    table.append(tb);
    const pages = Math.max(1, Math.ceil(data.total / data.per_page));
    fill(body, 
      data.logs.length ? table : h("p", { class: "empty", text: q ? "No logs match that search." : "No logs yet." }),
      h("div", { class: "pager" },
        h("button", { class: "btn", text: "Newer", disabled: page === 0, onclick: () => { page--; load(); } }),
        h("span", { class: "muted", text: `Page ${page + 1} of ${pages} (${data.total} logs)` }),
        h("button", { class: "btn", text: "Older", disabled: page + 1 >= pages, onclick: () => { page++; load(); } })));
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { q = input.value.trim(); page = 0; load(); } });
  fill(el, pageHead("Logs", "Every closed ticket. Search, then pick one to read it."),
    h("div", { class: "row" }, h("div", { class: "grow" }, input), h("button", { class: "btn primary", text: "Search", onclick: () => { q = input.value.trim(); page = 0; load(); } })), body);
  await load();
};

async function renderLog(el, key, back) {
  const l = await api("GET", "logs/" + encodeURIComponent(key));
  const tr = h("div", { class: "transcript" });
  for (const m of l.messages) {
    const isNote = ["note", "internal", "persistent_note"].includes(m.type);
    const tag = isNote ? "staff note, not sent" : m.type === "anonymous" ? "anonymous reply" : m.type === "system" ? "system" : m.author.mod ? "staff" : "";
    const atts = m.attachments.map((a) => a.is_image && /^https:\/\//.test(a.url)
      ? h("a", { href: a.url, target: "_blank", rel: "noopener" }, h("img", { src: a.url, alt: a.filename || "attachment" }))
      : h("div", {}, h("a", { href: a.url, target: "_blank", rel: "noopener", text: a.filename || "attachment" })));
    tr.append(h("div", { class: "msg" + (isNote ? " note" : m.author.mod ? " mod" : "") },
      h("img", { class: "av", src: m.author.avatar || "https://cdn.discordapp.com/embed/avatars/0.png", alt: "", onerror: (e) => { e.target.style.visibility = "hidden"; } }),
      h("div", {}, h("div", { class: "who", text: m.author.name }, tag ? h("span", { class: "tag", text: tag }) : null, h("span", { class: "tag", text: when(m.timestamp) + (m.edited ? " (edited)" : "") })),
        h("div", { class: "body", text: m.content }), atts.length ? h("div", { class: "att" }, atts) : null)));
  }
  if (!l.messages.length) tr.append(h("p", { class: "empty", text: "This log has no messages." }));
  fill(el,
    h("p", {}, h("a", { href: `#/${state.bot.id}/${back}`, text: back === "shared" ? "Back to shared logs" : "Back to logs" })),
    pageHead(l.recipient.name, `Opened ${when(l.created_at)}` + (l.open ? ", still open" : `, closed ${when(l.closed_at)}${l.closer ? " by " + l.closer.name : ""}`)),
    l.shared_view ? h("p", { class: "muted small", text: "This conversation was shared with you. Staff-only notes aren't shown." }) : null,
    can("logs") ? await sharePanel(l) : null,
    l.close_message ? h("div", { class: "note", text: "Closing message: " + l.close_message }) : null, tr);
}

/* Give someone temporary access to one log. */
async function sharePanel(l) {
  const grants = await api("GET", `logs/${encodeURIComponent(l.key)}/grants`);
  const link = `${location.origin}/logs/${encodeURIComponent(state.bot.id)}/${encodeURIComponent(l.key)}`;
  const hours = h("select", { "aria-label": "For how long" },
    h("option", { value: "1", text: "for 1 hour" }), h("option", { value: "24", text: "for 1 day" }),
    h("option", { value: "168", text: "for 7 days" }), h("option", { value: "720", text: "for 30 days" }));
  hours.value = "24";
  const share = async (id, name) => {
    try {
      await api("POST", `logs/${encodeURIComponent(l.key)}/grants`, { user_id: id, hours: Number(hours.value) });
      toast(`${name} can see this log ${hours.selectedOptions[0].text}. Send them the link.`);
      route();
    } catch (e) { toast(e.message, true); }
  };
  const list = h("ul", { class: "list" });
  for (const g of grants) {
    list.append(h("li", {}, h("div", { class: "main" }, h("div", { class: "title", text: g.name }),
      h("div", { class: "sub", text: `Until ${when(g.expires)}${g.granted_by ? ", shared by " + g.granted_by : ""}` })),
      h("div", { class: "actions" }, h("button", { class: "btn quiet danger", text: "Remove access", onclick: async (e) => busy(e.target, async () => {
        await api("DELETE", `logs/${encodeURIComponent(l.key)}/grants/${g.id}`); toast(`Removed ${g.name}'s access.`); route();
      }) }))));
  }
  const copy = h("button", { class: "btn", text: "Copy link", onclick: async () => {
    try { await navigator.clipboard.writeText(link); toast("Link copied."); } catch (e) { toast(link); }
  } });
  const panel = h("details", { class: "panel share" + (grants.length ? " has" : "") },
    h("summary", { text: grants.length ? `Shared with ${grants.length} ${grants.length === 1 ? "person" : "people"}` : "Share this log" }),
    h("p", { class: "muted small", text: "Let someone outside your staff read this conversation for a while, like the member who opened it or someone reviewing an appeal. They sign in with Discord and only see logs shared with them. Staff-only notes are hidden from them." }),
    h("div", { class: "row" }, h("span", { class: "grow mono small", text: link }), copy),
    h("div", { class: "row mt" }, hours,
      l.recipient.id ? h("button", { class: "btn primary", text: `Share with ${l.recipient.name}`, onclick: () => share(l.recipient.id, l.recipient.name) }) : null),
    h("h3", { class: "mt", text: "Or share with someone else" }),
    memberPicker((id, name) => share(id, name), "Share"),
    grants.length ? h("h3", { class: "mt", text: "Who can see it" }) : null, grants.length ? list : null);
  if (grants.length) panel.open = true;
  return panel;
}

VIEWS.shared = async (el) => {
  if (state.arg) return renderLog(el, state.arg, "shared");
  const rows = await api("GET", "shared");
  const list = h("ul", { class: "list" });
  for (const l of rows) {
    list.append(h("li", { class: "click", tabindex: "0", onclick: () => go("shared", l.key), onkeydown: (e) => { if (e.key === "Enter") go("shared", l.key); } },
      h("div", { class: "main" }, h("div", { class: "title", text: l.recipient.name }),
        h("div", { class: "sub", text: `Opened ${when(l.created_at)}. You can see it until ${when(l.expires)}.` })),
      h("div", { class: "actions" }, h("button", { class: "btn", text: "Read", onclick: (e) => { e.stopPropagation(); go("shared", l.key); } }))));
  }
  if (!rows.length) list.append(h("li", { class: "empty", text: "Nothing is shared with you right now. Shared access expires after a while." }));
  fill(el, pageHead("Shared with you", `Conversations from ${state.bot.name} that staff have let you read.`), list);
};

/* ---- snippets / aliases / autotriggers -------------------------------- */
const KV = {
  snippets: { title: "Snippets", intro: "Saved replies. Staff send one by typing ?name in a ticket.", name: "Name", value: "Reply text", valuePh: "Hi! Thanks for reaching out…", namePh: "e.g. hello", area: true },
  aliases: { title: "Aliases", intro: "Shortcuts for commands. An alias called thanks that runs reply Thanks! lets staff type ?thanks.", name: "Alias", value: "Runs", valuePh: "reply Thanks for contacting us!", namePh: "e.g. thanks" },
  autotriggers: { title: "Autotriggers", intro: "When the first message of a new ticket contains the keyword, the bot runs the command, alias or snippet.", name: "Keyword", value: "Runs", valuePh: "e.g. verifyprompt or reply …", namePh: "e.g. refund" },
};
for (const kind of Object.keys(KV)) {
  VIEWS[kind] = async (el) => {
    const cfg = KV[kind];
    const items = await api("GET", kind);
    const nameIn = h("input", { placeholder: cfg.namePh });
    const valueIn = cfg.area ? h("textarea", { placeholder: cfg.valuePh }) : h("input", { placeholder: cfg.valuePh });
    const save = h("button", { class: "btn primary", text: "Save" });
    save.onclick = () => busy(save, async () => {
      const name = nameIn.value.trim();
      if (!name) throw new Error(`Enter a ${cfg.name.toLowerCase()}.`);
      await api("PUT", `${kind}/${encodeURIComponent(name)}`, { value: valueIn.value });
      toast(`Saved ${name}.`);
      route();
    });
    const list = h("ul", { class: "list" });
    for (const [name, value] of Object.entries(items)) {
      list.append(h("li", {},
        h("div", { class: "main" }, h("div", { class: "title mono", text: name }), h("div", { class: "sub", text: value })),
        h("div", { class: "actions" },
          h("button", { class: "btn quiet", text: "Edit", onclick: () => { nameIn.value = name; valueIn.value = value; nameIn.focus(); window.scrollTo({ top: 0 }); } }),
          h("button", { class: "btn quiet danger", text: "Delete", onclick: async (e) => {
            if (!confirmDo(`Delete ${name}?`)) return;
            await busy(e.target, async () => { await api("DELETE", `${kind}/${encodeURIComponent(name)}`); toast(`Deleted ${name}.`); route(); });
          } }))));
    }
    if (!Object.keys(items).length) list.append(h("li", { class: "empty", text: `No ${cfg.title.toLowerCase()} yet. Add the first one above.` }));
    fill(el, pageHead(cfg.title, cfg.intro),
      h("div", { class: "panel" }, h("div", { class: "row" }, h("div", { class: "grow" }, field(cfg.name, nameIn))),
        field(cfg.value, valueIn, "Saving with an existing name replaces it."), save), list);
  };
}

/* ---- blocked users ---------------------------------------------------- */
VIEWS.blocks = async (el) => {
  const b = await api("GET", "blocks");
  const g = await guild().catch(() => ({}));
  const reason = h("input", { placeholder: "Why? (optional)" });
  const listOf = (rows, kind) => {
    const ul = h("ul", { class: "list" });
    for (const r of rows) {
      ul.append(h("li", {}, h("div", { class: "main" }, h("div", { class: "title", text: r.name }), h("div", { class: "sub", text: r.reason || "No reason given" })),
        h("div", { class: "actions" }, h("button", { class: "btn", text: "Unblock", onclick: async (e) => busy(e.target, async () => {
          await api("DELETE", `blocks/${kind}/${r.id}`); toast(`Unblocked ${r.name}.`); route();
        }) }))));
    }
    if (!rows.length) ul.append(h("li", { class: "empty", text: kind === "role" ? "No roles are blocked." : "Nobody is blocked." }));
    return ul;
  };
  const block = async (kind, id, name) => {
    try { await api("POST", "blocks", { kind, target: id, reason: reason.value }); toast(`Blocked ${name}.`); route(); }
    catch (e) { toast(e.message, true); }
  };
  const roles = mainGuild(g) ? mainGuild(g).roles : [];
  const rsel = roleSelect(roles, "", "Pick a role");
  fill(el, pageHead("Blocked users", "Blocked members and roles can't open tickets."),
    h("div", { class: "panel" }, field("Reason", reason),
      h("h3", { text: "Block a member" }), memberPicker((id, name) => block("user", id, name), "Block"),
      h("h3", { class: "mt", text: "Block a role" }),
      h("div", { class: "row" }, h("div", { class: "grow" }, rsel), h("button", { class: "btn", text: "Block role", onclick: () => rsel.value && block("role", rsel.value, rsel.selectedOptions[0].text) }))),
    h("h2", { text: "Members" }), listOf(b.users, "user"), h("h2", { text: "Roles" }), listOf(b.roles, "role"));
};

/* ---- settings ---------------------------------------------------------- */
VIEWS.settings = async (el) => {
  const items = await api("GET", "settings");
  const search = h("input", { type: "search", placeholder: "Find a setting" });
  const list = h("div", {});
  const row = (s) => {
    const isDefault = JSON.stringify(s.value) === JSON.stringify(s.default);
    let input;
    const save = h("button", { class: "btn primary", text: "Save" });
    const reset = h("button", { class: "btn quiet", text: "Reset", title: "Go back to the default", disabled: isDefault });
    const put = async (value) => {
      const r = await api("PUT", `settings/${s.key}`, { value });
      s.value = r.value;
      toast(r.message || `Saved ${humanize(s.key).toLowerCase()}.`);
      reset.disabled = JSON.stringify(s.value) === JSON.stringify(s.default);
    };
    if (s.kind === "boolean") {
      input = switchEl(!!s.value, async (e) => { try { await put(e.target.checked); } catch (err) { e.target.checked = !e.target.checked; toast(err.message, true); } }, humanize(s.key));
    } else if (s.kind === "choice") {
      input = h("select", {}, ...s.choices.map((c) => h("option", { value: c, text: humanize(c) })));
      input.value = String(s.value).toLowerCase();
      input.onchange = () => put(input.value).catch((e) => toast(e.message, true));
    } else if (s.kind === "json") {
      input = h("textarea", { value: JSON.stringify(s.value, null, 2) });
    } else if (s.kind === "color") {
      const hex = /^#?[0-9a-f]{6}$/i.test(String(s.value || "")) ? "#" + String(s.value).replace("#", "") : "#5865f2";
      const text = h("input", { class: "w-color", value: s.value ?? "" });
      const pick = h("input", { type: "color", value: hex, oninput: (e) => { text.value = e.target.value; } });
      input = h("span", { class: "row" }, pick, text);
      input.getValue = () => text.value;
    } else {
      input = h("input", { value: s.value ?? "", type: s.kind === "integer" ? "number" : "text" });
    }
    const getValue = () => (input.getValue ? input.getValue() : s.kind === "json" ? JSON.parse(input.value) : input.value);
    save.onclick = () => busy(save, () => put(getValue()));
    reset.onclick = () => busy(reset, async () => {
      const r = await api("DELETE", `settings/${s.key}`);
      s.value = r.value; toast(r.message || `Reset ${humanize(s.key).toLowerCase()}.`); render();
    });
    const ctrl = h("div", { class: "ctrl" }, input, s.kind === "boolean" || s.kind === "choice" ? null : save, reset);
    return h("div", { class: "setting", "data-search": `${s.key} ${s.description}`.toLowerCase() },
      h("div", {}, h("div", { class: "name", text: humanize(s.key) }), h("div", { class: "key", text: s.key }),
        s.description ? h("div", { class: "desc", text: plain(s.description) }) : null,
        !isDefault && s.default !== null && typeof s.default !== "object" ? h("div", { class: "desc", text: `Default: ${s.default}` }) : null),
      ctrl);
  };
  const render = () => {
    const q = search.value.trim().toLowerCase();
    fill(list, ...items.filter((s) => !q || `${s.key} ${humanize(s.key)} ${s.description}`.toLowerCase().includes(q)).map(row));
    if (!list.children.length) list.append(h("p", { class: "empty", text: "No settings match that." }));
  };
  search.addEventListener("input", render);
  fill(el, pageHead("Settings", "Everything you can change with ?config set, explained. Switches save straight away."), search, list);
  render();
};

/* ---- staff access ------------------------------------------------------ */
VIEWS.permissions = async (el) => {
  const [p, g] = await Promise.all([api("GET", "permissions"), guild()]);
  const staff = g.staff || mainGuild(g);
  const LEVELS = [
    ["OWNER", "Owner", "Everything, including bot settings, plugins and staff access."],
    ["ADMINISTRATOR", "Administrator", "The thread menu, pausing tickets and most setup."],
    ["MODERATOR", "Moderator", "Blocking people and moving tickets."],
    ["SUPPORTER", "Supporter", "Replying to and closing tickets, logs and snippets."],
  ];
  const change = async (body, msg) => { try { await api("POST", "permissions", body); toast(msg); route(); } catch (e) { toast(e.message, true); } };
  const blocks = LEVELS.map(([lvl, name, desc]) => {
    const chips = h("div", {}, ...(p.levels[lvl] || []).map((x) => h("span", { class: "chip" }, x.name,
      h("button", { "aria-label": `Remove ${x.name}`, text: "×", onclick: () => change({ kind: "level", name: lvl, target: x.id, add: false }, `Removed ${x.name} from ${name}.`) }))));
    if (!(p.levels[lvl] || []).length) chips.append(h("span", { class: "muted small", text: "Nobody has this level." }));
    const sel = roleSelect(staff.roles, "", "Add a role…");
    sel.prepend(h("option", { value: "-1", text: "@everyone" })); sel.value = "";
    sel.onchange = () => sel.value && change({ kind: "level", name: lvl, target: sel.value, add: true }, `Gave ${name} to ${sel.selectedOptions[0].text}.`);
    const pickerWrap = h("div", { hidden: true }, memberPicker((id, who) => change({ kind: "level", name: lvl, target: id, add: true }, `Gave ${name} to ${who}.`), "Add"));
    return h("div", { class: "panel" }, h("h3", { text: name }), h("p", { class: "muted small", text: desc }), chips,
      h("div", { class: "row mt" }, h("div", { class: "grow" }, sel), h("button", { class: "btn quiet", text: "Add a member", onclick: () => { pickerWrap.hidden = !pickerWrap.hidden; } })), pickerWrap);
  });

  const cmds = h("datalist", { id: "allcmds" }, ...p.all_commands.map((c) => h("option", { value: c })));
  const oTable = h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "Command" }), h("th", { text: "Who can use it" }), h("th", {}))));
  const ob = h("tbody");
  const levelSel = (v) => { const s = h("select", {}, ...["owner", "administrator", "moderator", "supporter", "regular"].map((l) => h("option", { value: l, text: humanize(l) }))); s.value = (v || "administrator").toLowerCase(); return s; };
  const setOverride = async (command, level, msg) => { try { await api("POST", "overrides", { command, level }); toast(msg); route(); } catch (e) { toast(e.message, true); } };
  for (const [cmd, lvl] of Object.entries(p.overrides)) {
    const s = levelSel(lvl);
    s.onchange = () => setOverride(cmd, s.value, `${cmd} now needs ${humanize(s.value)}.`);
    ob.append(h("tr", {}, h("td", { class: "mono", text: "?" + cmd }), h("td", {}, s),
      h("td", {}, h("button", { class: "btn quiet", text: "Use default", onclick: () => setOverride(cmd, "default", `${cmd} is back to its normal level.`) }))));
  }
  if (!Object.keys(p.overrides).length) ob.append(h("tr", {}, h("td", { colspan: 3, class: "muted", text: "No commands have changed levels." })));
  oTable.append(ob);
  const newCmd = h("input", { list: "allcmds", placeholder: "Command, e.g. funclaim" });
  const newLvl = levelSel("administrator");

  fill(el, pageHead("Staff access", `Who counts as staff, using roles from ${staff.name}. Each level can do everything the levels below it can.`),
    h("div", { class: "note", text: "People with Discord's Administrator permission in the staff server are treated as Administrators automatically. Bot owners in OWNERS always have full access." }),
    ...blocks,
    h("h2", { text: "Command levels" }), h("p", { class: "muted", text: "Make a single command need a higher or lower level than normal." }), cmds, oTable,
    h("div", { class: "row mt" }, h("div", { class: "grow" }, newCmd), newLvl,
      h("button", { class: "btn primary", text: "Set level", onclick: () => newCmd.value.trim() && setOverride(newCmd.value.trim(), newLvl.value, `${newCmd.value.trim()} now needs ${humanize(newLvl.value)}.`) })));
};

/* ---- plugins ----------------------------------------------------------- */
VIEWS.plugins = async (el) => {
  const p = await api("GET", "plugins");
  const out = h("div", {});
  const run = async (btn, action, name) => busy(btn, async () => {
    btn.textContent = action === "add" ? "Installing…" : action === "remove" ? "Removing…" : "Updating…";
    const r = await api("POST", "plugins", { action, name });
    fill(out, ...r.replies.map((x) => h("div", { class: x.error ? "alert" : "note", text: plain(x.text) })));
    if (r.ok) { toast(action === "add" ? "Installed." : action === "remove" ? "Removed." : "Updated."); await refreshCaps(); }
    setTimeout(route, r.ok ? 600 : 0);
  });
  const inst = h("ul", { class: "list" });
  for (const x of p.installed) {
    inst.append(h("li", {}, h("div", { class: "main" }, h("div", { class: "title mono", text: x.name }), h("div", { class: "sub", text: x.loaded ? "Running" : "Installed but not running. Check the bot's logs." })),
      h("div", { class: "actions" },
        h("button", { class: "btn", text: "Update", onclick: (e) => run(e.target, "update", x.name) }),
        h("button", { class: "btn danger", text: "Remove", onclick: (e) => confirmDo(`Remove ${x.name}? Its settings are kept.`) && run(e.target, "remove", x.name) }))));
  }
  if (!p.installed.length) inst.append(h("li", { class: "empty", text: "No plugins installed." }));
  const custom = h("input", { placeholder: "user/repo/plugin@branch, e.g. tomg12345667/Modmail/embed@master" });
  const reg = h("ul", { class: "list" });
  for (const r of p.registry) {
    const installed = p.installed.some((x) => x.name.includes(`/${r.name}@`) || x.name === r.name);
    reg.append(h("li", {}, h("div", { class: "main" }, h("div", { class: "title", text: r.title }), h("div", { class: "sub", text: plain(r.description).replace(/\[([^\]]+)\]\([^)]+\)/g, "$1") })),
      h("div", { class: "actions" }, installed ? h("span", { class: "muted small", text: "Installed" }) : h("button", { class: "btn", text: "Install", onclick: (e) => run(e.target, "add", r.name) }))));
  }
  fill(el, pageHead("Plugins", "Add features to this bot. Installing downloads the plugin from GitHub and starts it straight away."), out,
    h("h2", { text: "Installed" }), inst,
    h("h2", { text: "Install from GitHub" }), h("div", { class: "row" }, h("div", { class: "grow" }, custom), h("button", { class: "btn primary", text: "Install", onclick: (e) => custom.value.trim() && run(e.target, "add", custom.value.trim()) })),
    h("h2", { text: "Plugin list" }), reg);
};

/* ---- thread menu --------------------------------------------------------- */
VIEWS.threadmenu = async (el) => {
  const data = await api("GET", "threadmenu");
  const toList = (obj) => Object.entries(obj || {}).map(([key, o]) => ({ key, ...o }));
  const model = {
    enabled: !!data.thread_creation_menu_enabled,
    main: toList(data.thread_creation_menu_options),
    subs: Object.fromEntries(Object.entries(data.thread_creation_menu_submenus || {}).map(([n, o]) => [n, toList(o)])),
    extra: Object.fromEntries(Object.entries(data).filter(([k, v]) => !["thread_creation_menu_enabled", "thread_creation_menu_options", "thread_creation_menu_submenus"].includes(k) && (v === null || typeof v !== "object"))),
  };
  let dirty = false;
  const markDirty = () => { dirty = true; saveBtn.disabled = false; status.textContent = "Unsaved changes"; };
  const slug = (s, taken) => { let base = s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "") || "option"; let k = base, i = 2; while (taken.has(k)) k = `${base}_${i++}`; return k; };

  const optRow = (list, i, isSub) => {
    const o = list[i];
    const subNames = Object.keys(model.subs);
    const emoji = h("input", { class: "emoji", value: o.emoji || "", "aria-label": "Emoji", maxlength: 64, oninput: (e) => { o.emoji = e.target.value; markDirty(); } });
    const label = h("input", { value: o.label || "", "aria-label": "Label", maxlength: 100, placeholder: "What people pick", oninput: (e) => { o.label = e.target.value; markDirty(); } });
    const desc = h("input", { value: o.description || "", "aria-label": "Description", maxlength: 100, placeholder: "Short explanation (optional)", oninput: (e) => { o.description = e.target.value; markDirty(); } });
    const type = h("select", { "aria-label": "What it does" }, h("option", { value: "command", text: "Runs a command" }), h("option", { value: "submenu", text: "Opens a submenu", disabled: !subNames.length }));
    type.value = o.type === "submenu" ? "submenu" : "command";
    const cbWrap = h("div", {});
    const drawCb = () => {
      if (type.value === "submenu") {
        const s = h("select", { "aria-label": "Submenu" }, ...subNames.map((n) => h("option", { value: n, text: n })));
        if (!subNames.includes(o.callback)) o.callback = subNames[0];
        s.value = o.callback; s.onchange = () => { o.callback = s.value; markDirty(); };
        fill(cbWrap, s);
      } else {
        fill(cbWrap, h("input", { value: o.callback || "", "aria-label": "Command", class: "mono", placeholder: "e.g. reply Thanks, we'll be with you shortly", oninput: (e) => { o.callback = e.target.value; markDirty(); } }));
      }
    };
    type.onchange = () => { o.type = type.value; if (o.type === "command" && subNames.includes(o.callback)) o.callback = ""; drawCb(); markDirty(); };
    drawCb();
    const move = (d) => { const j = i + d; if (j < 0 || j >= list.length) return; [list[i], list[j]] = [list[j], list[i]]; markDirty(); draw(); };
    return h("div", { class: "opt" }, emoji, label, desc, type, cbWrap,
      h("div", { class: "tools" },
        h("button", { class: "btn quiet", text: "↑", title: "Move up", "aria-label": "Move up", disabled: i === 0, onclick: () => move(-1) }),
        h("button", { class: "btn quiet", text: "↓", title: "Move down", "aria-label": "Move down", disabled: i === list.length - 1, onclick: () => move(1) }),
        h("button", { class: "btn quiet danger", text: "Remove", onclick: () => { list.splice(i, 1); markDirty(); draw(); } })));
  };
  const block = (title, list, isSub, name) => {
    const max = isSub ? 24 : 25;
    const head = h("header", {}, h("div", { class: "row" }, h("h3", { text: title }), h("span", { class: "muted small", text: `${list.length} of ${max} options` })),
      isSub ? h("button", { class: "btn quiet danger", text: "Delete submenu", onclick: () => {
        const used = [model.main, ...Object.values(model.subs)].some((l) => l.some((o) => o.type === "submenu" && o.callback === name));
        if (used) return toast(`Something still opens ${name}. Change that option first.`, true);
        if (!confirmDo(`Delete the ${name} submenu?`)) return;
        delete model.subs[name]; markDirty(); draw();
      } }) : null);
    const rows = list.map((_, i) => optRow(list, i, isSub));
    return h("section", { class: "menu-block" }, head,
      list.length ? h("div", { class: "opt opt-head", "aria-hidden": "true" }, h("span", { text: "Emoji" }), h("span", { text: "Label" }), h("span", { text: "Description" }), h("span", { text: "Does" }), h("span", { text: "Command or submenu" }), h("span")) : null,
      ...rows,
      h("div", { class: "add" }, h("button", { class: "btn", text: "Add an option", disabled: list.length >= max, onclick: () => {
        const taken = new Set(list.map((o) => o.key));
        list.push({ key: slug("option " + (list.length + 1), taken), label: "", description: "", emoji: "", type: "command", callback: "" });
        markDirty(); draw();
      } })));
  };
  const status = h("span", { class: "muted", text: "No changes" });
  const saveBtn = h("button", { class: "btn primary", text: "Save menu", disabled: true });
  const holder = h("div", {});
  const newSub = h("input", { placeholder: "New submenu name, e.g. phone-brands" });
  const draw = () => {
    fill(holder, block("Main menu", model.main, false),
      ...Object.entries(model.subs).map(([n, l]) => block(`Submenu: ${n}`, l, true, n)));
  };
  saveBtn.onclick = () => busy(saveBtn, async () => {
    const pack = (list) => {
      const out = {}, taken = new Set();
      for (const o of list) {
        if (!o.label || !o.label.trim()) throw new Error("Every option needs a label.");
        const key = o.key && !taken.has(o.key) ? o.key : slug(o.label, taken);
        taken.add(key);
        out[key] = { label: o.label.trim(), description: (o.description || "").trim(), emoji: (o.emoji || "").trim() || null, type: o.type || "command", callback: o.callback || "" };
      }
      return out;
    };
    const body = { thread_creation_menu_enabled: model.enabled, thread_creation_menu_options: pack(model.main), thread_creation_menu_submenus: Object.fromEntries(Object.entries(model.subs).map(([n, l]) => [n, pack(l)])) };
    for (const [k, v] of Object.entries(model.extra)) body[k] = v === "" ? null : v;
    await api("PUT", "threadmenu", body);
    dirty = false; saveBtn.disabled = true; status.textContent = "Saved";
    toast("Saved the thread menu.");
  });
  const extras = Object.entries(model.extra).map(([k, v]) => field(humanize(k.replace("thread_creation_menu_", "")),
    h("input", { value: v ?? "", oninput: (e) => { model.extra[k] = e.target.value; markDirty(); } })));

  fill(el, pageHead("Thread menu", "The dropdown people see when they message the bot. Options either run a command or open a submenu."),
    h("div", { class: "row panel" }, switchEl(model.enabled, (e) => { model.enabled = e.target.checked; markDirty(); }, "Show the menu"),
      h("div", { class: "grow" }, h("strong", { text: "Show the menu when someone opens a ticket" }))),
    holder,
    h("div", { class: "row" }, h("div", { class: "grow" }, newSub), h("button", { class: "btn", text: "Add a submenu", onclick: () => {
      const n = newSub.value.trim().toLowerCase().replace(/\s+/g, "_");
      if (!n) return;
      if (model.subs[n]) return toast("There's already a submenu with that name.", true);
      model.subs[n] = []; newSub.value = ""; markDirty(); draw();
    } })),
    extras.length ? h("details", { class: "mt" }, h("summary", { text: "Menu text and timing" }), h("div", { class: "panel" }, ...extras)) : null,
    h("div", { class: "savebar" }, saveBtn, h("button", { class: "btn quiet", text: "Discard changes", onclick: () => route() }), status));
  draw();
  window.onbeforeunload = () => (dirty ? true : undefined);
};

/* ---- verification plugin -------------------------------------------- */
VIEWS.verification = async (el) => {
  const [v, g] = await Promise.all([api("GET", "verification"), guild()]);
  const main = mainGuild(g);
  const s = v.settings;
  const ROLE = ["verified_role", "unverified_role", "auto_role", "staff_role"];
  const TEXT = ["join_dm", "prompt_message", "verified_message", "kick_message"];
  const rows = Object.entries(v.help).map(([key, help]) => {
    let input;
    if (ROLE.includes(key)) input = roleSelect(main.roles, s[key], key === "verified_role" ? "Pick a role" : "None");
    else if (key === "log_channel") input = channelSelect(main.channels, s[key], "None");
    else if (key === "young_account_action") { input = h("select", {}, h("option", { value: "flag", text: "Flag them in the log" }), h("option", { value: "kick", text: "Kick them" })); input.value = s[key]; }
    else if (key === "close_after_verify") { input = h("select", {}, h("option", { value: "no", text: "No" }), h("option", { value: "yes", text: "Yes" })); input.value = s[key] ? "yes" : "no"; }
    else if (TEXT.includes(key)) input = h("textarea", { value: s[key] || "" });
    else if (key === "kick_after") input = h("input", { value: s[key] ? `${Math.round(s[key] / 3600)}h` : "", placeholder: "e.g. 24h, 3d (empty = never)" });
    else input = h("input", { value: s[key] ?? "" });
    const save = h("button", { class: "btn", text: "Save" });
    save.onclick = () => busy(save, async () => { await api("PUT", `verification/${key}`, { value: input.value }); toast(`Saved ${humanize(key).toLowerCase()}.`); });
    return h("div", { class: "setting" }, h("div", {}, h("div", { class: "name", text: humanize(key) }), h("div", { class: "desc", text: plain(help) })), h("div", { class: "ctrl" }, input, save));
  });
  const toggle = h("button", { class: "btn " + (s.enabled ? "" : "primary"), text: s.enabled ? "Turn off" : "Turn on" });
  toggle.onclick = () => busy(toggle, async () => { await api("POST", `verification/${s.enabled ? "disable" : "enable"}`); toast(s.enabled ? "Verification is off." : "Verification is on."); route(); });
  fill(el, pageHead("Verification", "New members message the bot your keyword, staff check them in the ticket and run ?verify.", toggle),
    h("ul", { class: "status-lines" },
      h("li", {}, h("span", { class: s.enabled ? "lamp on" : "lamp" }), s.enabled ? "Verification is on." : "Verification is off."),
      h("li", {}, h("span", { class: "lamp amber" }), `${v.pending} member${v.pending === 1 ? " is" : "s are"} waiting to verify.`)),
    h("p", { class: "muted small", text: "Messages can use {user}, {mention}, {server} and {keyword}." }), ...rows);
};

/* ---- message commands plugin ----------------------------------------- */
VIEWS.msgcmd = async (el) => {
  const [cmds, g] = await Promise.all([api("GET", "msgcmd"), guild()]);
  const main = mainGuild(g);
  const BUILTIN = ["user", "mention", "staff", "server", "channel"];
  const editor = h("div", { class: "panel", hidden: true });
  const open = (c) => {
    const name = h("input", { value: c ? c.name : "", disabled: !!c, placeholder: "e.g. outage" });
    const text = h("textarea", { value: c ? c.text : "", placeholder: "We have an outage from {start} to {end}. Sorry! - {staff}" });
    const qs = h("div", {});
    const questions = Object.fromEntries((c ? c.variables : []).map((x) => [x.name, x.question]));
    const drawQs = () => {
      const found = [...new Set([...text.value.matchAll(/\{([A-Za-z0-9_]{1,32})\}/g)].map((m) => m[1].toLowerCase()).filter((n) => !BUILTIN.includes(n)))];
      fill(qs, ...found.map((n) => field(`Question for {${n}}`, h("input", { value: questions[n] || "", placeholder: `What should {${n}} be?`, oninput: (e) => { questions[n] = e.target.value; } }))));
      if (!found.length) qs.append(h("p", { class: "muted small", text: "No variables yet. Put words in {curly braces} to ask for them when the command runs." }));
      return found;
    };
    text.addEventListener("input", drawQs);
    const dest = h("select", {}, h("option", { value: "here", text: "The channel it's used in" }), h("option", { value: "reply", text: "A reply to the ticket's member" }), h("option", { value: "anonreply", text: "An anonymous reply to the ticket's member" }),
      ...main.channels.map((ch) => h("option", { value: ch.id, text: "Always #" + ch.name })));
    dest.value = c ? c.destination : "here";
    const level = h("select", {}, ...["supporter", "moderator", "administrator", "owner"].map((l) => h("option", { value: l, text: humanize(l) })));
    level.value = c ? c.level : "supporter";
    const save = h("button", { class: "btn primary", text: c ? "Save changes" : "Create command" });
    save.onclick = () => busy(save, async () => {
      const n = name.value.trim().toLowerCase().replace(/^\?/, "");
      if (!n) throw new Error("Give the command a name.");
      const vars = drawQs().map((x) => ({ name: x, question: questions[x] || "" }));
      await api("PUT", `msgcmd/${encodeURIComponent(n)}`, { text: text.value, variables: vars, destination: dest.value, level: level.value });
      toast(c ? `Saved ?${n}.` : `Created ?${n}.`); route();
    });
    fill(editor, h("h3", { text: c ? `Edit ?${c.name}` : "New message command" }), field("Command name", name, "Staff will type ?name"), field("Message", text, "Filled in automatically: {user}, {mention}, {staff}, {server}, {channel}"),
      qs, field("Sends to", dest), field("Who can use it", level), h("div", { class: "row" }, save, h("button", { class: "btn quiet", text: "Cancel", onclick: () => { editor.hidden = true; } })));
    drawQs(); editor.hidden = false; name.focus();
  };
  const list = h("ul", { class: "list" });
  for (const c of cmds) {
    const where = { here: "the channel it's used in", reply: "a reply to the ticket's member", anonreply: "an anonymous reply" }[c.destination] || "#" + ((main.channels.find((x) => x.id === c.destination) || {}).name || "a channel");
    list.append(h("li", {}, h("div", { class: "main" }, h("div", { class: "title mono", text: "?" + c.name }), h("div", { class: "sub", text: c.text }),
      h("div", { class: "sub", text: `Asks for ${c.variables.map((x) => x.name).join(", ") || "nothing"}. Sends to ${where}. ${humanize(c.level)} and up.` })),
      h("div", { class: "actions" }, h("button", { class: "btn quiet", text: "Edit", onclick: () => open(c) }),
        h("button", { class: "btn quiet danger", text: "Delete", onclick: async (e) => {
          if (!confirmDo(`Delete ?${c.name}?`)) return;
          await busy(e.target, async () => { await api("DELETE", `msgcmd/${encodeURIComponent(c.name)}`); toast(`Deleted ?${c.name}.`); route(); });
        } }))));
  }
  if (!cmds.length) list.append(h("li", { class: "empty", text: "No message commands yet. Create one to send a message with blanks filled in." }));
  fill(el, pageHead("Message commands", "Your own commands that ask for a few details, then send a message.", h("button", { class: "btn primary", text: "New command", onclick: () => open(null) })), editor, list);
};

/* ------------------------------------------------------------------ */
window.addEventListener("hashchange", () => { window.onbeforeunload = null; route(); });
route();
