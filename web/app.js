"use strict";
const $ = id => document.getElementById(id);
const statusNames = {idle:"Не подключено",connecting:"Подключаемся",connected:"На связи",reconnecting:"Переподключение",error:"Нет соединения",checked:"Проверка пройдена"};
let state = null, initial = true, submitting = false, lastEvents = "", lastMessages = "", nodesSignature = "";
let panelLost = false;
const roles = {team:"Команда",guest:"Гость"};
function element(tag, cls, text) { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = text; return node; }
function timeText(value) { return new Date(value).toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit",second:"2-digit"}); }
function clean(value) { if(value === null || value === undefined) return "—"; return typeof value === "object" ? JSON.stringify(value) : String(value); }
function freshness() {
  if (!state) return;
  $("clock").textContent = timeText(Date.now());
  const seconds = state.last_telemetry_at ? Math.max(0,Math.round((Date.now()-new Date(state.last_telemetry_at))/1000)) : null;
  $("last-data").textContent = seconds === null ? "—" : seconds < 2 ? "сейчас" : seconds < 60 ? `${seconds} с назад` : `${Math.floor(seconds/60)} мин назад`;
  $("freshness").textContent = seconds !== null && state.status !== "connected" ? "Показан последний полученный снимок. Соединение сейчас не активно." : seconds > 10 ? "Данные не обновлялись более 10 секунд. Проверьте связь с куполом." : "";
}
function updateButtons() {
  const active = state && ["connecting","connected","reconnecting"].includes(state.status);
  $("connect-button").disabled = submitting || !state?.key_configured || active;
  $("check-button").disabled = submitting || !state?.key_configured || active;
  $("disconnect-button").disabled = submitting || !active;
}
function renderNodes() {
  if (!state) return;
  const query = $("search").value.toLowerCase();
  const catalog = new Map((state.catalog || []).filter(x=>x && typeof x==="object").map(x=>[x.node_id,x]));
  const allNodes = Object.entries(state.nodes || {});
  const nodes = allNodes.filter(([id,node]) => `${id} ${catalog.get(id)?.title || ""}`.toLowerCase().includes(query));
  const signature = JSON.stringify([nodes, state.catalog, query, state.status]);
  if (signature === nodesSignature) return;
  nodesSignature = signature;
  const opened = new Set([...document.querySelectorAll(".node-card[open]")].map(x=>x.dataset.id));
  const container = $("nodes");
  container.replaceChildren();
  for (const [id, raw] of nodes) {
    const data = raw && typeof raw === "object" ? raw : {};
    const card = element("details","node-card"); card.dataset.id = id; card.open = opened.has(id);
    const heading = element("summary");
    heading.append(element("span",`node-indicator ${data.online === true ? "online" : data.online === false ? "offline" : ""}`),element("span","node-name",catalog.get(id)?.title || id), element("span","","⌄"));
    card.append(heading,element("div","node-id",id));
    const chips = element("div","node-summary");
    const fields = [["state",""],["soc_pct","Заряд: "],["co2_ppm","CO₂: "],["temperature_c","°C: "],["power_w","Вт: "],["progress_pct","Прогресс: "],["status",""]];
    for (const [field,label] of fields) if (data[field] !== undefined && data[field] !== null) chips.append(element("span","node-chip",label + clean(data[field])));
    if (!chips.children.length) chips.append(element("span","node-chip",raw === null ? "Нет показаний" : `${Object.keys(data).length} параметров`));
    card.append(chips,element("pre","",JSON.stringify(raw,null,2)));
    const tile=element("article","node-tile");
    tile.append(card);
    const available=state.control_specs?.[id] || [];
    if(available.length) {
      const control=element("button","button control-open","Управление и запросы ↗");
      control.type="button"; control.dataset.node=id;
      control.setAttribute("aria-label","Управление: "+(catalog.get(id)?.title || id));
      control.addEventListener("click",()=>window.openDeviceControls?.(id));
      tile.append(control);
    }
    container.append(tile);
  }
  $("empty-state").hidden = nodes.length > 0;
  if (query && !nodes.length) { $("empty-title").textContent = "Ничего не найдено"; $("empty-description").textContent = "Попробуйте другое название или идентификатор устройства."; }
  else if(state.status === "error" || state.status === "reconnecting") { $("empty-title").textContent = "Сигнал пока не получен"; $("empty-description").textContent = state.error || "Проверьте этап подключения и журнал ниже."; }
  else { $("empty-title").textContent = "Ожидание сигнала"; $("empty-description").textContent = "Подключитесь к куполу, чтобы увидеть показания датчиков и состояние систем."; }
}
function renderJournal() {
  const events = JSON.stringify(state.events);
  if(events !== lastEvents) {
    const container = $("events"), wasBottom = container.scrollTop + container.clientHeight >= container.scrollHeight - 30;
    container.replaceChildren();
    if(!state.events.length) container.append(element("div","journal-empty","События подключения появятся здесь."));
    for(const entry of state.events) {
      const row = element("div",`event-row ${entry.level}`);
      row.append(element("time","event-time",timeText(entry.time)),element("span","event-dot"),element("span","event-text",entry.text));
      container.append(row);
    }
    if(wasBottom) container.scrollTop = container.scrollHeight;
    lastEvents = events;
  }
  const messages = JSON.stringify(state.messages);
  if(messages !== lastMessages) {
    const container = $("messages"); container.replaceChildren();
    if(!state.messages.length) container.append(element("div","journal-empty","Входящих сообщений пока нет."));
    for(const msg of state.messages) {
      const row = element("article","chat-row");
      row.append(element("div","chat-meta",`${timeText(msg.received_at)} · ${msg.sender?.name || msg.sender?.role || "Купол"}`),element("div","chat-text",clean(msg.text)));
      container.append(row);
    }
    lastMessages = messages;
  }
}
function render() {
  const nodes = Object.values(state.nodes || {});
  if(initial) { $("url").value = state.url || ""; initial = false; }
  $("status-badge").className = `status-badge ${state.status}`;
  $("status-badge").lastElementChild.textContent = statusNames[state.status] || state.status;
  $("node-count").textContent = state.last_telemetry_at ? nodes.length : "—";
  $("device-badge").textContent = nodes.length;
  $("online-count").textContent = state.last_telemetry_at ? `${nodes.filter(x=>x?.online === true).length} на связи` : "нет данных";
  $("ping-value").textContent = state.ping_ms === null ? "—" : state.ping_ms;
  $("packet-count").textContent = `${state.telemetry_count} сообщ.`;
  $("role-value").textContent = roles[state.role] || "—";
  $("team-value").textContent = state.team ? `№ ${state.team}` : state.role === "guest" ? "доступен общий чат" : "ожидание авторизации";
  $("key-label").textContent = state.key_configured ? "Ключ настроен" : "Ключ не задан";
  $("key-dot").className = `tiny-dot ${state.key_configured ? "ready" : ""}`;
  $("connection-error").hidden = !state.error;
  $("connection-error").textContent = state.error || "";
  const diagnosticContainer = $("diagnostic-stages");
  diagnosticContainer.replaceChildren();
  for(const [id,title] of [["tcp","TCP / TLS"],["websocket","WebSocket"],["auth","Авторизация"],["api","API: ping и каталог"],["telemetry","Телеметрия"]]) {
    const diagnostic = state.diagnostics?.[id];
    const status = diagnostic?.state || "waiting";
    const row = element("div",`diagnostic-stage ${status}`);
    row.title = diagnostic?.detail || "Ожидает своей очереди";
    const label = {waiting:"—",running:"…",ok:"OK",error:"Ошибка",skipped:"Гость"}[status] || status;
    row.append(element("span","",title),element("b","",label));
    diagnosticContainer.append(row);
  }
  $("connection-mode").textContent = state.status === "connecting" || state.status === "reconnecting" ? `попытка ${state.attempts}` : state.status === "connected" ? "активно" : state.status === "checked" ? "проверено" : state.status === "error" ? "ошибка" : "ожидает запуска";
  $("route-value").textContent = state.proxy || "Напрямую";
  $("access-mode").textContent = state.status==="connected" && state.role==="team" ? "Управление доступно" : "Наблюдение";
  const hour = state.game_time?.hour;
  $("game-time").textContent = Number.isFinite(hour) ? `${String(Math.floor(hour)).padStart(2,"0")}:${String(Math.floor(hour%1*60)).padStart(2,"0")}` : "—";
  updateButtons(); freshness(); renderNodes(); renderJournal();
  window.renderControlState?.();
}
async function poll() {
  try {
    const response = await fetch("/api/state",{cache:"no-store"});
    if(!response.ok) throw new Error("Панель недоступна");
    state = await response.json();
    if(panelLost) { $("form-message").textContent = ""; panelLost = false; }
    render();
  } catch(error) { panelLost = true; $("form-message").textContent = "Нет связи с локальной панелью. Запустите dome_web.py и обновите страницу."; }
  setTimeout(poll,1000);
}
async function command(action) {
  let url = $("url").value.trim();
  if(action !== "disconnect" && !/^(wss?|https?):\/\//i.test(url)) { $("form-message").textContent = "Укажите адрес купола: ws://хост:порт или http://хост:порт."; return; }
  submitting = true; updateButtons(); $("form-message").textContent = "";
  try {
    const response = await fetch("/api/connection",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,url})});
    const result = await response.json();
    if(!response.ok) throw new Error(result.error || "Запрос не выполнен");
    state = await (await fetch("/api/state",{cache:"no-store"})).json();
    nodesSignature = ""; render();
  } catch(error) { $("form-message").textContent = error.message; }
  finally { submitting = false; updateButtons(); }
}
$("connection-form").addEventListener("submit",event=>{event.preventDefault();command("connect");});
$("check-button").addEventListener("click",()=>command("check"));
$("disconnect-button").addEventListener("click",()=>command("disconnect"));
$("search").addEventListener("input",renderNodes);
for(const [tab,panel] of [["events-tab","events"],["chat-tab","messages"],["actions-tab","command-log"]]) {
  $(tab).addEventListener("click",()=>{
    for(const [other,target] of [["events-tab","events"],["chat-tab","messages"],["actions-tab","command-log"]]) {
      $(other).classList.toggle("active",other === tab); $(other).setAttribute("aria-selected",String(other === tab)); $(target).hidden = target !== panel;
    }
  });
}
setInterval(freshness,1000);
poll();
