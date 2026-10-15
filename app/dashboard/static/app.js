"use strict";
// Дашборд диспетчерской. Права проверяет сервер; здесь только прячем недоступные кнопки.

const STATUS = {
  new: ["Новая", "accent"],
  in_progress: ["В работе", "flat"],
  done: ["Выполнена", "good"],
  cancelled: ["Отменена", "flat"],
};
const ARROW_UP = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
const ARROW_DOWN = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M5 12l7 7 7-7"/></svg>';
const DASH = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M6 12h12"/></svg>';

const state = { me: null, meta: null, page: "overview", scope: "active", staff: [] };
const $ = id => document.getElementById(id);

// --- помощники DOM (подписи — данные, вставляем только через textContent) ---
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;  // только для наших статичных SVG-иконок
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
function keepPairs(text) {
  return typeof text === "string" ? text.replace(/(\d) /g, "$1 ") : text;
}
function initials(name) {
  return (name || "").split(/\s+/).filter(w => /^\p{L}/u.test(w)).slice(0, 2).map(w => w[0].toUpperCase()).join("") || "?";
}
function fmtPhone(p) {
  return p && p.length === 11 ? `+7 ${p.slice(1, 4)} ${p.slice(4, 7)}-${p.slice(7, 9)}-${p.slice(9)}` : (p || "—");
}
function fmtPct(v) {
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}${Math.abs(v).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
}
const can = perm => !!state.me && state.me.perms.includes(perm);

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 3500);
}

// --- запросы к серверу ---
async function api(url, { method = "GET", body } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["content-type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (res.status === 401) { location.replace("/login"); throw new Error("Нужно войти"); }
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? "Проверьте введённые данные" : data.detail;
    throw new Error(detail || `Ошибка ${res.status}`);
  }
  return data;
}

function showError(message) {
  const box = $("error");
  box.textContent = message || "";
  box.hidden = !message;
}

function table(head, rows) {
  return el("table", {},
    el("thead", {}, el("tr", {}, head.map(([text, cls]) => el("th", { class: cls }, text)))),
    el("tbody", {}, rows),
  );
}
function who(name) {
  return el("div", { class: "who" }, el("span", { class: "avatar", "aria-hidden": "true" }, initials(name)), name);
}
function statusBadge(status) {
  const [title, tone] = STATUS[status] || [status, "flat"];
  return el("span", { class: `badge ${tone}` }, title);
}

// ================= Обзор =================

function tone(change, upIs) {
  if (change == null || change === 0 || upIs === "neutral") return "flat";
  return (change > 0) === (upIs === "good") ? "good" : "bad";
}

function kpiCard({ label, value, change, upIs, sub }) {
  const t = tone(change, upIs);
  const icon = change == null || change === 0 ? DASH : change > 0 ? ARROW_UP : ARROW_DOWN;
  return el("article", { class: "card kpi" },
    el("div", { class: "kpi-top" }, el("span", { class: "kpi-label" }, label),
      change == null ? null : el("span", { class: `badge ${t}`, title: "к предыдущему периоду" }, fmtPct(change))),
    el("div", { class: "kpi-value" }, keepPairs(value), change == null ? null : el("span", { class: `arrow ${t}`, html: icon })),
    el("div", { class: "kpi-sub" }, sub),
  );
}

