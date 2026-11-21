"use strict";
// Only structural offsets survive a pause. Reuse them only for the same verified content hash.
const selectionKey="reader-selection/"+location.pathname;
function rememberSelection(){
  const hash=document.documentElement.dataset.contentHash, sel=window.getSelection();
  if(!hash)return;
  try{
    if(!sel || sel.isCollapsed || !sel.rangeCount){sessionStorage.removeItem(selectionKey);return;}
    function point(node,offset){
      const path=[];
      while(node && !(node.nodeType===1 && node.id)){
        const parent=node.parentNode;if(!parent)return null;
        path.unshift(Array.prototype.indexOf.call(parent.childNodes,node));node=parent;
      }
      return node && {id:node.id,path,offset};
    }
    const r=sel.getRangeAt(0),a=point(r.startContainer,r.startOffset),b=point(r.endContainer,r.endOffset);
    if(a && b)sessionStorage.setItem(selectionKey,JSON.stringify({hash,a,b}));
  }catch{}
}
function recoverSelection(hash){
  try{
    const raw=sessionStorage.getItem(selectionKey);if(!raw)return;
    sessionStorage.removeItem(selectionKey);
    const saved=JSON.parse(raw),sel=window.getSelection();if(saved.hash!==hash || !sel?.isCollapsed)return;
    function resolve(p){
      let node=document.getElementById(p.id);for(const i of p.path)node=node?.childNodes[i];return node;
    }
    const a=resolve(saved.a),b=resolve(saved.b);if(!a || !b)return;
    const range=document.createRange();range.setStart(a,saved.a.offset);range.setEnd(b,saved.b.offset);
    sel.removeAllRanges();sel.addRange(range);
  }catch{}
}

const by = id => document.getElementById(id);
const make = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
function setText(el,value){if(el.textContent!==value)el.textContent=value;}
const key = location.pathname.split("/").pop();
const base = "/english/a/" + encodeURIComponent(key);
const rows = new Map();
const memory = new Map();
let meta, active, selectedUnit, storageKey, ui, timer, aborter;
let generation = 0, painting = false, stream, busy = false, queued = false, announced = 0;
history.scrollRestoration = "manual";

