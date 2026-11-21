"use strict";
const el = id => document.getElementById(id);
const base = location.pathname;
const data = document.documentElement.dataset;
let active = null, timer = 0, generation = 0, failures = 0, scale = 1, live = true;
const pinned = new Set();
function clearLesson() {
  active = null; el("lesson").hidden = true;
  el("page").hidden = true; el("material").removeAttribute("src");
  for (const id of ["course","section","goal","location","cite","delivery"]) el(id).textContent = "";
  el("practice").replaceChildren(); el("practice").hidden=true;
  el("cards").replaceChildren(); el("source").open = false; pinned.clear();
  for (const key of Object.keys(data)) delete data[key];
}
async function read(path, options) {
  const r = await fetch(base + "/" + path, {cache:"no-store", ...options});
  if (!r.ok) throw Error(String(r.status));
  return r.json();
}
function draw(s) {
  const boundary = !active || s.lesson_id !== active.lesson_id || s.access_epoch !== active.access_epoch;
  if (boundary) clearLesson();
  active = s;
  el("course").textContent = s.course; el("section").textContent = s.section;
  el("goal").textContent = s.goal;
  drawPractice(s);
  const practicing = !!(s.micro || s.reference);
  el("source").hidden = practicing || (!s.source && s.stage!=="standby");
  el("coaching-material").hidden = practicing;
  const waiting = s.stage === "standby";
  el("help").parentElement.hidden = waiting || practicing;
  el("source").querySelector("summary").textContent = waiting ? "资料目录" : "查看资料出处";
  if (waiting) {
    el("location").textContent = s.inventory.length + " 份资料 · " + s.inventory.reduce((n,r)=>n+r.pages,0) + " 页 · PDF 模式";
    el("cite").textContent = s.inventory.map(i => i.name + " · " + i.pages + " 页").join("\n");
  } else if (s.source) {
    const pos = s.video ? (s.video.start_seconds == null ? "本节视频" : "视频 " + s.video.start_seconds + " 秒") : "PDF 跟课";
    el("location").textContent = pos + " · " + s.source.source_id + (s.source.pdf_page ? " · 物理第 "+s.source.pdf_page+" 页" : "");
    el("cite").textContent = s.source.filename + (s.source.pdf_page ? "\n物理第 "+s.source.pdf_page+" 页" : "")+"\nSHA-256 "+s.source.sha256;
  }
  el("page").hidden = !s.page_image;
  if (s.page_image) {
    const src = base + "/page/" + s.page_image + "?epoch=" + encodeURIComponent(s.access_epoch);
    el("material").alt = s.source.filename + "，物理第 " + s.source.pdf_page + " 页";
    if (el("material").getAttribute("src") !== src) el("material").src = src;
  } else el("material").removeAttribute("src");
  const list = el("cards");
  const old = new Map([...list.children].map(n => [n.dataset.id,n]));
  const unpinned = s.cards.filter(c=>!pinned.has(c.id));
  const ordered = [...s.cards.filter(c=>pinned.has(c.id)), ...unpinned.filter(c=>c.figure), ...unpinned.filter(c=>!c.figure)];
  ordered.forEach((card, index) => {
    let n = old.get(card.id);
    if (!n) {
      n = document.createElement("section"); n.dataset.id = card.id;
      const header = document.createElement("div"), h = document.createElement("h2"), b = document.createElement("button");
      b.type = "button";
      b.onclick = () => {
        if (pinned.has(card.id)) pinned.delete(card.id); else pinned.add(card.id);
        applyPin(n, b, card.id);
        if (active) draw(active);
      };
      header.append(h,b); n.append(header,document.createElement("p"));
    }
    n.querySelector("h2").textContent = card.title;
    const p = n.querySelector(":scope > p"); if (p.textContent !== card.body) p.textContent = card.body;
    let frame = n.querySelector("iframe");
    if (card.figure) {
      if (!frame) {
        frame = document.createElement("iframe");
        frame.setAttribute("sandbox","allow-scripts");
        p.before(frame);
      }
      frame.title = card.title;
      const src = base + "/figure/" + card.figure + "?epoch=" + encodeURIComponent(s.access_epoch);
      if (frame.getAttribute("src") !== src) frame.src = src;
    } else if (frame) frame.remove();
    let flow=n.querySelector(".correction-flow");
    if (card.corrections) {
      if (!flow) {
        flow=document.createElement("ol");
        flow.className="correction-flow";
        flow.setAttribute("aria-label","本轮逐项纠错");
      }
      flow.replaceChildren(...card.corrections.map(row=>{
        const item=document.createElement("li");
        const label=document.createElement("strong"), pair=document.createElement("div");
        const before=document.createElement("span"), arrow=document.createElement("span"), after=document.createElement("b");
        const reason=document.createElement("small");
        label.textContent=row.label; before.textContent=row.original; after.textContent=row.replacement;
        arrow.textContent="→"; arrow.setAttribute("aria-label","改为"); reason.textContent=row.reason;
        pair.append(before,arrow,after); item.append(label,pair,reason); return item;
      }));
      p.after(flow);
      if (frame) flow.after(frame);
    } else if (flow) flow.remove();
    let link=n.querySelector("a");
    if (card.href && /^https:\/\/www\.bilibili\.com\/video\/BV[a-zA-Z0-9]+\/(?:\?(?:p=\d+|t=\d+)(?:&t=\d+)?)?$/.test(card.href)) {
      if (!link) {link=document.createElement("a");link.target="_blank";link.rel="noopener noreferrer";n.append(link);}
      link.href=card.href;link.textContent=card.link_label;
    } else if (link) link.remove();
    applyPin(n,n.querySelector("button"),card.id);
    n.querySelector("button").hidden=ordered.length===1;
    if (list.children[index] !== n) list.insertBefore(n,list.children[index] || null);
    old.delete(card.id);
  });
  for (const [id,n] of old) { pinned.delete(id); n.remove(); }
  el("lesson").hidden = false; el("help").disabled = false;
  el("status").textContent = s.stage_label + " · 资料版本 " + s.revision;
  Object.assign(data,{revision:String(s.revision),entityKey:s.entity_key,sourceVersion:s.source_version,
    attemptId:s.attempt_id,accessEpoch:s.access_epoch,contentHash:s.content_hash,rendererVersion:s.micro||s.reference?"english-writing/2":"english-writing/1",readerTemplate:document.querySelector('meta[name="writing-release"]').content});
}
function drawPractice(s) {
  const panel = el("practice"), m = s.micro;
  if (s.reference) {
    panel.hidden=false;panel.replaceChildren();
    const p=document.createElement("p");p.className="draft-reference";panel.append(p);
    const d=s.reference.draft, a=s.reference.anchor;
    p.textContent=d?.text || "提交后在这里对照原稿。";
    if (d && a?.draft_id===d.id && a.draft_sha256===d.sha256) {
      const chars=Array.from(d.text);
      if (chars.slice(a.offset,a.offset+a.length).join("")===a.quote) {
        const mark=document.createElement("mark");mark.textContent=a.quote;
        p.replaceChildren(document.createTextNode(chars.slice(0,a.offset).join("")),mark,document.createTextNode(chars.slice(a.offset+a.length).join("")));
        mark.scrollIntoView({block:"nearest"});
      }
    }
    return;
  }
  panel.hidden = !m;
  if (!m) { panel.replaceChildren(); return; }
  const previous = panel.querySelector("textarea");
  const retained = previous && panel.dataset.attempt === s.attempt_id &&
    previous.value !== previous.defaultValue ? previous.value : null;
  const hadFocus = previous === document.activeElement;
  panel.dataset.attempt = s.attempt_id;
  panel.replaceChildren();
  function append(parent, tag, value) {
    const node = document.createElement(tag);
    node.textContent = value;
    parent.append(node);
    return node;
  }
  const g=m.guided;
  if (g) {
    append(panel,"p",({"needs_practice":"本点待练","ready_for_next":"本轮过关","paused":"已暂停","skipped_by_user":"已跳过"})[g.status]);
    if (g.feedback) append(panel,"p",g.feedback);
  }
  const message = append(panel,"p",m.prompt);
  message.className = "practice-prompt";
  if (m.goal) append(panel,"p",m.goal);
  if (m.source) {
    const link = append(panel,"a","回到原文 " + m.source.unit_id);
    link.href = m.source.href; link.target = "_blank"; link.rel = "noopener";
  } else if (m.source_status) append(panel,"p",m.source_status);
  if (m.phase === "skipped") { append(panel,"p","已跳过本次短写"); return; }
  const drafts = m.drafts, latest = drafts.at(-1), fb = m.feedback;
  const columns = append(panel,"div",""); columns.className = "practice-columns";
  const original = append(columns,"section",""), edit = append(columns,"section","");
  let select, excerpt;
  function showDraft(id, locate = false) {
    const d = drafts.find(row => row.id === id);
    if (!d) return;
    excerpt.replaceChildren();
    const a = fb?.anchor;
    if (a && a.draft_id === d.id && a.draft_sha256 === d.sha256) {
      const chars = Array.from(d.text);
      const quote = chars.slice(a.offset,a.offset+a.length).join("");
      if (quote === a.quote) {
        excerpt.append(document.createTextNode(chars.slice(0,a.offset).join("")));
        const mark = append(excerpt,"mark",quote);
        excerpt.append(document.createTextNode(chars.slice(a.offset+a.length).join("")));
        if (locate) { mark.tabIndex=-1; mark.focus(); mark.scrollIntoView({block:"nearest"}); }
        return;
      }
    }
    excerpt.textContent=d.text;
    if (locate) status.textContent="原句定位已失效";
  }
  if (latest) {
    const label=append(original,"label","稿件版本");
    select=append(label,"select","");
    drafts.forEach((d,i)=>{ const o=append(select,"option",i===0?"首稿":"修订 "+i);o.value=d.id; });
    select.value=m.selected_draft || fb?.anchor.draft_id || drafts[0].id;
    excerpt=append(original,"p",""); excerpt.className="draft-reference";
    select.onchange=()=>{showDraft(select.value);act("select_draft",select.value);};
    showDraft(select.value);
  }
  const status=append(edit,"p",""); status.setAttribute("role","status");
  function control(text,fn) { const b=append(edit,"button",text);b.type="button";b.onclick=fn;return b; }
  if (fb) {
    append(edit,"p",fb.direction);
    control("定位原句",()=>{select.value=fb.anchor.draft_id;showDraft(select.value,true);act("locate",select.value);});
  }
  const label=append(edit,"label",latest?"我来改":"我的短写");
  const input=append(label,"textarea","");
  input.defaultValue=retained ?? (m.unsubmitted || latest?.text || "");input.value=input.defaultValue;
  if (hadFocus) input.focus({preventScroll:true});
  let busy=false;
  async function act(kind, draftId) {
    if (busy) return;
    busy=true;
    const payload={attempt_id:s.attempt_id,revision:s.revision,action_id:crypto.randomUUID(),kind};
    if (draftId && kind!=="point_control") payload.draft_id=draftId;
    if (kind==="point_control") { payload.control=draftId; if(draftId==="explain")payload.text=input.value; }
    if (g && ["question_seen","feedback_seen"].includes(kind) && g.outbound_id) payload.feedback_id=g.outbound_id;
    if (g && ["question_seen","submit"].includes(kind)) {
      payload.question_id=g.question.id;payload.question_sha256=g.question.sha256;
    }
    if (["submit","accept"].includes(kind)) payload.base_draft=latest?.id??null;
    if (["save_draft","submit","help"].includes(kind)) payload.text=input.value;
    try {
      await read("micro-actions",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
      input.defaultValue=input.value; status.textContent="已保存";
      const next=await read("projection");
      if (live && active?.attempt_id===s.attempt_id && next.attempt_id===s.attempt_id) draw(next);
    } catch (_) { status.textContent="未保存，请核对当前版本后重试"; }
    finally { busy=false; }
  }
  if (g) {
    label.firstChild.textContent="本题回答";
    input.defaultValue=retained ?? (m.unsubmitted || "");input.value=input.defaultValue;
    const answering=g.enabled && g.status==="needs_practice" && !g.pending_response;
    input.disabled=!answering;
    if (!g.enabled) { append(edit,"p","逐点教学已关闭，原稿可回看。");return; }
    if (answering) {
      control("提交回答",()=>act("submit")).disabled=g.question.status!=="sent";
      if (g.question.status==="planned") requestAnimationFrame(()=>{
        if (live && active===s && document.visibilityState==="visible" && message.getClientRects().length) act("question_seen");
      });
      control("请解释",()=>act("point_control","explain"));
      control("暂停",()=>act("point_control","pause"));control("跳过本点",()=>act("point_control","skip"));
    } else if (g.status==="paused" || g.status==="skipped_by_user") control("继续本点",()=>act("point_control","resume"));
    if (g.outbound_id && !g.feedback_seen && (g.question.status!=="planned" || !answering)) requestAnimationFrame(()=>{
      if (live && active===s && document.visibilityState==="visible" && panel.getClientRects().length) act("feedback_seen");
    });
    if (g.pending_response) append(edit,"p","已提交，待本轮教练反馈");
    return;
  }
  control(latest?"提交修订":"提交首稿",()=>act("submit"));control("保存草稿",()=>act("save_draft"));
  if (!fb) control("请给一个提示",()=>act("help"));
  if (!latest) control("跳过",()=>act("skip"));
  if (fb && fb.decision==="pending") {
    if (fb.has_example) control("看一个例子",()=>act("example"));
    if (fb.has_rewrite) control("查看建议",()=>act("rewrite"));
    if (m.exposed.some(e=>e.kind==="rewrite")) control("采纳这处建议",()=>act("accept"));
    control("暂不采用",()=>act("decline"));
  } else if (fb) append(edit,"p",({"accepted":"已采纳这处建议","declined":"已暂缓","self_revised":"已自行提交修订"})[fb.decision]);
  for (const e of m.exposed) append(edit,"p",e.text);
  const review=m.reviews.filter(r=>r.draft_id===latest?.id).at(-1);
  if (review) {
    append(edit,"p","本次目标 · "+({met:"已达到",not_met:"仍需修改",unknown:"待核",not_applicable:"不适用"})[review.result]);
    review.findings.forEach(v=>append(edit,"p",v));review.language.forEach(v=>append(edit,"p","语言 · "+v));
  }
}
function applyPin(n,b,id) {
  const on = pinned.has(id); n.dataset.pinned = on ? "yes":"no";
  b.setAttribute("aria-pressed",String(on)); b.textContent = on ? "已固定":"固定";
}
window.addEventListener("message", event => {
  const msg = event.data;
  if (event.origin !== "null" || msg?.type !== "english-figure-height" ||
      !Number.isSafeInteger(msg.height) || msg.height < 24 || msg.height > 24000) return;
  const frame = [...el("cards").querySelectorAll("iframe")].find(f=>f.contentWindow===event.source);
  if (frame) frame.style.height = msg.height + "px";
});
async function refresh() {
  const g = generation;
  try {
    const r = await read("revision");
    if (generation !== g) return;
    if (!active || r.content_hash !== active.content_hash) {
      if (active && r.access_epoch !== active.access_epoch) clearLesson();
      const s = await read("projection");
      if (generation !== g) return;
      draw(s);
    }
    if (active.micro || active.reference || active.stage === "standby") { failures = 0; return; }
    const a = await read("actions");
    if (generation !== g || !active || a.access_epoch !== active.access_epoch || a.payload_revision !== active.revision) return;
    const last = a.actions.at(-1);
    el("delivery").textContent = last ? (last.status==="coach_read" ? "教练已读" : "已发送，待教练读取") : "";
    if (failures) el("status").textContent = active.stage_label + " · 资料版本 " + active.revision;
    failures = 0;
  } catch (_) {
    failures++; clearLesson(); el("status").textContent = "资料暂不可用，正在重新连接";
  } finally {
    if (live && generation === g) timer = setTimeout(refresh, failures ? 2500 : 800);
  }
}
el("help").onclick = async () => {
  const s = active; if (!s) return;
  const action = {session_id:s.session_id,lesson_id:s.lesson_id,payload_revision:s.revision,access_epoch:s.access_epoch,
    action_id:crypto.randomUUID(),kind:"request_explanation"};
  try {
    const r = await read("actions",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(action)});
    if (active === s) el("delivery").textContent = r.status === "coach_read" ? "教练已读" : "已发送，待教练读取";
  } catch (_) { if (active === s) el("delivery").textContent="本节已更新，请重试"; }
};
for (const [id, step] of [["smaller",-0.1],["larger",0.1]]) el(id).onclick = () => {
  scale = Math.min(1.65,Math.max(.85,scale+step)); document.documentElement.style.setProperty("--scale",scale);
};
window.addEventListener("pagehide",() => {live=false;generation++;clearTimeout(timer);clearLesson();});
window.addEventListener("pageshow",() => {live=true;generation++;clearTimeout(timer);refresh();});