function renderOverview(d) {
  const p = d.period, c = d.change;
  const vs = `к предыдущим ${d.days} дням`, noPrev = "нет данных за прошлый период";
  $("kpis").replaceChildren(
    kpiCard({ label: "Поступило", value: String(p.created), change: c.created, upIs: "neutral", sub: c.created == null ? noPrev : vs }),
    kpiCard({ label: "Закрыто", value: String(p.done), change: c.done, upIs: "good",
      sub: p.on_time_pct != null ? `в срок ${p.on_time_pct}% · ${c.done == null ? noPrev : vs}` : vs }),
    kpiCard({ label: "Время до взятия", value: p.avg_take ?? "—", change: c.avg_take, upIs: "bad",
      sub: c.avg_take == null ? noPrev : `в среднем, ${vs}` }),
    kpiCard({ label: "Время выполнения", value: p.avg_close ?? "—", change: c.avg_close, upIs: "bad",
      sub: c.avg_close == null ? "от назначения до «Готово»" : `от назначения до «Готово», ${vs}` }),
  );

  const n = d.now;
  const row = (label, hint, value, extra) => el("div", { class: "queue-row" },
    el("div", { class: "queue-label" }, label, el("small", {}, hint)),
    el("div", { class: "queue-value" }, String(value), extra));
  $("queue").replaceChildren(
    row("Новые", "ждут исполнителя", n.new),
    row("В работе", "у исполнителей", n.in_progress),
    row("Просрочено", "новые и в работе", n.overdue,
      n.overdue > 0 ? el("span", { class: "badge bad" }, "дольше нормы") : el("span", { class: "badge good" }, "всё в срок")),
  );

  $("assignees-sub").textContent = `за ${d.days} дней`;
  if (!d.assignees.length) {
    $("assignees").replaceChildren(el("div", { class: "empty" }, "За период никто не закрывал заявки"));
  } else {
    const max = Math.max(1, ...d.assignees.map(a => a.done));
    $("assignees").replaceChildren(table(
      [["Исполнитель"], ["Закрыто", "num"], ["В работе", "num"], ["Ср. время выполнения", "num"], ["В срок", "num"]],
      d.assignees.map(a => {
        const meter = el("span", { class: "meter" }, el("b"));
        meter.firstChild.style.width = `${(a.done / max) * 100}%`;
        return el("tr", {},
          el("td", {}, who(a.name)),
          el("td", { class: "num strong" }, a.done, meter),
          el("td", { class: "num" }, a.in_progress),
          el("td", { class: "num" }, keepPairs(a.avg_close ?? "—")),
          el("td", { class: "num" }, a.on_time_pct != null ? `${a.on_time_pct}%` : "—"));
      })));
  }

  if (!d.overdue.length) {
    $("overdue").replaceChildren(el("div", { class: "empty" }, "Просроченных заявок нет"));
  } else {
    $("overdue").replaceChildren(table(
      [["№"], ["Категория"], ["Место"], ["Проблема"], ["Статус"], ["Исполнитель"], ["Висит", "num"], ["Сверх нормы", "num"]],
      d.overdue.map(r => el("tr", { class: "clickable", tabindex: 0, onclick: () => openRequestById(r.id) },
        el("td", { class: "muted" }, `№${r.id}`),
        el("td", {}, r.category),
        el("td", {}, r.place),
        el("td", { class: "desc" }, r.description),
        el("td", {}, statusBadge(r.status)),
        el("td", {}, r.assignee ? who(r.assignee) : el("span", { class: "muted" }, "—")),
        el("td", { class: "num" }, keepPairs(r.age)),
        el("td", { class: "num" }, el("span", { class: "badge bad" }, keepPairs(`+${r.over_by}`)))))));
  }

  $("demo").hidden = !d.demo;
  $("updated").textContent = d.generated_at;
  $("org-name").textContent = d.org_name;
}

async function loadOverview() {
  const days = Number($("period").value);
  $("content").classList.add("loading");
  try {
    renderOverview(await api(`/api/stats?days=${days}`));
    showError("");
    try { localStorage.setItem("days", String(days)); } catch (_) {}
  } catch (e) {
    showError(`Не удалось загрузить данные: ${e.message}`);
  } finally {
    $("content").classList.remove("loading");
  }
}

// ================= Заявки =================

let requestsCache = [];

async function loadRequests() {
  try {
    const data = await api(`/api/requests?scope=${state.scope}`);
    requestsCache = data.items;
    $("req-total").textContent = data.total > data.items.length
      ? `показано ${data.items.length} из ${data.total}` : `всего: ${data.total}`;
    renderRequests();
    showError("");
  } catch (e) {
    showError(`Не удалось загрузить заявки: ${e.message}`);
  }
}

