"use strict";
let controlNode = null, selectedCommand = null, controlSending = false;
let actionToken = null, tokenPayload = null, lastControlResult = "", lastCommandLog = "";
const controlStatuses = {sending:"Отправляется",verifying:"Проверяется",verified:"Подтверждено",done:"Ответ получен",accepted:"Принято",unknown:"Не подтверждено",error:"Ошибка"};

function currentSpec() {
  return (state?.control_specs?.[controlNode] || []).find(spec=>`${spec.type}:${spec.action}` === $("control-action").value);
}
function nodeFileNames() {
  const files=state?.nodes?.[controlNode]?.files || [];
  return (Array.isArray(files) ? files : Object.keys(files)).map(file=>typeof file==="string" ? file : file?.name || file?.file_name).filter(Boolean);
}
function showControlError(message) {
  $("control-error").hidden = !message;
  $("control-error").textContent = message || "";
}
function renderFields() {
  const spec=currentSpec();
  $("control-fields").replaceChildren();
  $("control-hint").textContent=spec?.hint || "";
  showControlError("");
  if(!spec) { updateControlPreview(); return; }
  for(const descriptor of spec.fields) {
    const wrap=element("div","control-field");
    const label=element("label","",descriptor.label+(descriptor.optional ? " (необязательно)" : ""));
    const id="parameter-"+descriptor.name; label.htmlFor=id;
    let input;
    const files=descriptor.type==="file_name" ? nodeFileNames() : [];
    if(descriptor.type==="select" || files.length) {
      input=element("select");
      const empty=element("option","","Выберите…"); empty.value=""; input.append(empty);
      const choices=descriptor.type==="select" ? descriptor.choices : files;
      for(const value of choices) {
        const option=element("option","",value===true ? "Да / включено" : value===false ? "Нет / выключено" : String(value));
        option.value=descriptor.type==="select" ? JSON.stringify(value) : value;
        input.append(option);
      }
    } else if(["textarea","json"].includes(descriptor.type)) {
      input=element("textarea"); input.rows=descriptor.type==="json" ? 4 : 3;
      if(descriptor.type==="json") input.placeholder='{"параметр": "значение"}';
    } else {
      input=element("input");
      input.type=descriptor.type==="number" ? "number" : descriptor.type==="file_text" ? "file" : "text";
      if(descriptor.type==="number") {
        if(descriptor.min!==null && descriptor.min!==undefined) input.min=descriptor.min;
        if(descriptor.max!==null && descriptor.max!==undefined) input.max=descriptor.max;
        input.step=descriptor.integer ? "1" : "any";
      }
      if(descriptor.type==="file_text") input.accept=".gcode,.gc,.gco,.txt";
    }
    input.id=id; input.dataset.parameter=descriptor.name;
    input.required=!descriptor.optional;
    if(descriptor.max_length) input.maxLength=descriptor.max_length;
    if(descriptor.default!==undefined) input.value=descriptor.type==="select" ? JSON.stringify(descriptor.default) : descriptor.default;
    input.addEventListener("input",updateControlPreview);
    input.addEventListener("change",()=>{
      if(descriptor.type==="file_text" && input.files[0] && $("parameter-file_name")) $("parameter-file_name").value=input.files[0].name;
      updateControlPreview();
    });
    wrap.append(label,input); $("control-fields").append(wrap);
  }
  updateControlPreview();
  window.renderControlState();
}

async function parameterValue(spec, preview=false) {
  if(spec.mode==="none") return null;
  const result={};
  for(const f of spec.fields) {
    const input=$("parameter-"+f.name);
    if(f.type==="file_text") {
      const file=input.files[0];
      if(!file) { if(preview) continue; throw new Error("Выберите G-code файл."); }
      if(file.size>16*1024*1024) throw new Error("Файл больше 16 МиБ.");
      result[f.name]=preview ? `[${file.name}; ${file.size} байт]` : await file.text();
      continue;
    }
    const text=input.value.trim();
    if(!text) { if(f.optional || preview) continue; throw new Error(`Заполните «${f.label}».`); }
    if(f.type==="number") {
      const value=Number(text);
      if(!Number.isFinite(value)) throw new Error(`«${f.label}»: нужно число.`);
      result[f.name]=value;
    } else if(f.type==="select" || f.type==="json") {
      try { result[f.name]=JSON.parse(text); } catch { throw new Error(`«${f.label}»: неверный JSON.`); }
    } else result[f.name]=input.value;
  }
  return spec.mode==="scalar" || spec.mode==="json" ? result.value ?? null : result;
}
function requestPreview(spec,value) {
  if(spec.type==="query") return {type:"query",node_id:controlNode,query:spec.action,params:value || {}};
  const request={type:"control",node_id:controlNode,action:spec.action};
  if(value!==null) request.value=value;
  return request;
}
async function updateControlPreview() {
  const spec=currentSpec();
  if(!spec) { $("control-preview").textContent="Выберите команду."; $("control-send").disabled=true; return; }
  try { $("control-preview").textContent=JSON.stringify(requestPreview(spec,await parameterValue(spec,true)),null,2); }
  catch(error) { $("control-preview").textContent=error.message; }
}

