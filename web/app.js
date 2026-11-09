/* AoE4 存档编辑器 —— 前端（只通过 /api/* 与后端通信） */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  const S = {
    state: null,
    rows: [],
    byId: new Map(),
    childrenOf: new Map(),
    elById: new Map(),
    collapsed: new Set(),
    selected: null,
    editing: null,
    search: "",
    onlyEditable: false,
    lastPath: null,
  };

  const KIND_LABEL = { num: "数值", str: "字符串", key: "分组", group: "" };

  /* ---------------- 基础工具 ---------------- */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function setStatus(text) { $("#status").textContent = text || ""; }

  function toast(msg, kind, ms) {
    const box = $("#toasts");
    const el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity .25s";
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 260);
    }, ms || 3000);
  }

  async function api(path, body) {
    const opt = { headers: {} };
    if (body !== undefined) {
      opt.method = "POST";
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opt);
    } catch (e) {
      throw new Error("连不上后端服务，请确认 aoe4_rlc_server.py 仍在运行");
    }
    const text = await res.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch (e) {
      throw new Error("后端返回了非 JSON 响应（HTTP " + res.status + "）");
    }
    if (!res.ok || data.ok === false) throw new Error(data.error || ("HTTP " + res.status));
    return data;
  }

  /* ---------------- 状态 ---------------- */
  function applyState(st) {
    S.state = st;
    S.rows = st.rows || [];
    S.byId = new Map();
    S.childrenOf = new Map();
    for (const r of S.rows) {
      S.byId.set(r.id, r);
      if (!r.parent) continue;
      if (!S.childrenOf.has(r.parent)) S.childrenOf.set(r.parent, []);
      S.childrenOf.get(r.parent).push(r);
    }
    if (S.selected && !S.byId.has(S.selected)) S.selected = null;
    if (st.path !== S.lastPath) {
      S.lastPath = st.path;
      S.collapsed = defaultCollapsed(S.rows);
    }
    render();
  }

  /* 默认折叠层级 >= 2 的分组，免得一打开就是几百行 */
  function defaultCollapsed(rows) {
    const out = new Set();
    for (const r of rows) {
      if (r.has_children && r.depth >= 2) out.add(r.id);
    }
    return out;
  }

  function render() {
    const st = S.state || {};
    const loaded = !!st.loaded;

    $("#ver").textContent = st.version || "";

    const acc = $("#account");
    if (document.activeElement !== acc && st.account) acc.value = st.account;

    const info = $("#fileinfo");
    info.textContent = loaded ? st.path : "未打开文件";
    info.title = loaded ? st.path : "";

    const badge = $("#dirty-badge");
    badge.textContent = !loaded ? "未打开" : (st.dirty ? "● 未保存" : "已同步");
    badge.className = "badge " + (!loaded ? "" : (st.dirty ? "dirty" : "clean"));

    $("#btn-save").disabled = !loaded;
    $("#btn-undo").disabled = !loaded || !st.undo;
    $("#btn-export-plain").disabled = !loaded;
    $("#btn-export-json").disabled = !loaded;

    $("#summary").innerHTML = buildSummary(st);
    $("#empty").hidden = loaded;

    renderRows();
    renderMeta();
  }

  function buildSummary(st) {
    if (!st.loaded) return "";
    const s = st.summary || {};
    const c = st.counts || {};
    const parts = [];
    parts.push("<b>" + (c.total || 0) + "</b> 条（数值 " + (c.num || 0) +
               " / 字符串 " + (c.str || 0) + "）");
    if (s.favor) {
      const f = Array.isArray(s.favor) ? s.favor : [s.favor];
      parts.push("favor <b>" + f.map(esc).join(" / ") + "</b>");
    }
    if (s.favor_cap != null) parts.push("上限 " + esc(s.favor_cap));
    if (s.perkpoint_sum != null) parts.push("天赋成本合计 " + esc(s.perkpoint_sum));
    if (s.version != null) parts.push("版本 " + esc(s.version));
    return parts.join(" · ");
  }

  function renderMeta() {
    const s = S.state || {};
    const bits = [];
    if (s.header) bits.push("头部 " + s.header.head_u32 + " · 格式 v" + s.header.format);
    if (s.loaded) bits.push("明文 " + s.plain_size + " 字节");
    $("#meta").textContent = bits.join(" · ");
  }

  /* ---------------- 表格 ---------------- */
  function renderRows() {
    const wrap = $("#rows");
    const scroll = wrap.scrollTop;
    wrap.textContent = "";
    S.elById = new Map();
    const frag = document.createDocumentFragment();
    for (const r of S.rows) {
      const el = makeRow(r);
      S.elById.set(r.id, el);
      frag.appendChild(el);
    }
    wrap.appendChild(frag);
    applyView();
    wrap.scrollTop = scroll;
    if (S.selected && S.elById.has(S.selected)) {
      S.elById.get(S.selected).classList.add("selected");
    }
  }

  function makeRow(r) {
    const el = document.createElement("div");
    el.className = "row kind-" + r.kind + (r.modified ? " modified" : "");
    el.dataset.id = r.id;
    el.dataset.parent = r.parent || "";
    el.style.setProperty("--d", r.depth || 0);

    const key = document.createElement("div");
    key.className = "c-key";
    const tw = document.createElement("span");
    tw.className = "twist";
    tw.dataset.act = "toggle";
    if (r.has_children) tw.dataset.open = S.collapsed.has(r.id) ? "0" : "1";
    key.appendChild(tw);
    const k = document.createElement("span");
    k.className = "k";
    k.textContent = r.key;
    k.title = r.path;
    key.appendChild(k);
    if (r.modified) {
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.title = "已修改（尚未写盘）";
      key.appendChild(dot);
    }
    el.appendChild(key);

    const val = document.createElement("div");
    val.className = "c-val";
    val.textContent = r.kind === "group" || r.value == null ? "" : String(r.value);
    if (r.kind === "num" || r.kind === "str") val.title = "双击修改";
    el.appendChild(val);

    const ty = document.createElement("div");
    ty.className = "c-type";
    ty.textContent = KIND_LABEL[r.kind] || r.kind;
    el.appendChild(ty);

    const doc = document.createElement("div");
    doc.className = "c-doc";
    doc.textContent = r.doc || "";
    el.appendChild(doc);

    return el;
  }

  function applyView() {
    const needle = S.search.trim().toLowerCase();
    const onlyEd = S.onlyEditable;

    // 1) 命中：自身命中，或任一后代命中
    const match = new Map();
    for (let i = S.rows.length - 1; i >= 0; i--) {
      const r = S.rows[i];
      let m = !needle ||
        r.key.toLowerCase().includes(needle) ||
        (r.path || "").toLowerCase().includes(needle);
      if (onlyEd && r.kind === "key") m = false;
      if (!m) {
        const kids = S.childrenOf.get(r.id);
        if (kids) {
          for (const c of kids) {
            if (match.get(c.id)) { m = true; break; }
          }
        }
      }
      match.set(r.id, m);
    }

    // 2) 折叠：父节点不可见或被折叠时，子孙一并隐藏（行是 DFS 序，父在子前）
    const hiddenSet = new Set();
    let shown = 0;
    for (const r of S.rows) {
      let vis = !!match.get(r.id);
      if (vis && r.parent) {
        if (hiddenSet.has(r.parent) || S.collapsed.has(r.parent)) vis = false;
      }
      if (!vis) hiddenSet.add(r.id);
      else shown++;
      const el = S.elById.get(r.id);
      if (el) el.hidden = !vis;
    }

    // 3) 折叠箭头
    for (const r of S.rows) {
      if (!r.has_children) continue;
      const el = S.elById.get(r.id);
      if (!el) continue;
      const tw = el.querySelector(".twist");
      if (tw) tw.dataset.open = S.collapsed.has(r.id) ? "0" : "1";
    }

    if (S.state && S.state.loaded) {
      const total = S.rows.length;
      if (shown !== total) setStatus("显示 " + shown + " / " + total + " 行");
    }
    return shown;
  }

  function select(id) {
    if (S.selected && S.elById.has(S.selected)) {
      S.elById.get(S.selected).classList.remove("selected");
    }
    S.selected = id;
    const el = S.elById.get(id);
    if (el) {
      el.classList.add("selected");
      el.scrollIntoView({ block: "nearest" });
    }
    showRowInfo();
  }

  function showRowInfo() {
    const r = S.selected ? S.byId.get(S.selected) : null;
    if (!r) { setStatus("就绪"); return; }
    let s = r.path + "  [" + (KIND_LABEL[r.kind] || r.kind) + "]";
    if (r.offset != null) s += "  偏移 0x" + r.offset.toString(16) + " · " + r.size + " 字节";
    if (r.doc) s += "  — " + r.doc;
    setStatus(s);
  }

  function toggleCollapse(id) {
    if (S.collapsed.has(id)) S.collapsed.delete(id);
    else S.collapsed.add(id);
    applyView();
  }

  function expandAll() {
    S.collapsed.clear();
    applyView();
  }

  function collapseAll() {
    S.collapsed = new Set();
    for (const r of S.rows) {
      if (r.has_children) S.collapsed.add(r.id);
    }
    applyView();
  }

  /* ---------------- 就地编辑 ---------------- */
  function beginEdit(id) {
    const r = S.byId.get(id);
    if (!r) return;
    if (r.kind !== "num" && r.kind !== "str") {
      toast("「" + r.key + "」是分组名，不能直接改", "warn", 2500);
      return;
    }
    if (S.editing) commitEdit();
    const el = S.elById.get(id);
    if (!el) return;
    const cell = el.querySelector(".c-val");
    cell.textContent = "";
    const input = document.createElement("input");
    input.type = "text";
    input.className = "cell-input";
    input.spellcheck = false;
    input.value = r.value == null ? "" : String(r.value);
    cell.appendChild(input);
    S.editing = { id, input, row: r };
    input.focus();
    input.select();
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); commitEdit(); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancelEdit(); }
    });
    input.addEventListener("blur", () => {
      if (S.editing && S.editing.input === input) commitEdit();
    });
  }

  function cancelEdit() {
    const ed = S.editing;
    if (!ed) return;
    S.editing = null;
    const r = S.byId.get(ed.id);
    const el = S.elById.get(ed.id);
    if (el && r) {
      el.querySelector(".c-val").textContent = r.value == null ? "" : String(r.value);
    }
  }

  async function commitEdit() {
    const ed = S.editing;
    if (!ed) return;
    S.editing = null;
    const value = ed.input.value;
    S.selected = ed.id;
    try {
      const d = await api("/api/edit", { index: ed.row.index, value: value });
      applyState(d.state);
      if (d.changed) {
        toast(ed.row.key + "：" + d.old + " → " + d.value +
              "\n（还在内存里，点「保存并加密」才写盘）", "ok", 3200);
      }
    } catch (e) {
      toast("修改失败：" + e.message, "err", 7000);
      renderRows();
    }
  }

  /* ---------------- 业务动作 ---------------- */
  async function doOpen(path) {
    const account = $("#account").value.trim();
    try {
      const st = await api("/api/open", { path: path, account: account });
      closeModals();
      S.selected = null;
      S.lastPath = null;      // 强制按新文件重算默认折叠
      applyState(st);
      if (st.warning) toast("⚠ " + st.warning, "warn", 8000);
      else toast("已载入 " + st.file + "（明文 " + st.plain_size + " 字节）", "ok", 2600);
      setStatus("已载入 " + st.path);
    } catch (e) {
      toast("打开失败：" + e.message, "err", 8000);
    }
  }

  async function doSave() {
    if (!S.state || !S.state.loaded) return;
    const account = $("#account").value.trim();
    try {
      const d = await api("/api/save", { account: account });
      applyState(d.state);
      const v = d.verify || {};
      if (v.ok) {
        let msg = "已保存并加密\n" + S.state.path + "\n" + v.detail;
        if (d.backup) msg += "\n备份：" + d.backup;
        toast(msg, "ok", 6000);
        setStatus("已写盘 " + S.state.file);
      } else {
        toast("⚠ 写盘后校验异常：" + v.detail, "err", 9000);
      }
    } catch (e) {
      toast("保存失败：" + e.message, "err", 9000);
    }
  }

  async function doUndo() {
    try {
      const d = await api("/api/undo", {});
      applyState(d.state);
      toast("已撤销一步修改", "ok", 1800);
    } catch (e) {
      toast(e.message, "warn", 2500);
    }
  }

  async function doRevert() {
    const r = S.selected ? S.byId.get(S.selected) : null;
    if (!r || r.index == null) return;
    try {
      const d = await api("/api/revert", { index: r.index });
      applyState(d.state);
      toast(d.changed ? (r.key + " 已还原为打开时的值") : (r.key + " 本来就是原始值"), "ok", 2500);
    } catch (e) {
      toast(e.message, "err", 4000);
    }
  }

  async function doExport(kind) {
    try {
      const d = await api("/api/export", { kind: kind });
      toast("已导出 " + d.count + " 条字段：\n" + d.path, "ok", 5000);
    } catch (e) {
      toast("导出失败：" + e.message, "err", 7000);
    }
  }

  async function doSelftest() {
    try {
      const d = await api("/api/selftest");
      $("#text-title").textContent = "自检";
      $("#text-body").textContent = (d.lines || []).join("\n");
      $("#modal-text").hidden = false;
    } catch (e) {
      toast(e.message, "err", 6000);
    }
  }

  async function detectAccount() {
    try {
      const d = await api("/api/candidates");
      const accs = d.accounts || [];
      if (!accs.length) { toast("没找到存档目录：" + d.users_dir, "warn", 6000); return; }
      $("#account").value = accs[0].account;
      toast("已填入 SteamID64：" + accs[0].account +
            (accs.length > 1 ? "（共 " + accs.length + " 个账号，取最近使用的）" : ""), "ok", 3000);
    } catch (e) {
      toast(e.message, "err", 6000);
    }
  }

  /* ---------------- 打开存档弹窗 ---------------- */
  async function openDialog() {
    const box = $("#cand-list");
    $("#modal-open").hidden = false;
    box.textContent = "正在扫描存档目录…";
    try {
      const d = await api("/api/candidates");
      const items = d.items || [];
      if (!items.length) {
        box.textContent = "没有在默认目录找到 .rlc 存档：" + d.users_dir +
                          "\n可在下面直接填完整路径。";
        return;
      }
      box.textContent = "";
      for (const it of items) {
        const row = document.createElement("div");
        row.className = "cand";
        row.innerHTML =
          '<span class="name">' + esc(it.name) + "</span>" +
          (it.cloud ? '<span class="tag">cloud</span>' : "") +
          '<span class="acc">' + esc(it.account) + "</span>" +
          '<span class="path">' + esc(it.path) + "</span>";
        row.title = it.path;
        row.addEventListener("click", () => doOpen(it.path));
        box.appendChild(row);
      }
    } catch (e) {
      box.textContent = "扫描失败：" + e.message;
    }
  }

  function closeModals() {
    $("#modal-open").hidden = true;
    $("#modal-text").hidden = true;
  }

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    $("#btn-open").addEventListener("click", openDialog);
    $("#btn-cancel-open").addEventListener("click", closeModals);
    $("#btn-close-text").addEventListener("click", closeModals);
    $("#btn-save").addEventListener("click", doSave);
    $("#btn-undo").addEventListener("click", doUndo);
    $("#btn-detect").addEventListener("click", detectAccount);
    $("#btn-expand").addEventListener("click", expandAll);
    $("#btn-collapse").addEventListener("click", collapseAll);
    $("#btn-selftest").addEventListener("click", doSelftest);
    $("#btn-export-plain").addEventListener("click", () => doExport("plain"));
    $("#btn-export-json").addEventListener("click", () => doExport("json"));
    $("#btn-manual-open").addEventListener("click", () => {
      const p = $("#manual-path").value.trim();
      if (!p) { toast("请先填路径", "warn", 2000); return; }
      doOpen(p);
    });

    $("#search").addEventListener("input", (e) => {
      S.search = e.target.value;
      applyView();
    });

    $("#only-editable").addEventListener("change", (e) => {
      S.onlyEditable = e.target.checked;
      applyView();
    });

    const rows = $("#rows");
    rows.addEventListener("click", (e) => {
      const tw = e.target.closest(".twist");
      if (tw) {
        const row = tw.closest(".row");
        if (row) toggleCollapse(row.dataset.id);
        return;
      }
      const row = e.target.closest(".row");
      if (row) select(row.dataset.id);
    });

    rows.addEventListener("dblclick", (e) => {
      const row = e.target.closest(".row");
      if (!row) return;
      if (!e.target.closest(".c-val")) return;
      beginEdit(row.dataset.id);
    });

    rows.addEventListener("contextmenu", (e) => {
      const row = e.target.closest(".row");
      if (!row) return;
      e.preventDefault();
      select(row.dataset.id);
      const r = S.byId.get(row.dataset.id);
      if (r && (r.kind === "num" || r.kind === "str")) doRevert();
    });

    rows.addEventListener("keydown", (e) => {
      if (!S.selected) return;
      if (e.key === "Enter" || e.key === "F2") { e.preventDefault(); beginEdit(S.selected); }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closeModals();
        if (S.editing) cancelEdit();
        return;
      }
      const mod = e.ctrlKey || e.metaKey;
      if (!mod) return;
      const k = e.key.toLowerCase();
      if (k === "s") { e.preventDefault(); doSave(); }
      else if (k === "f") { e.preventDefault(); $("#search").focus(); $("#search").select(); }
      else if (k === "o") { e.preventDefault(); openDialog(); }
      else if (k === "z" && !S.editing) { e.preventDefault(); doUndo(); }
    });

    $("#modal-open").addEventListener("click", (e) => {
      if (e.target.id === "modal-open") closeModals();
    });
    $("#modal-text").addEventListener("click", (e) => {
      if (e.target.id === "modal-text") closeModals();
    });
  }

  async function init() {
    bind();
    try {
      const st = await api("/api/state");
      applyState(st);
      if (st.loaded) setStatus("已载入 " + st.path);
      else setStatus("就绪：先填 SteamID64（或点「自动检测」），再打开存档");
    } catch (e) {
      toast(e.message, "err", 9000);
    }
  }

  init();
})();