const PRIORITY_MARK = { urgent: "🔴", high: "🟠" };

function priorityBadge(r) {
  if (r.priority === "urgent") return el("span", { class: "badge bad" }, "срочно");
  if (r.priority === "high") return el("span", { class: "badge warn" }, "высокий");
  if (r.priority === "low") return el("span", { class: "badge flat" }, "низкий");
  return null;
}

function dueCell(r) {
  if (!r.due_at || !(r.status === "new" || r.status === "in_progress")) return el("span", { class: "muted" }, "—");
  return r.overdue
    ? el("span", { class: "badge bad", title: "срок прошёл" }, keepPairs(`до ${r.due_at}`))
    : el("span", { class: "nowrap", title: r.due_left ? `осталось ${r.due_left}` : "" }, `до ${r.due_at}`);
}

// Заявитель: ссылка на профиль MAX. В браузере max:// открывается, только если стоит приложение MAX,
// поэтому рядом — «прислать мне в MAX»: бот пришлёт ссылку, по которой профиль откроется точно.
function applicantLink(r) {
  if (!r.applicant) return el("span", { class: "muted" }, "—");
  return el("a", {
    href: r.applicant_url, class: "profile-link", title: "Открыть профиль в приложении MAX",
    onclick: e => e.stopPropagation(),
  }, r.applicant);
}

function renderRequests() {
  document.querySelectorAll("#req-tabs button").forEach(b =>
    b.setAttribute("aria-selected", String(b.dataset.scope === state.scope)));
  if (!requestsCache.length) {
    const empty = {
      active: "Активных заявок нет", mine: "У вас нет заявок в работе",
      direction: "По вашим направлениям активных заявок нет", closed: "Закрытых заявок нет",
    };
    $("requests").replaceChildren(el("div", { class: "empty" }, empty[state.scope]));
    return;
  }
  const closed = state.scope === "closed";
  const showApplicant = can("work");
  const head = [["№"], ["Создана"], ["Категория"], ["Место"], ["Проблема"]];
  if (showApplicant) head.push(["Заявитель"]);
  head.push(["Статус"], ["Исполнитель"], closed ? ["Время работы", "num"] : ["Срок"]);
  $("requests").replaceChildren(table(head,
    requestsCache.map(r => el("tr", {
      class: "clickable", tabindex: 0,
      onclick: () => openRequest(r),
      onkeydown: e => { if (e.key === "Enter") openRequest(r); },
    },
      el("td", { class: "muted nowrap" }, PRIORITY_MARK[r.priority] ? `${PRIORITY_MARK[r.priority]} №${r.id}` : `№${r.id}`),
      el("td", { class: "muted" }, r.created_at),
      el("td", {}, r.category_title),
      el("td", {}, `${r.building_title}, каб. ${r.room}`),
      el("td", { class: "desc" }, r.description),
      showApplicant ? el("td", {}, applicantLink(r)) : null,
      el("td", {}, el("div", { class: "chips" }, statusBadge(r.status), priorityBadge(r))),
      el("td", {}, r.assignee ? who(r.assignee) : el("span", { class: "muted" }, "—")),
      closed ? el("td", { class: "num" }, keepPairs(r.age)) : el("td", {}, dueCell(r)),
    )),
  ));
}

async function openRequestById(id) {
  // Из «Обзора»: ищем заявку среди активных
  try {
    const data = await api("/api/requests?scope=active");
    const r = data.items.find(i => i.id === id);
    if (r) openRequest(r); else toast("Заявка уже закрыта");
  } catch (e) { toast(e.message); }
}

function selectInput(name, options, value) {
  return el("select", { class: "input", name },
    options.map(([v, t]) => el("option", { value: v, selected: String(v) === String(value) }, t)));
}

function row(...children) {
  return el("div", { class: "checks" }, ...children);
}

function grow(child) {
  return el("div", { style: "flex:1;min-width:180px" }, child);
}