window.openDeviceControls = function(nodeId) {
  controlNode=nodeId; selectedCommand=null; lastControlResult=""; actionToken=null; tokenPayload=null;
  const catalog=(state.catalog || []).find(node=>node.node_id===nodeId) || {};
  $("controls-title").textContent=catalog.title || nodeId;
  $("controls-node-id").textContent=nodeId;
  $("control-action").replaceChildren();
  const placeholder=element("option","","Выберите действие или запрос…"); placeholder.value="";
  $("control-action").append(placeholder);
  for(const [kind,title] of [["query","Запросы данных"],["control","Управление устройством"]]) {
    const specs=(state.control_specs?.[nodeId] || []).filter(spec=>spec.type===kind);
    if(!specs.length) continue;
    const group=element("optgroup"); group.label=title;
    for(const spec of specs) {
      const option=element("option","",`${spec.label} · ${spec.action}`);
      option.value=`${spec.type}:${spec.action}`; group.append(option);
    }
    $("control-action").append(group);
  }
  const physical=catalog.operator_controls || [];
  $("operator-actions").hidden=!physical.length;
  $("operator-list").textContent=physical.length ? "Эти действия выполняются человеком: "+physical.join(", ")+". Они не отправляются как команды API." : "";
  $("control-result").textContent="Команды ещё не отправлялись.";
  renderFields();
  $("controls-dialog").showModal();
  window.renderControlState();
};

function commandResult(record) {
  const wrapper=element("div","command-result-body");
  wrapper.append(element("div","chat-meta",`${record.label} · ${record.action} · ${timeText(record.time)}`));
  wrapper.append(element("span",`command-status ${record.status}`,controlStatuses[record.status] || record.status),element("p","",record.detail));
  if(record.expected) wrapper.append(element("p","command-expect",`Ожидается ${record.expected.path.join(".")} = ${clean(record.expected.target)}; получено ${clean(record.observed)}`));
  if(record.result!==undefined) wrapper.append(element("pre","query-result",JSON.stringify(record.result,null,2)));
  return wrapper;
}
window.renderControlState = function() {
  if(!state) return;
  const records=state.commands || [];
  const serialized=JSON.stringify(records);
  if(serialized!==lastCommandLog) {
    $("command-log").replaceChildren();
    if(!records.length) $("command-log").append(element("div","journal-empty","Выполненные команды и запросы появятся здесь."));
    for(const record of [...records].reverse()) {
      const row=element("article","command-log-row");
      row.append(element("div","chat-meta",`${timeText(record.time)} · ${record.node_id} · ${record.action}`),commandResult(record));
      $("command-log").append(row);
    }
    lastCommandLog=serialized;
  }
  if(!controlNode || !$("controls-dialog").open) return;
  const node=state.nodes?.[controlNode] || {};
  $("controls-live").textContent=`Связь: ${node.online===true ? "есть" : node.online===false ? "нет" : "—"} · Состояние: ${clean(node.state || node.status)} · Роль: ${roles[state.role] || "—"}`;
  const spec=currentSpec();
  const busy=records.some(record=>["sending","verifying"].includes(record.status));
  const fresh=state.last_telemetry_at && Date.now()-new Date(state.last_telemetry_at).getTime()<10000;
  const connected=state.status==="connected" && state.role==="team" && fresh;
  $("control-send").disabled=controlSending || busy || !connected || !spec ||
    (spec.type==="control" && spec.action!=="reconnect" && (node.online===false || node.status==="UNKNOWN"));
  $("control-send").textContent=controlSending ? "Отправляется…" : busy ? "Проверяется предыдущая команда…" : spec?.type==="query" ? "Выполнить запрос" : "Выполнить команду";
  if(!connected) $("controls-live").textContent+=" · Управление недоступно: нет активной команды или свежих данных.";
  const record=records.find(item=>item.id===selectedCommand);
  if(record && JSON.stringify(record)!==lastControlResult) {
    $("control-result").replaceChildren(commandResult(record));
    lastControlResult=JSON.stringify(record);
    if(!["sending","verifying"].includes(record.status)) { actionToken=null; tokenPayload=null; }
  }
};

$("control-action").addEventListener("change",renderFields);
$("controls-close").addEventListener("click",()=>$("controls-dialog").close());
$("device-control-form").addEventListener("submit",async event=>{
  event.preventDefault();
  const spec=currentSpec();
  if(!spec || controlSending) return;
  controlSending=true; showControlError(""); window.renderControlState();
  try {
    const value=await parameterValue(spec);
    const payload={node_id:controlNode,action:spec.action,type:spec.type,value};
    const fingerprint=JSON.stringify(payload);
    if(!actionToken || tokenPayload!==fingerprint) { actionToken=crypto.randomUUID(); tokenPayload=fingerprint; }
    const response=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...payload,token:actionToken})});
    const result=await response.json();
    if(!response.ok) { actionToken=null; throw new Error(result.error || "Команда не принята."); }
    selectedCommand=result.command_id;
    state=await (await fetch("/api/state",{cache:"no-store"})).json();
    render();
  } catch(error) { showControlError(error.message); }
  finally { controlSending=false; window.renderControlState(); }
});