function saveLayout() {
  if (!storageKey || !ui || painting) return;
  ui.y = Math.round(window.scrollY);
  if (selectedUnit) ui.notesY[selectedUnit] = Math.round(by("notes").scrollTop);
  try { localStorage.setItem(storageKey, JSON.stringify(ui)); } catch {}
}
function loadLayout(next) {
  storageKey = ["english-layout-v1", key, next.source_version, next.attempt_id, next.access_epoch].join("/");
  try { ui = JSON.parse(localStorage.getItem(storageKey)); } catch { ui = null; }
  ui ||= {};
  for (const field of ["open","tabs","tabRevisions","notesY"]) ui[field] ||= {};
  by("originals").open = !!ui.originalsOpen;
}
function clearPage(message) {
  rememberSelection();
  saveLayout();
  by("reader").hidden = true;
  for (const id of ["article","toc","focus","jump","identity","recap-list","words-list","review-words"]) by(id).replaceChildren();
  delete by("recap-list").dataset.signature; by("recap").hidden = true;
  delete by("words-list").dataset.signature; by("words").hidden = true; by("words").open = false;
  delete by("review-words").dataset.signature; by("word-review").hidden = true; by("originals").open = false;
  by("search").value = ""; by("focus-heading").textContent = "本句精读";
  rows.clear(); memory.clear(); selectedUnit = active = meta = storageKey = ui = null;
  document.title = "整篇精读 · 英语";
  delete document.documentElement.dataset.revision;
  delete document.documentElement.dataset.renderedRevision;
  for (const field of ["contentHash","entityKey","sourceVersion","attemptId","accessEpoch","rendererVersion"])
    delete document.documentElement.dataset[field];
  by("version").textContent = message;
}
function jump(id, showOriginal = false) {
  const row = rows.get(id);
  if (!row) return;
  ui.review = id === active ? null : id;
  markActive(active); saveLayout();
  if (showOriginal) by("originals").open = true;
  const target = showOriginal ? row.node : by("focus-heading");
  target.scrollIntoView({block:"nearest"});
  target.focus({preventScroll:true});
}
function createRow(item) {
  const {id} = item;
  const node = make("section"); node.id = id; node.tabIndex = -1;
  const container = by("article");
  const group = item.paragraph_id || "阅读题";
  if (!by("p-" + group)) {
    const heading = make("h2", "group", group); heading.id = "p-" + group;
    container.append(heading); by("toc").append(make("div","group",group));
  }
  const button = make("button","",id);
  button.type = "button"; button.onclick = () => jump(id); by("toc").append(button);
  const mark = make("button","mark",id); mark.type = "button";
  mark.onclick = () => jump(id);
  const original = make("p","original"); original.lang = "en";
  const tabs = make("div","tabbar"); tabs.hidden = true;
  const details = make("details");
  const summary = make("summary", "", "作答记录");
  const layers = make("div");
  details.append(summary, layers);
  summary.addEventListener("click", () => {
    if (ui) { ui.open[id] = !details.open; saveLayout(); }
  });
  node.append(mark, tabs, original, details);
  container.append(node);
  const notes = make("div","unit-notes"); notes.id = "notes-" + id; notes.hidden = true;
  const contextLine = make("p","note-original"); contextLine.lang = "en";
  const noteTabs = make("div","tabbar"); noteTabs.hidden = true;
  noteTabs.setAttribute("role","group"); noteTabs.setAttribute("aria-label","切换题干和选项讲解");
  const empty = make("p","empty");
  notes.append(noteTabs,contextLine,empty); by("focus").append(notes);
  const row = {node, button, mark, original, tabs, noteTabs, details, layers, notes, contextLine, empty, blocks:new Map()};
  rows.set(id, row);
  return row;
}
function blockNode(block, unit) {
  const node = make("div","layer");
  node.dataset.role = block.role;
  let label = block.label;
  if (block.figure) {
    node.classList.add("figure-block");
    const frame = make("iframe");
    frame.title = [unit,block.tab,label].filter(Boolean).join(" · ");
    frame.setAttribute("sandbox","allow-scripts");
    frame.dataset.src = base + "/figure/" + unit + "/" + block.figure + "?epoch=" + meta.access_epoch;
    frame.addEventListener("load", () => {
      if (frame.dataset.word) frame.contentWindow.postMessage({type:"english-focus-word",form:frame.dataset.word},"*");
    });
    node.append(frame);
    if (block.text) {
      const supplement = make("details","figure-note");
      supplement.append(make("summary","","讲解要点"),make("p","",block.text));
      node.append(supplement);
    }
    return node;
  }
  if (block.role === "first_attempt") {
    const note = {none:"独立",hint:"提示后",explanation:"讲解后",not_observed:"提示情况未核"};
    label += " · " + note[block.hint_dependence];
    if (block.transcribed) label += " · 语音转录文本，未核对音频";
  } else if (block.role === "revision" && block.reason === "prompted") label += " · 提示后";
  node.append(make("div","layer-label",label));
  if (block.text && (block.role === "explanation" || block.role === "structure")) {
    for (const paragraph of block.text.trim().split(/\n[ \t]*\n/)) {
      const line = make("p");
      for (const [index, part] of paragraph.split(/\*\*([^*\n]+)\*\*/).entries()) {
        line.append(index % 2 ? make("strong","",part) : part);
      }
      node.append(line);
    }
  } else if (block.text) node.append(make("p","",block.text));
  for (const choice of block.suggestions || []) {
    node.append(make("p","",choice.style + " · " + choice.text),
                make("p","",(choice.meaning_relation === "preserved" ? "保留的含义 · " : "变化的含义 · ") + choice.basis));
  }
  return node;
}
function loadVisibleFigures(row) {
  if (!row || row.notes.hidden) return;
  for (const frame of row.notes.querySelectorAll(".layer:not([hidden]) iframe[data-src]")) {
    frame.src = frame.dataset.src;
    delete frame.dataset.src;
  }
}
addEventListener("message", event => {
  const data = event.data;
  if (event.origin !== "null" || data?.type !== "english-figure-height"
      || !Number.isSafeInteger(data.height) || data.height < 24 || data.height > 24000) return;
  const frame = [...by("focus").querySelectorAll("iframe")].find(f => f.contentWindow === event.source);
  if (frame) frame.style.height = data.height + "px";
});
function switchTab(row, item, tab, persist) {
  row.original.textContent = item.tabs[tab];
  for (const bar of [row.tabs,row.noteTabs]) {
    for (const button of bar.children) button.setAttribute("aria-pressed", String(button.dataset.tab === tab));
  }
  for (const block of item.blocks) if (block.tab) row.blocks.get(block.id).node.hidden = block.tab !== tab;
  row.contextLine.textContent = item.tabs[tab];
  row.contextLine.hidden = item.blocks.some(b => b.role === "question_language" && b.tab === tab && b.figure);
  row.empty.hidden = item.blocks.some(b => b.actor !== "user" && (!b.tab || b.tab === tab));
  if (persist) {
    ui.tabs[item.id] = tab;
    ui.review = item.id === active ? null : item.id;
    markActive(active); saveLayout();
  }
}
function paintRow(item) {
  const row = rows.get(item.id) || createRow(item);
  memory.set(item.id, item); row.updatedAt = meta.revision;
  if (!item.tabs) row.original.textContent = item.text || "原文待开放";
  if (item.tabs && !row.tabs.childElementCount) {
    for (const bar of [row.tabs,row.noteTabs]) {
      bar.hidden = false;
      for (const tab of Object.keys(item.tabs)) {
        const btn = make("button","",tab === "STEM" ? "题干" : tab); btn.dataset.tab = tab;
        btn.onclick = () => switchTab(row, memory.get(item.id), tab, true);
        bar.append(btn);
      }
    }
  }
  const remaining = new Set();
  for (const block of item.blocks) {
    remaining.add(block.id);
    const sig = JSON.stringify(block);
    const old = row.blocks.get(block.id);
    if (old?.sig === sig) continue;
    const node = blockNode(block, item.id);
    if (old) old.node.replaceWith(node);
    else (block.actor === "user" ? row.layers : row.notes).append(node);
    row.blocks.set(block.id, {node,sig});
  }
  for (const [id,old] of row.blocks) if (!remaining.has(id)) {
    old.node.remove(); row.blocks.delete(id);
  }
  row.details.hidden = !row.layers.childElementCount;
  setText(row.contextLine,item.text || "");
  row.contextLine.hidden = item.blocks.some(b => b.role === "figure" && b.figure);
  row.empty.textContent = item.tabs ? "当前仅开放题面。" : (item.text ? "等你给出首译。" : "本句原文待开放。");
  row.empty.hidden = item.blocks.some(b => b.actor !== "user");
  if (item.tabs) {
    if (item.active_tab && ui.tabRevisions[item.id] !== item.tab_revision) {
      ui.tabs[item.id] = item.active_tab;
      ui.tabRevisions[item.id] = item.tab_revision;
    }
    switchTab(row, item, ui.tabs[item.id] in item.tabs ? ui.tabs[item.id] : "STEM", false);
  }
}
function markActive(id) {
  if (!memory.has(id)) return;
  const reading = memory.has(ui.review) ? ui.review : id;
  for (const [uid,row] of rows) {
    const current = uid === id, selected = uid === reading;
    row.node.classList.toggle("current", current);
    row.node.classList.toggle("selected", selected);
    row.button.setAttribute("aria-current", String(current));
    row.mark.setAttribute("aria-pressed", String(selected));
    setText(row.mark,current ? uid + " · 当前" : uid + " · 查看");
    row.notes.hidden = !selected;
    if (!row.details.hidden) row.details.open = ui.open[uid] ?? selected;
  }
  setText(by("focus-heading"),reading + (memory.get(reading).tabs ? " · 题目讲解" : " · 本句精读"));
  by("jump").textContent = reading === id ? "查看此处原文" : "回到当前 " + id;
  by("jump").onclick = () => jump(id, reading === id);
  if (reading !== selectedUnit) by("notes").scrollTop = ui.notesY[reading] || 0;
  active = id; selectedUnit = reading;
  loadVisibleFigures(rows.get(reading));
  paintWords();
  const prior = [...memory.values()].filter(v => v.id !== reading && !v.tabs && v.blocks.some(b => b.role === "explanation"));
  const list = by("recap-list");
  const recent = prior.slice(-3);
  const signature = JSON.stringify(recent.map(v => [v.id, rows.get(v.id).updatedAt]));
  if (list.dataset.signature !== signature) {
    list.replaceChildren();
    for (const v of recent) {
      const link = make("button","recap-link",v.id);
      link.onclick = () => jump(v.id);
      const point = make("div","recap-point");
      point.append(link,make("p","",v.blocks.findLast(b => b.role === "explanation").text));
      list.append(point);
    }
    list.dataset.signature = signature;
  }
  by("recap").hidden = !recent.length;
  filterDirectory();
}
function selectWord(word) {
  by("words").open = false;
  if (word.tab) switchTab(rows.get(word.id), memory.get(word.id), word.tab, true);
  jump(word.id);
  for (const frame of rows.get(word.id).notes.querySelectorAll(".layer:not([hidden]) iframe")) {
    frame.dataset.word = word.source_form;
    frame.contentWindow.postMessage({type:"english-focus-word",form:word.source_form},"*");
  }
}
function wordLocator(word) {
  return word.id + (word.tab ? " · " + (word.tab === "STEM" ? "题干" : word.tab) : "");
}
function paintWordReview(entries) {
  by("word-review").hidden = !entries.length;
  if (!entries.length) {by("review-words").replaceChildren(); delete by("review-words").dataset.signature; return;}
  const pageSize = wordReviewWide.matches ? 8 : wordReviewMedium.matches ? 5 : 3;
  const section = memory.get(selectedUnit);
  const tab = section?.tabs ? ui.tabs[selectedUnit] || section.active_tab || "STEM" : null;
  const context = selectedUnit + "/" + (tab || "");
  if (ui.reviewWordContext !== context) {
    ui.wordRotation = ui.reviewWordContext ? (ui.wordRotation || 0) + 1 : 0;
    ui.wordPage = 0;
    ui.reviewWordContext = context;
  }
  // A repeated surface may have a different sense here. Its chip keeps the original source.
  const tokens = text => " " + (text.toLowerCase().match(/[\p{L}\p{N}]+/gu) || []).join(" ") + " ";
  const sentence = tokens(section?.tabs?.[tab] || section?.text || "");
  const here = [], recurring = [], other = [];
  for (const word of entries) {
    if (word.id === selectedUnit && word.tab === tab) here.push(word);
    else if (sentence.includes(tokens(word.source_form))) recurring.push(word);
    else other.push(word);
  }
  const offset = (ui.wordRotation || 0) % (other.length || 1);
  const ordered = [...here,...recurring,...other.slice(offset),...other.slice(0,offset)];
  const pages = Math.ceil(ordered.length/pageSize);
  ui.wordPage = (ui.wordPage || 0) % pages;
  by("next-words").hidden = pages < 2;
  by("next-words").onclick = () => {ui.wordPage = (ui.wordPage+1)%pages; paintWordReview(entries); saveLayout();};
  const masked = !!ui.maskWordMeanings;
  by("mask-meanings").setAttribute("aria-pressed", String(masked));
  setText(by("mask-meanings"), masked ? "显示释义" : "遮住释义");
  const visible = ordered.slice(ui.wordPage * pageSize,(ui.wordPage+1)*pageSize);
  const list = by("review-words"), signature = JSON.stringify([visible,masked]);
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  list.replaceChildren();
  for (const word of visible) {
    const button = make("button","review-word");
    const form = make("strong","",word.source_form); form.lang = "en";
    button.append(form);
    if (!masked) button.append(make("span","",word.context_meaning));
    button.append(make("small","",wordLocator(word)));
    button.onclick = () => selectWord(word);
    list.append(button);
  }
}
by("mask-meanings").onclick = () => {
  ui.maskWordMeanings = !ui.maskWordMeanings; paintWords(); saveLayout();
};
const wordReviewWide = matchMedia("(min-width: 1400px)");
const wordReviewMedium = matchMedia("(min-width: 850px)");
for (const query of [wordReviewWide,wordReviewMedium]) {
  query.addEventListener("change", () => {if (ui && selectedUnit) paintWords();});
}
by("originals").ontoggle = () => {
  if (ui && !by("reader").hidden) {ui.originalsOpen = by("originals").open; saveLayout();}
};
function paintWords() {
  const entries = [...memory.values()].flatMap(item => (item.words || []).map(word => ({...word, id:item.id})));
  paintWordReview(entries);
  const list = by("words-list");
  const signature = JSON.stringify(entries);
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  list.replaceChildren();
  by("words").hidden = !entries.length;
  by("words").open = !!ui.wordsOpen;
  setText(by("words-label"), "本篇词语（" + entries.length + "）");
  for (const word of entries) {
    const item = make("div","word-entry");
    const link = make("button","recap-link",wordLocator(word));
    link.onclick = () => selectWord(word);
    const meaning = make("p");
    const form = make("button","word-form",word.source_form); form.lang = "en";
    form.onclick = () => selectWord(word);
    meaning.append(form, document.createTextNode("　" + word.context_meaning));
    item.append(link, meaning, make("p","word-reading","当时的读法　“" + word.user_translation + "”"));
    list.append(item);
  }
}
by("words").ontoggle = () => {
  if (ui && !by("words").hidden) {ui.wordsOpen = by("words").open; saveLayout();}
};
function filterDirectory() {
  const term = by("search").value.trim().toLocaleLowerCase();
  for (const [id, row] of rows) {
    const item = memory.get(id);
    const words = [id, item.text, ...Object.values(item.tabs || {}),
      ...(item.words || []).flatMap(w => [w.source_form,w.context_meaning,w.user_translation]), ...item.blocks.flatMap(b =>
      [b.label, b.text, ...(b.suggestions || []).flatMap(s => [s.text,s.basis])])];
    row.button.hidden = !!term && !words.filter(Boolean).join(" ").toLocaleLowerCase().includes(term);
  }
}
by("search").addEventListener("input",filterDirectory);
by("search").addEventListener("keydown",e => {
  if (e.key === "Enter") [...rows.values()].find(r => !r.button.hidden)?.button.click();
});
by("notes").addEventListener("scroll",saveLayout,{passive:true});