async function loadHistory(id, box) {
  try {
    const { items } = await api(`/api/requests/${id}/history`);
    box.replaceChildren(...items.map(i => el("li", {},
      el("span", { class: "muted nowrap" }, i.at), " ", i.text, i.who ? el("span", { class: "muted" }, ` — ${i.who}`) : null)));
  } catch (e) {
    box.replaceChildren(el("li", { class: "muted" }, `Не удалось загрузить историю: ${e.message}`));
  }
}

function openRequest(r) {
  const dlg = $("req-dialog");
  $("req-title").textContent = `Заявка №${r.id}`;
  const active = r.status === "new" || r.status === "in_progress";

  async function run(promise, message) {
    try {
      await promise;
      toast(message);
      dlg.close();
      await refreshCurrent();
    } catch (e) {
      toast(e.message);
    }
  }
  const post = (path, payload) => api(`/api/requests/${r.id}/${path}`, { method: "POST", body: payload });

  let dueText = "—";
  if (r.due_at) {
    dueText = [`до ${r.due_at}`];
    if (active && r.overdue) dueText.push(" ", el("span", { class: "badge bad" }, "просрочена"));
    else if (active && r.due_left) dueText.push(el("span", { class: "hint" }, ` · осталось ${r.due_left}`));
    if (r.due_manual) dueText.push(el("span", { class: "hint" }, " · изменён вручную"));
  }

  const applicant = r.applicant ? [el("dt", {}, "Заявитель"), el("dd", {},
    applicantLink(r), " ",
    el("button", { class: "btn sm ghost", type: "button", title: "Бот пришлёт вам в MAX ссылку на профиль заявителя",
      onclick: async () => {
        try { toast((await post("contact", {})).message); } catch (e) { toast(e.message); }
      } }, "Прислать мне в MAX"))] : null;

  const facts = el("dl", { class: "facts" },
    el("dt", {}, "Статус"), el("dd", {}, el("div", { class: "chips" }, statusBadge(r.status), priorityBadge(r))),
    el("dt", {}, "Приоритет"), el("dd", {}, r.priority_title),
    active ? [el("dt", {}, "Срок"), el("dd", {}, dueText)] : null,
    el("dt", {}, "Категория"), el("dd", {}, r.category_title, r.category_source === "manual" ? el("span", { class: "hint" }, " · исправлена вручную") : null),
    el("dt", {}, "Место"), el("dd", {}, `${r.building_title}, каб. ${r.room}`),
    el("dt", {}, "Проблема"), el("dd", {}, r.description),
    applicant,
    el("dt", {}, "Исполнитель"), el("dd", {}, r.assignee || "—"),
    el("dt", {}, "Создана"), el("dd", {}, r.created_at),
    el("dt", {}, active ? "Висит" : "Время работы"), el("dd", {}, r.age),
  );
  const body = [facts];

  if (active && can("admin_requests")) {
    // Приоритет
    const prio = selectInput("priority", state.meta.priorities.map(p => [p.code, p.title]), r.priority);
    body.push(el("div", { class: "section-title" }, "Приоритет"),
      row(grow(prio), el("button", { class: "btn", type: "button",
        onclick: () => run(post("priority", { priority: prio.value }), "Приоритет изменён") }, "Изменить")));

    // Срок
    const dueInput = el("input", { class: "input", type: "datetime-local", value: r.due_iso || "" });
    const comment = el("input", { class: "input", placeholder: "Причина — её увидят заявитель и исполнитель", maxlength: 300 });
    const shift = hours => run(post("due", { shift_hours: hours, comment: comment.value }), "Срок перенесён");
    body.push(el("div", { class: "section-title" }, "Срок выполнения"),
      row(
        el("button", { class: "btn sm", type: "button", onclick: () => shift(24) }, "+1 день"),
        el("button", { class: "btn sm", type: "button", onclick: () => shift(72) }, "+3 дня"),
        el("button", { class: "btn sm", type: "button", onclick: () => shift(168) }, "+1 неделя"),
        r.due_manual ? el("button", { class: "btn sm ghost", type: "button",
          onclick: () => run(post("due", { reset: true, comment: comment.value }), "Срок возвращён к нормативу") },
          "По нормативу") : null,
      ),
      el("div", { style: "height:8px" }),
      row(grow(dueInput), el("button", { class: "btn", type: "button", onclick: () => {
        if (!dueInput.value) return toast("Выберите дату и время");
        run(post("due", { due_at: dueInput.value, comment: comment.value }), "Срок изменён");
      } }, "Перенести")),
      el("div", { style: "height:8px" }), comment);
  }

  if (active && can("work")) {
    const cat = selectInput("category", state.meta.categories.filter(c => c.active || c.code === r.category)
      .map(c => [c.code, c.title]), r.category);
    body.push(el("div", { class: "section-title" }, "Категория"),
      row(grow(cat), el("button", { class: "btn", type: "button",
        onclick: () => run(post("category", { category: cat.value }), "Категория изменена") }, "Изменить")));
  }

  if (active && can("admin_requests")) {
    const assignees = state.meta.assignees;
    const sel = selectInput("assignee", [["", "Выберите исполнителя"], ...assignees.map(a => [a.id, a.name])], r.assignee_id ?? "");
    body.push(el("div", { class: "section-title" }, "Назначить исполнителя"),
      row(grow(sel), el("button", { class: "btn", type: "button", onclick: () => {
        if (!sel.value) return toast("Выберите исполнителя");
        run(post("assign", { assignee_id: Number(sel.value) }), "Исполнитель назначен");
      } }, "Назначить")));

    const building = selectInput("building", state.meta.buildings.map(b => [b.code, b.title]), r.building);
    const room = el("input", { class: "input", name: "room", value: r.room, maxlength: 20 });
    const desc = el("textarea", { class: "input", name: "description", maxlength: 1000 });
    desc.value = r.description;
    body.push(el("div", { class: "section-title" }, "Изменить заявку"),
      el("label", { class: "field" }, el("span", {}, "Корпус"), building),
      el("label", { class: "field" }, el("span", {}, "Кабинет"), room),
      el("label", { class: "field" }, el("span", {}, "Проблема"), desc),
      el("button", { class: "btn", type: "button", onclick: () => run(
        api(`/api/requests/${r.id}`, { method: "PATCH", body: { building: building.value, room: room.value, description: desc.value } }),
        "Заявка изменена") }, "Сохранить изменения"));
  }

  const history = el("ul", { class: "history" }, el("li", { class: "muted" }, "Загрузка…"));
  body.push(el("div", { class: "section-title" }, "История"), history);
  $("req-body").replaceChildren(...body);
  loadHistory(r.id, history);

  const foot = [];
  if (active && can("work")) {
    foot.push(el("button", { class: "btn danger", type: "button", onclick: () => {
      if (confirm(`Отменить заявку №${r.id}? Заявитель получит уведомление.`)) run(post("action", { action: "cancel" }), "Заявка отменена");
    } }, "Отменить заявку"), el("span", { class: "spacer" }));
    if (r.status === "new") {
      foot.push(el("button", { class: "btn primary", type: "button", onclick: () => run(post("action", { action: "take" }), "Заявка взята в работу") }, "Взять в работу"));
    } else {
      foot.push(
        el("button", { class: "btn", type: "button", onclick: () => run(post("action", { action: "release" }), "Заявка возвращена в очередь") }, "Вернуть в очередь"),
        el("button", { class: "btn primary", type: "button", onclick: () => run(post("action", { action: "done" }), "Заявка выполнена") }, "Готово"));
    }
  } else {
    foot.push(el("button", { class: "btn", type: "button", "data-close": true }, "Закрыть"));
  }
  $("req-foot").replaceChildren(...foot);
  dlg.showModal();
}

// ================= Сотрудники =================

async function loadStaff() {
  try {
    state.staff = (await api("/api/staff")).items;
    renderStaff();
    showError("");
  } catch (e) {
    showError(`Не удалось загрузить сотрудников: ${e.message}`);
  }
}

function renderStaff() {
  const titles = Object.fromEntries(state.meta.categories.map(c => [c.code, c.title]));
  if (!state.staff.length) {
    $("staff").replaceChildren(el("div", { class: "empty" }, "Сотрудников пока нет"));
    return;
  }
  $("staff").replaceChildren(table(
    [["Сотрудник"], ["Телефон"], ["Роль"], ["Направления"], ["Бот"], ["Линия"], ["Статус"]],
    state.staff.map(a => el("tr", {
      class: "clickable", tabindex: 0,
      onclick: () => openStaff(a),
      onkeydown: e => { if (e.key === "Enter") openStaff(a); },
    },
      el("td", {}, who(a.name)),
      el("td", { class: "muted nowrap" }, fmtPhone(a.phone)),
      el("td", {}, el("span", { class: `badge ${a.role === "admin" ? "accent" : "flat"}` }, a.role_title)),
      el("td", {}, a.role === "viewer" ? el("span", { class: "muted" }, "—")
        : el("div", { class: "chips" }, a.directions.length ? a.directions.map(d => el("span", { class: "badge flat" }, titles[d] || d)) : el("span", { class: "muted" }, "не заданы"))),
      el("td", {}, a.linked ? "подключён" : el("span", { class: "nowrap" }, el("span", { class: "muted" }, "ещё не входил"),
        el("button", { class: "btn sm soft invite", type: "button", title: "Скопировать приглашение для сотрудника",
          onclick: e => { e.stopPropagation(); copyInvite(a); } }, "Пригласить"))),
      el("td", {}, a.role === "viewer" ? el("span", { class: "muted" }, "—")
        : el("span", { class: "who" }, el("span", { class: `dot ${a.on_duty ? "on" : "off"}` }), a.on_duty ? "на линии" : "нет")),
      el("td", {}, a.active ? el("span", { class: "badge good" }, "активен") : el("span", { class: "badge bad" }, "отключён")),
    )),
  ));
}