function wake(g) {
  if (g !== generation || document.hidden) return;
  clearTimeout(timer);
  if (busy) queued = true;
  else poll(g);
}
async function poll(g) {
  if (g !== generation || document.hidden) return;
  busy = true; queued = false;
  const controller = new AbortController(); aborter = controller;
  const timeout = setTimeout(() => controller.abort(), 2600);
  try {
    const query = meta ? "?since=" + meta.revision + "&epoch=" + meta.access_epoch : "";
    const response = await fetch(base + "/projection" + query, {cache:"no-store",signal:controller.signal});
    if (!response.ok) throw Error("scope");
    const next = await response.json();
    if (g !== generation || document.hidden) return;
    if (next.entity_key !== key) throw Error("entity changed");
    if (!meta || next.access_epoch !== meta.access_epoch) {
      const head = await fetch(base + "/revision", {cache:"no-store",signal:controller.signal});
      if (!head.ok) throw Error("revision unavailable");
      const latest = await head.json();
      if (g !== generation || document.hidden) return;
      if (["entity_key","source_version","attempt_id","access_epoch","revision","content_hash"].some(k => latest[k] !== next[k]))
        throw Error("scope changed");
    }
    if (next.page_version !== document.documentElement.dataset.pageVersion) {
      saveLayout(); location.reload(); return;
    }
    if (next.revision < announced) { queued = true; return; }
    if (!meta || next.revision !== meta.revision || next.content_hash !== meta.content_hash) {
      const reset = !meta || next.full || next.access_epoch !== meta.access_epoch;
      if (reset) { clearPage("正在更新"); loadLayout(next); }
      const y = reset ? Number(ui.y || 0) : window.scrollY;
      const focused = document.activeElement;
      const changedFocus = !reset && focused.closest("section")?.id;
      painting = true; meta = next;
      for (const item of next.sections) paintRow(item);
      markActive(next.active_unit_id);
      by("reader").hidden = false;
      setText(by("identity"),key);
      setText(by("title"),next.synthetic && key === "SYN-OUTDOOR-NOTES" ? "窗边的观察笔记 · 整篇原文与作答" : "整篇原文与作答");
      document.title = key + " · 英语精读";
      by("version").textContent = (next.synthetic ? "合成示例 · " : "") + "版本 " + next.revision;
      if (!focused.isConnected && changedFocus) rows.get(changedFocus)?.node.focus({preventScroll:true});
      window.scrollTo(0,y);
      if (reset && location.hash) {
        const [unit,version,sha,question]=location.hash.slice(1).split("~");
        if (version) {
          const row=memory.get(unit);
          const bytes=typeof row?.text==="string" ? await crypto.subtle.digest("SHA-256",new TextEncoder().encode(row.text)) : null;
          const digest=bytes && Array.from(new Uint8Array(bytes),b=>b.toString(16).padStart(2,"0")).join("");
          if (version===next.source_version && sha===digest && (!question || memory.has(question))) jump(unit,true);
          else by("version").textContent="原句版本或授权已变化";
        } else if (!y) jump(unit);
      }
      await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
      if (g !== generation || document.hidden) return;
      document.documentElement.dataset.revision = String(next.revision);
      document.documentElement.dataset.renderedRevision = String(next.revision);
      performance.clearMarks("english-revision-painted");
      performance.mark("english-revision-painted");
      Object.assign(document.documentElement.dataset, {contentHash:next.content_hash, entityKey:key,
        sourceVersion:next.source_version, attemptId:next.attempt_id, accessEpoch:next.access_epoch,
        rendererVersion:next.page_version});
      recoverSelection(next.content_hash);
      document.title = key + " · 英语精读 · v" + next.revision + " · " + next.content_hash.slice(0,16);
      painting = false;
    }
  } catch {
    if (g === generation) clearPage("当前阅读范围待核对，连接恢复后重新读取");
  } finally {
    clearTimeout(timeout);
    if (g === generation && !document.hidden) {
      busy = false;
      const delay = queued ? 0 : (stream?.readyState === EventSource.OPEN ? 1800 : 420);
      timer = setTimeout(() => wake(g), delay);
    }
  }
}
function pause(message) {
  generation++; clearTimeout(timer); stream?.close(); aborter?.abort(); busy = false; queued = false; announced = 0;
  clearPage(message); painting = false;
}
function resume() {
  pause("正在核对当前阅读范围");
  if (document.hidden || !navigator.onLine) return;
  const g = generation;
  stream = new EventSource(base + "/changes");
  stream.addEventListener("revision", event => {
    if (g !== generation) return;
    const update = JSON.parse(event.data);
    announced = update.revision;
    performance.clearMarks("english-revision-event");
    performance.mark("english-revision-event", {detail:update.revision});
    if (meta && meta.access_epoch !== update.access_epoch) clearPage("正在更新阅读范围");
    if (meta?.content_hash !== update.content_hash || update.page_version !== document.documentElement.dataset.pageVersion) wake(g);
  });
  stream.addEventListener("invalidated", () => {
    if (g === generation) { clearPage("当前来源已更新"); wake(g); }
  });
  stream.onerror = () => wake(g);
  wake(g);
}
addEventListener("scroll", saveLayout, {passive:true});
addEventListener("pagehide", () => pause("正在核对当前阅读范围"));
addEventListener("pageshow", resume);
document.addEventListener("visibilitychange", resume);
addEventListener("offline", () => pause("连接已断开"));
addEventListener("online", resume);