let editing = null;

async function copyInvite(a) {
  const link = state.meta.staff_link;
  const text = link
    ? `${a.name}, здравствуйте! Чтобы работать с заявками диспетчерской, откройте бота по ссылке и нажмите «Поделиться номером»: ${link}`
    : `${a.name}, здравствуйте! Чтобы работать с заявками диспетчерской, напишите боту диспетчерской команду /staff и нажмите «Поделиться номером».`;
  try {
    await navigator.clipboard.writeText(text);
    toast("Приглашение скопировано — отправьте его сотруднику");
  } catch (_) {
    prompt("Скопируйте приглашение:", text);
  }
}

function openStaff(a) {
  editing = a;
  const form = $("staff-form");
  form.reset();
  $("staff-error").hidden = true;
  $("staff-title").textContent = a ? a.name : "Новый сотрудник";
  form.name.value = a ? a.name : "";
  form.phone.value = a ? fmtPhone(a.phone) : "";
  form.role.value = a ? a.role : "specialist";
  form.active.checked = a ? a.active : true;
  $("active-field").hidden = !a;
  $("staff-delete").hidden = !a || a.id === state.me.id;
  $("phone-hint").hidden = !a;
  const dirs = new Set(a ? a.directions : []);
  $("staff-directions").replaceChildren(...state.meta.categories.map(c =>
    el("label", { class: "check" }, el("input", { type: "checkbox", value: c.code, checked: dirs.has(c.code) }), c.title)));
  syncRoleFields();
  $("staff-dialog").showModal();
  form.name.focus();
}

function syncRoleFields() {
  $("directions-field").hidden = $("staff-form").role.value === "viewer";
}

async function saveStaff(e) {
  e.preventDefault();
  const form = $("staff-form");
  const payload = {
    name: form.name.value.trim(),
    phone: form.phone.value,
    role: form.role.value,
    directions: form.role.value === "viewer" ? []
      : [...$("staff-directions").querySelectorAll("input:checked")].map(i => i.value),
  };
  if (editing) payload.active = form.active.checked;
  try {
    if (editing) await api(`/api/staff/${editing.id}`, { method: "PATCH", body: payload });
    else await api("/api/staff", { method: "POST", body: payload });
    $("staff-dialog").close();
    toast(editing ? "Сотрудник сохранён" : "Сотрудник добавлен. Пусть напишет боту /staff и поделится номером.");
    await Promise.all([loadStaff(), loadMeta()]);
  } catch (err) {
    $("staff-error").textContent = err.message;
    $("staff-error").hidden = false;
  }
}

async function deleteStaff() {
  if (!editing || !confirm(`Удалить сотрудника «${editing.name}»?`)) return;
  try {
    const res = await api(`/api/staff/${editing.id}`, { method: "DELETE" });
    $("staff-dialog").close();
    toast(res.archived ? "У сотрудника есть история заявок — он отключён, история сохранена" : "Сотрудник удалён");
    await Promise.all([loadStaff(), loadMeta()]);
  } catch (err) {
    $("staff-error").textContent = err.message;
    $("staff-error").hidden = false;
  }
}

// ================= Категории =================

let categoriesCache = [];
let editingCat = null;

async function loadCategories() {
  try {
    categoriesCache = (await api("/api/categories")).items;
    renderCategories();
    showError("");
  } catch (e) {
    showError(`Не удалось загрузить категории: ${e.message}`);
  }
}

function renderCategories() {
  $("categories").replaceChildren(table(
    [["Категория"], ["Норматив", "num"], ["Ключевые слова"], ["Заявок", "num"], ["Статус"]],
    categoriesCache.map(c => el("tr", {
      class: "clickable", tabindex: 0,
      onclick: () => openCategory(c),
      onkeydown: e => { if (e.key === "Enter") openCategory(c); },
    },
      el("td", { class: "strong" }, c.title, c.fallback ? el("span", { class: "hint" }, " · по умолчанию") : null),
      el("td", { class: "num" }, keepPairs(`${c.sla_hours} ч`)),
      el("td", { class: "desc" }, c.keywords.length ? c.keywords.slice(0, 8).join(", ") + (c.keywords.length > 8 ? "…" : "") : el("span", { class: "muted" }, "—")),
      el("td", { class: "num" }, c.requests),
      el("td", {}, c.active ? el("span", { class: "badge good" }, "активна") : el("span", { class: "badge flat" }, "отключена")),
    )),
  ));
}

function openCategory(c) {
  editingCat = c;
  const form = $("cat-form");
  form.reset();
  $("cat-error").hidden = true;
  $("cat-title").textContent = c ? c.title : "Новая категория";
  form.title.value = c ? c.title : "";
  form.sla_hours.value = c ? c.sla_hours : 24;
  form.keywords.value = c ? c.keywords.join(", ") : "";
  form.active.checked = c ? c.active : true;
  $("cat-active-field").hidden = !c || c.fallback;
  $("cat-fallback-hint").hidden = !(c && c.fallback);
  $("cat-delete").hidden = !c || c.fallback;
  $("cat-dialog").showModal();
  form.title.focus();
}

async function saveCategory(e) {
  e.preventDefault();
  const form = $("cat-form");
  const payload = {
    title: form.title.value.trim(),
    sla_hours: Number(form.sla_hours.value),
    keywords: form.keywords.value.split(/[,;\n]/).map(w => w.trim()).filter(Boolean),
  };
  if (editingCat && !editingCat.fallback) payload.active = form.active.checked;
  try {
    if (editingCat) await api(`/api/categories/${editingCat.code}`, { method: "PATCH", body: payload });
    else await api("/api/categories", { method: "POST", body: payload });
    $("cat-dialog").close();
    toast(editingCat ? "Категория сохранена" : "Категория добавлена");
    await Promise.all([loadCategories(), loadMeta()]);
  } catch (err) {
    $("cat-error").textContent = err.message;
    $("cat-error").hidden = false;
  }
}

async function deleteCategory() {
  if (!editingCat || !confirm(`Удалить категорию «${editingCat.title}»?`)) return;
  try {
    const res = await api(`/api/categories/${editingCat.code}`, { method: "DELETE" });
    $("cat-dialog").close();
    toast(res.archived ? "По категории есть заявки — она отключена, история сохранена" : "Категория удалена");
    await Promise.all([loadCategories(), loadMeta()]);
  } catch (err) {
    $("cat-error").textContent = err.message;
    $("cat-error").hidden = false;
  }
}

// ================= Навигация и запуск =================

const PAGES = {
  overview: { title: "Обзор", load: loadOverview, perm: "analytics" },
  requests: { title: "Заявки", load: loadRequests },
  categories: { title: "Категории", load: loadCategories, perm: "manage_categories" },
  staff: { title: "Сотрудники", load: loadStaff, perm: "manage_staff" },
};

function route() {
  // Специалисту аналитика недоступна — его стартовая страница «Заявки»
  const home = can("analytics") ? "overview" : "requests";
  let page = location.hash.slice(1) || home;
  if (!PAGES[page] || (PAGES[page].perm && !can(PAGES[page].perm))) page = home;
  state.page = page;
  for (const name of Object.keys(PAGES)) $(`page-${name}`).hidden = name !== page;
  $("page-title").textContent = PAGES[page].title;
  $("period-pill").hidden = page !== "overview";
  document.querySelector(".sidebar-foot .version").hidden = page !== "overview";
  document.querySelectorAll(".nav a").forEach(a => {
    if (a.dataset.page === page) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  showError("");
  PAGES[page].load();
}

function refreshCurrent() {
  return PAGES[state.page].load();
}

async function loadMeta() {
  state.meta = await api("/api/meta");
}

async function start() {
  state.me = await api("/api/me");
  await loadMeta();

  document.querySelectorAll("[data-perm]").forEach(n => { n.hidden = !can(n.dataset.perm); });
  document.querySelectorAll("[data-perm-any]").forEach(n => {
    n.hidden = !n.dataset.permAny.split(" ").some(can);
  });
  $("org-name").textContent = state.me.org_name;
  $("demo").hidden = !state.me.demo;
  $("staff-role").replaceChildren(...state.meta.roles.map(r => el("option", { value: r.code }, r.title)));
  if (!state.me.demo) {
    $("user-chip").hidden = false;
    $("user-initials").textContent = initials(state.me.name);
    $("user-name").textContent = state.me.name;
    $("user-role").textContent = state.me.role_title;
  }

  let days = 30;
  try { days = Number(localStorage.getItem("days")) || 30; } catch (_) {}
  $("period").value = [7, 30, 90].includes(days) ? String(days) : "30";

  window.addEventListener("hashchange", route);
  route();
}

$("theme-toggle").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme !== "dark";
  if (dark) document.documentElement.dataset.theme = "dark"; else delete document.documentElement.dataset.theme;
  try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (_) {}
});
$("period").addEventListener("change", loadOverview);
$("req-tabs").addEventListener("click", e => {
  const scope = e.target.closest("button")?.dataset.scope;
  if (scope) { state.scope = scope; loadRequests(); }
});
$("staff-add").addEventListener("click", () => openStaff(null));
$("cat-add").addEventListener("click", () => openCategory(null));
$("cat-form").addEventListener("submit", saveCategory);
$("cat-delete").addEventListener("click", deleteCategory);
$("staff-form").addEventListener("submit", saveStaff);
$("staff-form").role.addEventListener("change", syncRoleFields);
$("staff-delete").addEventListener("click", deleteStaff);
$("logout").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST", body: {} }).catch(() => {});
  location.replace("/login");
});
document.addEventListener("click", e => {
  if (e.target.closest("[data-close]")) e.target.closest("dialog")?.close();
});

start().catch(e => showError(`Не удалось открыть дашборд: ${e.message}`));
