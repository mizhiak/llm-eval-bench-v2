const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

// ---- 可选鉴权支持 ----
// 后端设置 EVALBENCH_AUTH_TOKEN 后，前端通过 ?token= 传递令牌。
// 首次可访问 http://host/?token=xxx 把令牌存入 localStorage，随后自动带上。
(function initAuth() {
  const params = new URLSearchParams(location.search);
  const t = params.get("token");
  if (t) {
    try { localStorage.setItem("evalbench_token", t); } catch (e) {}
    params.delete("token");
    const qs = params.toString();
    history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
  }
  const _fetch = window.fetch;
  window.fetch = function (url, opts) {
    let tk = null;
    try { tk = localStorage.getItem("evalbench_token"); } catch (e) {}
    if (tk && typeof url === "string") {
      url = url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(tk);
    }
    return _fetch.call(this, url, opts);
  };
})();

let selectedDatasets = new Set();
let selectedSubjects = new Map();  // dsName -> Set(subjects)
let currentTaskId = null;
let _taskStreams = {};  // { taskId: { es, logs[], progress, sweepRows[], mode, done, config } }
let sweepRows = [];     // legacy, used by viewTask/rerun

// ---- Tabs ----
$$(".tab").forEach(t => t.addEventListener("click", () => {
  $$(".tab").forEach(x => x.classList.remove("active"));
  $$(".tab-pane").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  $("#pane-" + t.dataset.tab).classList.add("active");
}));

// ---- Perf scale mode toggle ----
$("#perfScaleMode").addEventListener("change", () => {
  const on = $("#perfScaleMode").checked;
  $("#perfScaleWrap").style.display = on ? "inline-flex" : "none";
  $("#perfTotal").style.display = on ? "none" : "";
  const preview = document.getElementById("perfScalePreview");
  if (preview) preview.style.display = on ? "inline" : "none";
  if (on) $("#perfTotal").dataset.prev = $("#perfTotal").value;
  else if ($("#perfTotal").dataset.prev) $("#perfTotal").value = $("#perfTotal").dataset.prev;
  updatePerfEstimate();
});
$("#perfScaleMult").addEventListener("input", updatePerfEstimate);
$("#sweepLevels").addEventListener("input", updatePerfScalePreview);
updatePerfScalePreview();

function updatePerfScalePreview() {
  const preview = document.getElementById("perfScalePreview");
  if (!preview) return;
  const scaleOn = $("#perfScaleMode").checked;
  if (!scaleOn) { preview.style.display = "none"; return; }
  const levels = parseLevels($("#sweepLevels").value);
  const mult = parseInt($("#perfScaleMult").value) || 10;
  if (!levels.length) { preview.textContent = ""; return; }
  const parts = levels.map(c => `${c}并发→${c * mult}请求`);
  preview.textContent = parts.slice(0, 4).join("，") + (levels.length > 4 ? "…" : "");
  preview.style.display = "inline";
}

// ---- Perf toggle ----
$("#runPerf").addEventListener("change", e => {
  $("#perfBody").classList.toggle("disabled", !e.target.checked);
  updatePerfEstimate();
  updateConfigSummaries();
});
$("#perfBody").classList.add("disabled");

// ---- Context scan toggle ----
$("#runCtxScan").addEventListener("change", e => {
  $("#ctxBody").classList.toggle("disabled", !e.target.checked);
  updateCtxEstimate();
  updateConfigSummaries();
});
$("#ctxBody").classList.add("disabled");

// ---- Context length warning + perf estimate ----
["input", "change"].forEach(evt => {
  $("#contextLength").addEventListener(evt, () => {
    const v = parseInt($("#contextLength").value) || 0;
    const warn = document.getElementById("perfCtxWarn");
    const promptField = document.getElementById("perfPromptField");
    if (v > 0) {
      warn.classList.add("show");
      const approx = Math.round(v / 1.5);
      document.getElementById("perfCtxApprox").textContent = approx;
      promptField.style.opacity = "0.4";
      promptField.style.pointerEvents = "none";
    } else {
      warn.classList.remove("show");
      promptField.style.opacity = "";
      promptField.style.pointerEvents = "";
    }
    updatePerfEstimate();
  });
  $("#sweepLevels").addEventListener(evt, updatePerfEstimate);
  $("#perfTotal").addEventListener(evt, updatePerfEstimate);
  $("#perfMaxTokens").addEventListener(evt, updatePerfEstimate);
  $("#ctxLengths").addEventListener(evt, updateCtxEstimate);
  $("#ctxConcurrency").addEventListener(evt, updateCtxEstimate);
  $("#ctxRequests").addEventListener(evt, updateCtxEstimate);
  $("#ctxMaxTokens").addEventListener(evt, updateCtxEstimate);
});

// 委托式 click 监听：数据集选择 / 逐请求明细按钮。
// 注意：必须放在上面的 forEach 之外，否则会被重复注册（点击触发两次请求）。
document.addEventListener("click", e => {
  if (e.target.closest(".ds-item") || e.target.closest(".subj-chip") || e.target.closest(".subj-clear")) {
    setTimeout(updateConfigSummaries, 100);
  }
  if (e.target.closest("#btnLoadPerfReq")) {
    loadPerfRequests();
  }
});

function updatePerfEstimate() {
  const el = document.getElementById("perfEst");
  if (!el) return;
  const levels = parseLevels($("#sweepLevels").value);
  const perLevel = parseInt($("#perfTotal").value) || 20;
  const maxTok = parseInt($("#perfMaxTokens").value) || 256;
  const ctxLen = parseInt($("#contextLength").value) || 0;
  const scaleOn = $("#perfScaleMode").checked;
  const scaleMult = parseInt($("#perfScaleMult").value) || 10;
  if (!levels.length) { el.classList.remove("show"); return; }
  let totalReq;
  if (scaleOn) {
    totalReq = levels.reduce((s, c) => s + c * scaleMult, 0);
  } else {
    totalReq = levels.length * perLevel;
  }
  const avgInput = ctxLen || 200;
  const estTokens = totalReq * (avgInput + maxTok);
  const tokStr = estTokens > 1e6 ? (estTokens / 1e6).toFixed(1) + "M" : estTokens > 1e3 ? (estTokens / 1e3).toFixed(1) + "K" : String(estTokens);
  const modeLabel = scaleOn ? `每档并发×${scaleMult}` : `每档${perLevel}`;
  el.innerHTML = `<b>${totalReq}</b> 请求 · <b>${levels.length}</b> 档 (${modeLabel}) · 预估 ~<b>${tokStr}</b> tokens${ctxLen > 0 ? "（长上下文填充模式）" : ""}`;
  el.classList.add("show");
  updateConfigSummaries();
}

function updateCtxEstimate() {
  const el = document.getElementById("ctxEst");
  if (!el) return;
  const lengths = parseLevels($("#ctxLengths").value);
  const perLevel = parseInt($("#ctxRequests").value) || 20;
  const maxTok = parseInt($("#ctxMaxTokens").value) || 256;
  if (!lengths.length) { el.classList.remove("show"); return; }
  const totalReq = lengths.length * perLevel;
  const avgInput = lengths.reduce((a,b) => a+b, 0) / lengths.length;
  const estTokens = totalReq * (avgInput + maxTok);
  const tokStr = estTokens > 1e6 ? (estTokens / 1e6).toFixed(1) + "M" : estTokens > 1e3 ? (estTokens / 1e3).toFixed(1) + "K" : String(estTokens);
  el.innerHTML = `<b>${totalReq}</b> 请求 · <b>${lengths.length}</b> 档 · 预估 ~<b>${tokStr}</b> tokens`;
  el.classList.add("show");
  updateConfigSummaries();
}

function updateConfigSummaries() {
  // Accuracy summary
  const accEl = document.getElementById("accSummary");
  if (accEl) {
    accEl.textContent = selectedDatasets.size ? `${selectedDatasets.size} 数据集` : "";
  }
  // Perf summary
  const perfEl = document.getElementById("perfSummary");
  if (perfEl) {
    if ($("#runPerf").checked) {
      const levels = parseLevels($("#sweepLevels").value);
      perfEl.textContent = levels.length ? `${levels.length} 档 · 并发 ${levels[0]}~${levels[levels.length-1]}` : "";
    } else {
      perfEl.textContent = "";
    }
  }
  // Context scan summary
  const ctxEl = document.getElementById("ctxSummary");
  if (ctxEl) {
    if ($("#runCtxScan").checked) {
      const lengths = parseLevels($("#ctxLengths").value);
      ctxEl.textContent = lengths.length ? `${lengths.length} 档` : "";
    } else {
      ctxEl.textContent = "";
    }
  }
}

// ---- Load datasets ----
async function loadDatasets() {
  try {
    const r = await fetch("/api/datasets");
    const data = await r.json();
    renderDatasets(data.datasets);
  } catch (e) {
    $("#datasetList").innerHTML = '<div class="loading-sm">加载失败，请刷新</div>';
  }
}

// Dataset categories
const DS_CATEGORIES = {
  "中文评测": ["ceval", "chinese_simpleqa", "cmmlu", "cmmmu"],
  "数学推理": ["aime25", "aime26", "gsm8k", "math_500", "math_vision", "math_vista", "mgsm", "minerva_math", "competition_math", "olympiad_bench"],
  "代码生成": ["humaneval", "humaneval_plus", "live_code_bench", "mbpp", "mbpp_plus", "multiple_humaneval"],
  "多模态": ["ai2d", "chartqa", "mm_bench", "mmmu", "mmmu_pro", "real_world_qa", "seed_bench_2_plus"],
  "常识推理": ["arc", "commonsense_qa", "hellaswag", "piqa", "qasc", "sciq", "siqa", "winogrande", "race"],
  "知识问答": ["bbh", "drop", "mmlu", "mmlu_pro", "mmlu_redux", "pubmedqa", "simple_qa", "trivia_qa", "truthful_qa", "super_gpqa"],
  "指令遵循": ["ifbench", "ifeval", "multi_if"],
  "逻辑推理": ["logi_qa"],
  "长文本": ["longbench_v2", "needle_haystack"],
};

let _allDatasets = [];
function renderDatasets(list, filter = "") {
  _allDatasets = list;
  const el = $("#datasetList");
  el.innerHTML = "";
  const q = (filter || "").toLowerCase();
  let filtered = list;
  if (q) {
    filtered = list.filter(ds =>
      ds.display.toLowerCase().includes(q) ||
      ds.name.toLowerCase().includes(q)
    );
  }
  if (!filtered.length) {
    el.innerHTML = q ? '<div class="loading-sm">无匹配数据集</div>'
      : '<div class="skeleton skeleton-card"></div><div class="skeleton skeleton-card"></div><div class="skeleton skeleton-card"></div><div class="skeleton skeleton-card"></div>';
    return;
  }

  // Group datasets by category
  const grouped = {};
  const uncategorized = [];
  
  filtered.forEach(ds => {
    let found = false;
    for (const [cat, names] of Object.entries(DS_CATEGORIES)) {
      if (names.includes(ds.name)) {
        if (!grouped[cat]) grouped[cat] = [];
        grouped[cat].push(ds);
        found = true;
        break;
      }
    }
    if (!found) uncategorized.push(ds);
  });

  // Render categorized groups
  for (const [cat, datasets] of Object.entries(grouped)) {
    if (!datasets.length) continue;
    const groupEl = document.createElement("details");
    groupEl.className = "ds-group";
    groupEl.open = false; // Collapsed by default
    
    const selectedInGroup = datasets.filter(ds => selectedDatasets.has(ds.name)).length;
    const totalCount = datasets.reduce((sum, ds) => sum + (ds.count || 0), 0);
    
    groupEl.innerHTML = `
      <summary class="ds-group-head">
        <span class="ds-group-name">${cat}</span>
        <span class="ds-group-meta">${datasets.length} 个数据集 · ${totalCount} 题${selectedInGroup ? ` · 已选 ${selectedInGroup}` : ''}</span>
      </summary>
      <div class="ds-group-body"></div>
    `;
    
    const body = groupEl.querySelector(".ds-group-body");
    datasets.forEach(ds => body.appendChild(createDatasetItem(ds)));
    el.appendChild(groupEl);
  }

  // Render uncategorized
  if (uncategorized.length) {
    const groupEl = document.createElement("details");
    groupEl.className = "ds-group";
    groupEl.open = true; // Expanded by default
    
    const selectedInGroup = uncategorized.filter(ds => selectedDatasets.has(ds.name)).length;
    const totalCount = uncategorized.reduce((sum, ds) => sum + (ds.count || 0), 0);
    
    groupEl.innerHTML = `
      <summary class="ds-group-head">
        <span class="ds-group-name">其他</span>
        <span class="ds-group-meta">${uncategorized.length} 个数据集 · ${totalCount} 题${selectedInGroup ? ` · 已选 ${selectedInGroup}` : ''}</span>
      </summary>
      <div class="ds-group-body"></div>
    `;
    
    const body = groupEl.querySelector(".ds-group-body");
    uncategorized.forEach(ds => body.appendChild(createDatasetItem(ds)));
    el.appendChild(groupEl);
  }
}

function createDatasetItem(ds) {
  const wrap = document.createElement("div");
  wrap.className = "ds-wrap";
  const item = document.createElement("div");
  item.className = "ds-item" + (selectedDatasets.has(ds.name) ? " checked" : "");
  const typeTag = ds.type === "numeric" ? "数值" : "选择";
  const hasSubjects = ds.subjects && ds.subjects.length > 1;
  // Special handling for dynamic datasets
  const isDynamic = ds.name === 'longbench_v2' || ds.name === 'needle_haystack';
  const countDisplay = isDynamic ? '动态' : `${ds.count} 题`;
  const isLongText = ds.name === 'longbench_v2' || ds.name === 'needle_haystack';
  const warnIcon = isLongText ? '<span class="ds-warn" title="长文本测试需要模型支持较长上下文（建议≥32K），否则可能崩溃或结果不准确">⚠</span>' : '';
  item.innerHTML = `
    <input type="checkbox" ${selectedDatasets.has(ds.name) ? "checked" : ""}>
    <span class="ds-name">${esc(ds.display)}</span>
    ${warnIcon}
    <span class="ds-type">${typeTag}</span>
    <span class="ds-meta">${countDisplay}</span>
    ${hasSubjects ? `<button class="ds-subj-btn" title="选择学科子集">学科</button>` : ""}`;
  item.addEventListener("click", (e) => {
    if (e.target.classList.contains("ds-subj-btn")) return;
    if (selectedDatasets.has(ds.name)) selectedDatasets.delete(ds.name);
    else selectedDatasets.add(ds.name);
    item.classList.toggle("checked");
    item.querySelector("input").checked = selectedDatasets.has(ds.name);
    updateDsCount();
  });
  wrap.appendChild(item);
  // 学科子集面板
  if (hasSubjects) {
    const panel = document.createElement("div");
    panel.className = "ds-subjects";
    panel.style.display = "none";
    panel.innerHTML = `<div class="subj-head">
      <span>选择学科（不选=全部 ${ds.subjects.length} 个）</span>
      <button class="subj-clear">清空</button></div>
      <div class="subj-chips"></div>`;
    const chips = panel.querySelector(".subj-chips");
    ds.subjects.forEach(s => {
      const chip = document.createElement("button");
      chip.className = "subj-chip"; chip.textContent = s;
      chip.addEventListener("click", () => {
        const set = selectedSubjects.get(ds.name) || new Set();
        if (set.has(s)) set.delete(s); else set.add(s);
        selectedSubjects.set(ds.name, set);
        chip.classList.toggle("on", set.has(s));
        // 选了学科自动勾选该数据集
        if (set.size && !selectedDatasets.has(ds.name)) {
          selectedDatasets.add(ds.name);
          item.classList.add("checked");
          item.querySelector("input").checked = true;
        }
        updateSubjBtnLabel(item, ds.name);
        updateDsCount();
      });
      chips.appendChild(chip);
    });
    panel.querySelector(".subj-clear").addEventListener("click", () => {
      selectedSubjects.delete(ds.name);
      panel.querySelectorAll(".subj-chip").forEach(c => c.classList.remove("on"));
      updateSubjBtnLabel(item, ds.name);
      updateDsCount();
    });
    wrap.appendChild(panel);
    item.querySelector(".ds-subj-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      panel.style.display = panel.style.display === "none" ? "block" : "none";
    });
  }
  return wrap;
}

function updateSubjBtnLabel(item, dsName) {
  const btn = item.querySelector(".ds-subj-btn");
  if (!btn) return;
  const set = selectedSubjects.get(dsName);
  btn.textContent = set && set.size ? `学科·${set.size}` : "学科";
  btn.classList.toggle("on", !!(set && set.size));
}
loadDatasets();

// Dataset select all / deselect
$("#dsSelectAll").addEventListener("click", () => {
  _allDatasets.forEach(ds => selectedDatasets.add(ds.name));
  renderDatasets(_allDatasets, dsSearch ? dsSearch.value : "");
  updateDsCount();
});
$("#dsDeselectAll").addEventListener("click", () => {
  selectedDatasets.clear();
  selectedSubjects.clear();
  renderDatasets(_allDatasets, dsSearch ? dsSearch.value : "");
  updateDsCount();
});

function updateDsCount() {
  const el = document.getElementById("dsCount");
  if (el) el.textContent = `已选 ${selectedDatasets.size} 个`;
  updateConfigSummaries();
}

// Patch renderDatasets to update count after render
const _origRenderDatasets = renderDatasets;
renderDatasets = function(list, filter) {
  _origRenderDatasets(list, filter);
  updateDsCount();
};

// Dataset search filter（200ms 防抖，避免每敲一个字全量重建列表）
const dsSearch = document.getElementById("datasetSearch");
if (dsSearch) {
  let _dsTimer = null;
  dsSearch.addEventListener("input", () => {
    clearTimeout(_dsTimer);
    _dsTimer = setTimeout(() => renderDatasets(_allDatasets, dsSearch.value), 200);
  });
}

// ---- Upload dataset ----
$("#fileInput").addEventListener("change", async e => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/upload_dataset", { method: "POST", body: fd });
    const data = await r.json();
    if (r.ok) {
      selectedDatasets.add(data.name);
      await loadDatasets();
    } else {
      toast("上传失败：" + (data.detail || "未知错误"), "error");
    }
  } catch (err) {
    toast("上传失败：" + err.message, "error");
  }
  e.target.value = "";
});

// ---- Build config ----
function buildConfig() {
  const accMaxTokens = parseInt($("#accMaxTokens").value);
  return {
    base_url: $("#baseUrl").value.trim(),
    api_key: $("#apiKey").value.trim(),
    model: $("#model").value.trim(),
    api_format: $("#apiFormat").value,
    timeout: parseFloat($("#timeout").value) || 120,
    disable_thinking: $("#disableThinking").checked,
    task_name: $("#taskName").value.trim(),
    serving_label: ($("#servingLabel")?.value || "").trim(),
    accuracy_datasets: [...selectedDatasets],
    dataset_subjects: Object.fromEntries(
      [...selectedSubjects.entries()]
        .filter(([k, v]) => v && v.size)
        .map(([k, v]) => [k, [...v]])),
    sample_limit: parseInt($("#sampleLimit").value) || 0,
    few_shot: parseInt($("#fewShot").value) || 0,
    acc_concurrency: parseInt($("#accConcurrency").value) || 4,
    acc_max_tokens: Number.isFinite(accMaxTokens) && accMaxTokens > 0 ? accMaxTokens : 0,
    acc_temperature: parseFloat($("#accTemperature").value) || 0,
    acc_system: $("#accSystem").value.trim(),
    acc_template: $("#accTemplate").value,
    max_retries: parseInt($("#maxRetries").value) >= 0 ? parseInt($("#maxRetries").value) : 2,
    acc_stream: $("#accStream").value === "true",
    run_performance: $("#runPerf").checked,
    perf: {
      levels: parseLevels($("#sweepLevels").value),
      requests_per_level: parseInt($("#perfTotal").value) || 20,
      scale_multiplier: $("#perfScaleMode").checked ? (parseInt($("#perfScaleMult").value) || 10) : 0,
      max_tokens: parseInt($("#perfMaxTokens").value) || 256,
      min_tokens: parseInt($("#perfMinTokens").value) || 0,
      stream: $("#perfStream").value === "true",
      prompt: $("#perfPrompt").value.trim(),
      context_length: parseInt($("#contextLength").value) || 0,
      temperature: parseFloat($("#perfTemperature").value) || 0,
      system: $("#perfSystem").value.trim(),
      timeout: parseFloat($("#perfTimeout").value) || 300,
      warmup_requests: parseFloat($("#perfWarmup").value) || 0,
    },
    // Context scan independent params
    context_lengths: $("#runCtxScan").checked ? parseLevels($("#ctxLengths").value) : [],
    context_concurrency: parseInt($("#ctxConcurrency").value) || 8,
    context_requests: parseInt($("#ctxRequests").value) || 20,
    context_max_tokens: parseInt($("#ctxMaxTokens").value) || 256,
    context_stream: $("#ctxStream").value === "true",
  };
}

function parseLevels(raw) {
  const levels = (raw || "")
    .split(",")
    .map(x => parseInt(x.trim()))
    .filter(x => Number.isFinite(x) && x > 0);
  return [...new Set(levels)].sort((a, b) => a - b);
}

// ---- Test connection ----
$("#btnTest").addEventListener("click", async () => {
  const cfg = buildConfig();
  const out = $("#connResult");
  if (!cfg.base_url) { out.className = "conn-result err"; out.textContent = "请填写接口地址"; return; }
  out.className = "conn-result"; out.textContent = "测试中…";
  $("#btnTest").disabled = true;
  try {
    const r = await fetch("/api/test_connection", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: cfg.base_url, api_key: cfg.api_key,
        model: cfg.model, api_format: cfg.api_format, timeout: cfg.timeout,
        disable_thinking: cfg.disable_thinking }),
    });
    const raw = await r.text();
    let data;
    try {
      data = JSON.parse(raw);
    } catch (_) {
      // 后端返回了非 JSON（如 500 错误页），直接显示原始内容
      out.className = "conn-result err";
      out.textContent = `✗ 服务端错误 (HTTP ${r.status}): ${raw.slice(0, 200)}`;
      $("#btnTest").disabled = false;
      return;
    }
    if (data.ok) {
      out.className = "conn-result ok";
      out.textContent = `✓ 连通正常 · 延迟 ${data.latency}s · 返回: ${data.sample || "(空)"}`;
      fetchModels();
      fetchServingInfo(cfg.base_url);  // 溯源：关联被测服务容器
    } else {
      out.className = "conn-result err";
      const ep = data.endpoint ? ` [请求地址: ${data.endpoint}]` : "";
      out.textContent = "✗ " + (data.error || "连接失败") + ep;
    }
  } catch (e) {
    out.className = "conn-result err"; out.textContent = "✗ " + e.message;
  }
  $("#btnTest").disabled = false;
});

// ---- 拉取模型列表（经后端代理，避免跨域）----
// ---- 溯源：查询被测地址对应的服务容器 ----
async function fetchServingInfo(baseUrl) {
  const out = $("#connResult");
  if (!out) return;
  try {
    const r = await fetch("/api/serving-info?base_url=" + encodeURIComponent(baseUrl));
    const d = await r.json();
    if (d.found && d.container) {
      const extra = document.createElement("div");
      extra.className = "conn-serving";
      extra.innerHTML = `🐳 被测服务容器：<b>${esc(d.container)}</b>${d.image ? ` <span class="mono-dim">(${esc(d.image)})</span>` : ""}${d.status ? ` · ${esc(d.status)}` : ""}${d.ambiguous ? " · ⚠ 同端口多容器" : ""}`;
      out.appendChild(extra);
    }
  } catch (e) { /* 溯源失败不影响主流程 */ }
}

async function fetchModels() {
  const baseUrl = $("#baseUrl").value.trim();
  if (!baseUrl) return;
  try {
    const r = await fetch("/api/models", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: baseUrl, api_key: $("#apiKey").value.trim() }),
    });
    const d = await r.json();
    if (d.ok && d.models && d.models.length) {
      $("#modelList").innerHTML = d.models.map(m => `<option value="${esc(m)}">`).join("");
      // 模型名为空且只有一个模型时自动填上
      if (!$("#model").value.trim() && d.models.length === 1) {
        $("#model").value = d.models[0];
        saveForm();
      }
    }
  } catch (e) { /* 拉取失败静默，手填即可 */ }
}
$("#baseUrl").addEventListener("blur", fetchModels);

// ---- Preflight ----
$("#btnPreflight").addEventListener("click", async () => {
  const cfg = buildConfig();
  const panel = $("#preflightPanel");
  panel.className = "preflight-panel open";
  panel.innerHTML = '<div class="loading-sm">正在预检…</div>';
  $("#btnPreflight").disabled = true;
  try {
    const r = await fetch("/api/preflight", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "预检失败");
    renderPreflight(data);
  } catch (e) {
    panel.className = "preflight-panel open error";
    panel.innerHTML = `<div class="pf-item error"><b>FAIL</b><span>${esc(e.message)}</span></div>`;
  }
  $("#btnPreflight").disabled = false;
});

function renderPreflight(data) {
  const panel = $("#preflightPanel");
  const badge = data.ok ? "可开始" : "需处理";
  const cls = data.ok ? "ok" : "error";
  const items = (data.issues || []).map(it => `
    <div class="pf-item ${it.level}">
      <b>${it.level === "ok" ? "OK" : it.level === "warn" ? "WARN" : "FAIL"}</b>
      <span><strong>${esc(it.title)}</strong>${esc(it.message)}</span>
    </div>`).join("");
  const est = data.estimate || {};
  const estLine = [
    est.accuracy_known_samples ? `精度样本约 ${fmtNum(est.accuracy_known_samples)}` : "",
    est.accuracy_unknown_datasets ? `${est.accuracy_unknown_datasets} 个数据集由 evalscope 运行时读取规模` : "",
    est.perf_requests ? `压测请求 ${fmtNum(est.perf_requests)}` : "",
  ].filter(Boolean).join(" · ");
  panel.className = `preflight-panel open ${cls}`;
  panel.innerHTML = `
    <div class="pf-head">
      <span>${badge}</span>
      <em>${data.errors || 0} 错误 · ${data.warnings || 0} 警告</em>
    </div>
    ${estLine ? `<div class="pf-est">${esc(estLine)}</div>` : ""}
    <div class="pf-list">${items}</div>`;
}

// ---- Console logging ----
let consoleStick = true;  // 用户上翻时暂停自动滚底，回到底部恢复
$("#console").addEventListener("scroll", () => {
  const c = $("#console");
  consoleStick = (c.scrollHeight - c.scrollTop - c.clientHeight) < 40;
});
function addLog(level, msg, ts) {
  const c = $("#console");
  const empty = c.querySelector(".console-empty");
  if (empty) empty.remove();
  const time = new Date((ts || Date.now() / 1000) * 1000)
    .toTimeString().split(" ")[0];
  const tags = { info: "INFO", success: " OK ", warn: "WARN", error: "FAIL" };
  const line = document.createElement("div");
  line.className = "log-line " + level;
  line.innerHTML = `<span class="ts">${time}</span><span class="tag">[${tags[level] || "INFO"}]</span><span class="msg"></span>`;
  line.querySelector(".msg").textContent = msg;
  c.appendChild(line);
  // DOM 行数上限：长任务日志不无限增长，超限移除最早的行
  while (c.children.length > 2500 && c.firstChild) c.removeChild(c.firstChild);
  if (consoleStick) c.scrollTop = c.scrollHeight;
}

function setStatus(text, cls) {
  const s = $("#globalStatus");
  s.textContent = text;
  s.className = "status-pill" + (cls ? " " + cls : "");
  // 运行中时激活顶栏实时活动流光条
  const app = document.querySelector(".app");
  if (app) app.classList.toggle("running", cls === "running");
  // 实时进程标签页圆点
  const liveTab = document.querySelector('[data-tab="live"]');
  if (liveTab) liveTab.classList.toggle("running", cls === "running");
}

// ---- Progress overview ----
let _currentConfig = null;
let _lastProgress = null;

function updateProgressOverview(ev) {
  const activeCfg = _taskStreams[currentTaskId]?.config || _currentConfig;
  if (!activeCfg) return;
  const el = $("#progressOverview");
  if (!el) return;
  _lastProgress = ev;

  const pct = ev.percent || 0;
  const completed = ev.completed || 0;
  const total = ev.total || 0;
  const elapsed = ev.elapsed || 0;
  const eta = pct > 0 ? Math.round(elapsed / pct * (100 - pct)) : null;

  let icon = "\u25cf";
  let title = "";
  let detail = "";
  let next = "";

  if (ev.stage === "accuracy") {
    icon = "\u25c9";
    title = `\u7cbe\u5ea6\u8bc4\u6d4b \u00b7 ${ev.dataset || ""}`;
    detail = `evalscope \u8bc4\u6d4b\u4e2d`;
    // Guess next dataset from config
    const dsList = activeCfg.accuracy_datasets || [];
    const idx = dsList.indexOf(ev.dataset);
    if (idx >= 0 && idx < dsList.length - 1) {
      next = `\u4e0b\u4e00\u4e2a\u6570\u636e\u96c6: ${dsList[idx + 1]}`;
    }
  } else if (ev.stage === "performance") {
    icon = "\u25c6";
    const levels = (activeCfg.perf || {}).levels || activeCfg.perf_sweep_levels || [];
    const reqPerLevel = ((activeCfg.perf || {}).requests_per_level) || 10;
    const done = completed >= total && total > 0;
    const currentLevelIdx = done
      ? levels.length - 1
      : Math.min(Math.floor(completed / Math.max(reqPerLevel, 1)), levels.length - 1);
    const currentLevel = levels[currentLevelIdx] || levels[0] || "?";
    const levelProgress = done ? reqPerLevel : (completed % Math.max(reqPerLevel, 1));
    title = `\u6027\u80fd\u538b\u6d4b \u00b7 \u7b2c ${currentLevelIdx + 1}/${levels.length} \u6863 (\u5e76\u53d1 ${currentLevel})`;
    detail = done ? `\u5f53\u524d\u6863: \u5df2\u5b8c\u6210 ${reqPerLevel}/${reqPerLevel}` : `\u5f53\u524d\u6863: \u5df2\u5b8c\u6210 ${levelProgress}/${reqPerLevel}`;
    if (!done && currentLevelIdx < levels.length - 1) {
      next = `\u4e0b\u4e00\u6863: \u5e76\u53d1 ${levels[currentLevelIdx + 1]}`;
    }
  } else if (ev.stage === "context_scan") {
    icon = "\u25b3";
    title = `\u4e0a\u4e0b\u6587\u626b\u63cf \u00b7 ${ev.dataset || ""}`;
    detail = `${completed}/${total} \u6863`;
  }

  el.innerHTML = `<div class="po-track">
    <div class="po-head">
      <span class="po-icon">${icon}</span>
      <span class="po-title">${esc(title)}</span>
    </div>
    <div class="po-bar-wrap">
      <div class="po-bar"><div class="po-bar-fill" style="width:${Math.round(pct)}%"></div></div>
      <span class="po-pct">${Math.round(pct)}%</span>
    </div>
    <div class="po-meta">
      <span>${completed}/${total}</span>
      <span>\u23f1 \u5df2\u8fd0\u884c ${fmtDur(elapsed)}</span>
      ${eta != null ? `<span>\u9884\u8ba1\u5269\u4f59 \u2248${fmtDur(eta)}</span>` : ""}
      ${next ? `<span class="po-next">\u2192 ${esc(next)}</span>` : ""}
      ${detail ? `<span class="po-detail">${esc(detail)}</span>` : ""}
    </div>
  </div>`;
}

// ---- Start ----
$("#btnStart").addEventListener("click", async () => {
  const cfg = buildConfig();
  if (window._batchModels && window._batchModels.size > 0) { batchStart(cfg); return; }
  if (!cfg.base_url) { toast("请填写接口地址 Base URL", "warn");return; }
  if (cfg.accuracy_datasets.length === 0 && !cfg.run_performance && cfg.context_lengths.length === 0) {
    toast("请至少选择一个精度数据集、性能压测或上下文扫描", "warn");return;
  }
  if (!["openai", "vllm", "anthropic"].includes(cfg.api_format)) {
    toast("正式评测/压测仅支持 OpenAI 兼容 / Anthropic 接口。Ollama 原生和 Completions 可用于连通性测试，但不能直接进入 evalscope。", "info");
    return;
  }
  // reset
  $("#console").innerHTML = "";
  $("#accResults").innerHTML = "";
  $("#perfResults").innerHTML = "";
  $("#ctxResults").innerHTML = "";
  _currentConfig = cfg;
  _lastProgress = null;
  // Init progress overview
  const po = document.getElementById("progressOverview");
  if (po) po.innerHTML = '<div class="po-track"><div class="po-head"><span class="po-icon">\u25cf</span><span class="po-title">\u4efb\u52a1\u542f\u52a8\u4e2d...</span></div><div class="po-bar-wrap"><div class="po-bar"><div class="po-bar-fill" style="width:0%"></div></div><span class="po-pct">0%</span></div></div>';
  sweepRows = [];
  $("#accEmpty").style.display = "block";
  $("#perfEmpty").style.display = "block";
  $("#ctxEmpty").style.display = "block";
  // clear config panel
  const cp = document.getElementById("configPanel");
  if (cp) cp.remove();
  $("#btnStart").style.display = "none";
  $("#btnStop").style.display = "block";
  setStatus("运行中", "running");
  // switch to live tab
  const _lt = document.querySelector('[data-tab="live"]');
  if (_lt) _lt.click();

  try {
    const r = await fetch("/api/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    const data = await r.json();
    if (!r.ok) { throw new Error(data.detail || "启动失败"); }
    currentTaskId = data.task_id;
    if (data.queue_position > 0) {
      addLog("warn", `当前已有任务在运行，本任务已排队（位置 #${data.queue_position}），前面任务完成后会自动启动。`);
      const po = document.getElementById("progressOverview");
      const qLen = data.queue_length || 0;
      if (po) po.innerHTML = `<div class="po-track"><div class="po-head"><span class="po-icon">⏳</span><span class="po-title">排队中 · 位置 #${data.queue_position} / 共 ${qLen} 个排队</span></div><div class="po-bar-wrap"><div class="po-bar"><div class="po-bar-fill" style="width:0%"></div></div><span class="po-pct">等待</span></div><div class="po-meta"><span>最多同时运行 3 个任务，前面的完成后自动启动</span></div></div>`;
    }
    const st = _ensureStream(currentTaskId);
    st.config = cfg;
    switchLiveTask(currentTaskId);
  } catch (e) {
    addLog("error", "启动失败：" + e.message);
    resetButtons();
    setStatus("错误", "error");
  }
});

$("#btnStop").addEventListener("click", async () => {
  if (!currentTaskId) return;
  if (!confirm("确定停止当前测评？已完成的进度会保存，之后可续跑。")) return;
  await fetch("/api/stop/" + currentTaskId, { method: "POST" });
  addLog("warn", "已发送停止信号，等待当前任务结束…");
});

function resetButtons() {
  const anyRunning = Object.values(_taskStreams).some(s => !s.done);
  $("#btnStart").style.display = anyRunning ? "none" : "block";
  $("#btnStop").style.display = anyRunning ? "block" : "none";
}

// ---- SSE stream ----
function connectStream(taskId) {
  const st = _ensureStream(taskId);
  switchLiveTask(taskId);
}

function _ensureStream(taskId) {
  if (_taskStreams[taskId]) return _taskStreams[taskId];
  const st = { es: null, logs: [], progress: null, sweepRows: [], result: null, done: false, mode: "" };
  let streamUrl = "/api/stream/" + taskId;
  try {
    const tk = localStorage.getItem("evalbench_token");
    if (tk) streamUrl += "?token=" + encodeURIComponent(tk);
  } catch (e) {}
  st.es = new EventSource(streamUrl);
  st.es.onopen = () => {
    st._connWarned = false;  // 重连成功后复位告警标记
    if (taskId === currentTaskId && !st.done) setStatus("运行中", "running");
  };
  st.es.onmessage = (e) => {
    try {
      _handleStreamEvent(taskId, JSON.parse(e.data));
    } catch (err) { /* 单条事件损坏不影响后续事件 */ }
  };
  st.es.onerror = () => {
    if (st.done) { st.es.close(); return; }  // 任务已结束：服务端关流属正常
    // 运行中连接中断：保留 EventSource 原生自动重连，仅提示一次
    if (!st._connWarned) {
      st._connWarned = true;
      if (taskId === currentTaskId) {
        addLog("warn", "实时连接中断，正在自动重连…");
        setStatus("重连中…", "running");
      }
    }
  };
  _taskStreams[taskId] = st;
  return st;
}

function switchLiveTask(taskId) {
  if (currentTaskId === taskId) return;
  currentTaskId = taskId;
  const st = _taskStreams[taskId];
  if (!st) return;
  _currentConfig = st.config || null;
  _lastProgress = st.progress || null;

  const con = $("#console");
  if (con) {
    con.innerHTML = "";
    for (const l of st.logs) addLog(l.level, l.msg, l.ts);
    if (!st.logs.length) con.innerHTML = '<div class="console-empty">等待任务启动...</div>';
  }
  if (st.progress) updateProgressOverview(st.progress);
  else {
    const po = document.getElementById("progressOverview");
    if (po) po.innerHTML = '<div class="po-track"><div class="po-head"><span class="po-icon">⏳</span><span class="po-title">等待中...</span></div><div class="po-bar-wrap"><div class="po-bar"><div class="po-bar-fill" style="width:0%"></div></div><span class="po-pct">0%</span></div></div>';
  }
  if (!st.done) {
    $("#btnStop").style.display = "block";
    setStatus("运行中", "running");
  } else {
    setStatus("完成", "done");
    $("#btnStop").style.display = "none";
  }
  if (st.sweepRows.length) renderPerf({ sweep: st.sweepRows });
  if (st.result) {
    renderResults(st.result);
  } else {
    // Clear result panes if no result yet
    $("#accResults").innerHTML = ""; $("#perfResults").innerHTML = ""; $("#ctxResults").innerHTML = "";
    $("#accEmpty").style.display = "block"; $("#perfEmpty").style.display = "block"; $("#ctxEmpty").style.display = "block";
  }
  _renderTaskSwitcher();
}

function _handleStreamEvent(taskId, ev) {
  const st = _taskStreams[taskId];
  if (!st) return;
  const isActive = taskId === currentTaskId;

  if (ev.type === "log") {
    st.logs.push({ level: ev.level, msg: ev.msg, ts: ev.ts });
    if (st.logs.length > 2500) st.logs.splice(0, st.logs.length - 2000);  // 内存日志设上限
    if (isActive) addLog(ev.level, ev.msg, ev.ts);
  } else if (ev.type === "progress") {
    st.progress = ev;
    st.mode = ev.stage || "";
    if (isActive) updateProgressOverview(ev);
    if (ev.stage === "accuracy") {
      const tab = document.querySelector('[data-tab="accuracy"]');
      if (tab) { const b = tab.querySelector('.tab-badge'); if (b) b.textContent = ev.dataset || ''; }
    } else if (ev.stage === "performance") {
      const tab = document.querySelector('[data-tab="perf"]');
      if (tab) { const b = tab.querySelector('.tab-badge'); if (b) b.textContent = '运行中'; }
    } else if (ev.stage === "context_scan") {
      const tab = document.querySelector('[data-tab="ctx"]');
      if (tab) { const b = tab.querySelector('.tab-badge'); if (b) b.textContent = '运行中'; }
    }
  } else if (ev.type === "sweep_level") {
    st.sweepRows.push(ev.row);
    if (isActive) renderPerf({ sweep: st.sweepRows });
  } else if (ev.type === "done") {
    st.done = true;
    st.es.close();  // 任务结束，关闭连接避免累积长连接
    st.result = ev.result;
    if (ev.stopped) flashTitle("⏹ 任务已停止 · LLM Eval Bench");
    else { flashTitle("✅ 评测完成 · LLM Eval Bench"); toast("评测完成，结果已更新", "success"); }
    loadBoard();
    if (isActive) {
      if (ev.stopped) { setStatus("已停止", "error"); addLog("warn", "测评已停止"); }
      else { setStatus("完成", "done"); addLog("success", "全部测评完成 ✓"); }
      _reviewTaskId = taskId;
      renderResults(ev.result);
      resetButtons();
      // 完成后自动跳到有结果的页签（board/live/accuracy/ctx/perf）
      const _res = ev.result || {};
      const _hasAcc = _res.accuracy && Object.keys(_res.accuracy).length;
      const _hasPerf = _res.performance && Object.keys(_res.performance).length;
      const _hasCtx = _res.context_scan && (_res.context_scan.sweep || []).length;
      const tabs = $$(".tab");
      if (_hasAcc && tabs[2]) tabs[2].click();
      else if (_hasPerf && tabs[4]) tabs[4].click();
      else if (_hasCtx && tabs[3]) tabs[3].click();
    }
    if (drawer.classList.contains("open")) refreshDrawerList();
    _renderTaskSwitcher();
  } else if (ev.type === "error") {
    st.done = true;
    st.es.close();  // 任务结束，关闭连接避免累积长连接
    if (isActive) { addLog("error", ev.msg); setStatus("错误", "error"); resetButtons(); }
    _renderTaskSwitcher();
  }
}

function _renderTaskSwitcher() {
  const running = Object.entries(_taskStreams).filter(([_, s]) => !s.done);
  const bar = document.getElementById("taskSwitcher");
  if (!bar) return;
  if (running.length <= 1) { bar.style.display = "none"; return; }
  bar.style.display = "flex";
  bar.innerHTML = running.map(([tid, s]) => {
    const cfg = s.config || {};
  const name = (cfg.model || tid).slice(0, 14);
    const modeLabel = s.mode === "accuracy" ? "\u7cbe\u5ea6" : s.mode === "performance" ? "\u6027\u80fd" : s.mode === "context_scan" ? "\u4e0a\u4e0b\u6587" : "\u542f\u52a8\u4e2d";
    const active = tid === currentTaskId ? " active" : "";
    return `<button class="ts-btn${active}" data-tid="${tid}">
      <span class="ts-name">${esc(name)}</span>
      <span class="ts-mode">${modeLabel}</span>
      <span class="ts-dot">\u25cf</span>
    </button>`;
  }).join("");
  bar.querySelectorAll(".ts-btn").forEach(btn => {
    btn.addEventListener("click", () => switchLiveTask(btn.dataset.tid));
  });
}

function _percentileGrid(obj, unit) {
  if (!obj || Object.keys(obj).length === 0) return "";
  const fmt = (v) => {
    if (v == null) return "-";
    if (unit === "s") return v.toFixed(2);
    if (unit === "tok") return v.toFixed(0);
    if (unit === "tps") return v.toFixed(1);
    if (unit === "rps") return v.toFixed(3);
    return String(v);
  };
  return `<div class="mini-grid">
    <span>min</span><b>${fmt(obj.min)}</b>
    <span>P25</span><b>${fmt(obj["25%"])}</b>
    <span>P50</span><b>${fmt(obj["50%"])}</b>
    <span>P75</span><b>${fmt(obj["75%"])}</b>
    <span>P90</span><b>${fmt(obj["90%"])}</b>
    <span>P99</span><b>${fmt(obj["99%"])}</b>
    <span>max</span><b>${fmt(obj.max)}</b>
    <span>mean ± std</span><b>${fmt(obj.mean)}${obj.std != null ? " ± " + fmt(obj.std) : ""}</b>
  </div>`;
}

// ---- Render final results ----
function renderResults(result) {
  if (result.accuracy && Object.keys(result.accuracy).length) renderAccuracy(result.accuracy);
  if (result.performance && Object.keys(result.performance).length) renderPerf(result.performance);
  if (result.context_scan && result.context_scan.sweep && result.context_scan.sweep.length) {
    renderContextScan(result.context_scan);
  }
}

function renderContextScan(cs) {
  $("#ctxEmpty").style.display = "none";
  const root = $("#ctxResults");
  const rows = cs.sweep;
  root.innerHTML = "";
  const block = document.createElement("div");
  block.className = "result-block";

  // Build table
  let head = `<tr><th>上下文长度</th><th>RPS</th><th>输出 tok/s</th><th>TTFT</th><th>TPOT</th><th>平均延迟</th><th>P99</th><th>成功率</th></tr>`;
  let body = rows.map(r => {
    const errCls = r.error ? "risk-row" : "";
    return `<tr class="${errCls}"><td>${fmtSize(r.context_length)}</td>
      <td>${r.error ? "ERR" : fmtMetric(r.rps)}</td>
      <td>${fmtMetric(r.output_tps)}</td>
      <td>${r.ttft_avg != null ? r.ttft_avg + "s" : "-"}</td>
      <td>${r.tpot_avg_ms != null ? r.tpot_avg_ms + "ms" : "-"}</td>
      <td>${r.latency_avg != null ? r.latency_avg + "s" : "-"}</td>
      <td>${r.latency_p99 != null ? r.latency_p99 + "s" : "-"}</td>
      <td>${r.success_rate != null ? r.success_rate + "%" : "-"}</td></tr>`;
  }).join("");

  // Build chart: context_length vs TTFT and P99
  const chart = ctxSweepChart(rows);

  block.innerHTML = `<h3>上下文长度扫描 · 固定并发 ${cs.concurrency}</h3>
    ${chart}
    <div class="table-scroll"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>
    <div class="diag-info">每档 ${cs.requests_per_level} 请求 · max_tokens 256 · 流式</div>`;
  root.appendChild(block);
}

function ctxSweepChart(rows) {
  const W = 720, H = 280, padL = 60, padR = 52, padT = 24, padB = 50;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const valid = rows.filter(r => !r.error);
  if (!valid.length) return "<div class='diag-warn'>所有档位均失败，无法绘制图表</div>";
  const xs = valid.map(r => r.context_length);
  const maxX = Math.max(...xs), minX = Math.min(...xs);
  const maxRps = Math.max(...valid.map(r => r.rps || 0), 0.1);
  const maxLat = Math.max(...valid.map(r => r.latency_p99 || r.latency_avg || 0), 0.1);
  const maxTtft = Math.max(...valid.map(r => r.ttft_avg || 0), 0.1);

  const xPos = (x) => padL + (maxX === minX ? plotW / 2 : (x - minX) / (maxX - minX) * plotW);
  const yRps = (v) => padT + plotH - (v / maxRps) * plotH;
  const yLat = (v) => padT + plotH - (v / maxLat) * plotH;
  const yTtft = (v) => padT + plotH - (v / maxTtft) * plotH;

  const line = (pts, color, w) =>
    `<polyline points="${pts.map(p => p.join(",")).join(" ")}" fill="none"
      stroke="${color}" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round"/>`;
  const dots = (pts, color, titleFn) => pts.map((p, i) =>
    `<circle cx="${p[0]}" cy="${p[1]}" r="3.5" fill="${color}" stroke="#ffffff" stroke-width="1.5">${titleFn ? `<title>${esc(titleFn(i))}</title>` : ""}</circle>`).join("");

  const rpsPts = valid.map(r => [xPos(r.context_length), yRps(r.rps || 0)]);
  const p99Pts = valid.map(r => [xPos(r.context_length), yLat(r.latency_p99 || r.latency_avg)]);
  const ttftPts = valid.map(r => [xPos(r.context_length), yTtft(r.ttft_avg || 0)]);

  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const y = padT + (plotH / 4) * i;
    grid += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="#e4e4e7" stroke-width="1"/>`;
    grid += `<text x="${padL - 8}" y="${y + 4}" text-anchor="end" fill="#4f46e5" font-size="9" font-family="monospace">${(maxRps * (1 - i / 4)).toFixed(1)}</text>`;
  }
  const xlabels = valid.map(r =>
    `<text x="${xPos(r.context_length)}" y="${H - padB + 18}" text-anchor="middle" fill="#71717a" font-size="10" font-family="monospace">${fmtSize(r.context_length)}</text>`).join("");

  return `<div class="chart-legend">
    <span class="lg"><i style="background:#4f46e5"></i>RPS</span>
    <span class="lg"><i style="background:#18181b"></i>P99</span>
    <span class="lg"><i style="background:#d97706"></i>TTFT</span>
    <span class="lg-axis">左轴 RPS · 右轴 延迟(s)</span>
  </div>
  <svg viewBox="0 0 ${W} ${H}" class="sweep-svg" xmlns="http://www.w3.org/2000/svg">
    ${grid}
    ${line(p99Pts, "#18181b", 1.8)}
    ${line(ttftPts, "#d97706", 1.8)}
    ${line(rpsPts, "#4f46e5", 2.4)}
    ${dots(p99Pts, "#18181b", i => `上下文 ${fmtSize(valid[i].context_length)} · P99 ${fmtMetric(valid[i].latency_p99 ?? valid[i].latency_avg)}s`)}
    ${dots(ttftPts, "#d97706", i => `上下文 ${fmtSize(valid[i].context_length)} · TTFT ${fmtMetric(valid[i].ttft_avg)}s`)}
    ${dots(rpsPts, "#4f46e5", i => `上下文 ${fmtSize(valid[i].context_length)} · RPS ${fmtMetric(valid[i].rps)}`)}
    ${xlabels}
    <text x="${W / 2}" y="${H - 6}" text-anchor="middle" fill="#71717a" font-size="11" font-family="monospace">上下文长度 (tokens)</text>
  </svg>`;
}

function fmtSize(n) {
  if (!n) return "-";
  if (n >= 1024) return (n / 1024).toFixed(1) + "K";
  return String(n);
}

function renderAccuracy(acc) {
  $("#accEmpty").style.display = "none";
  const root = $("#accResults");
  root.innerHTML = "";

  // ── Build aggregate metric cards across all datasets ──
  const dsList = Object.entries(acc);
  const totalSubjects = dsList.reduce((n, [,r]) => n + Object.keys(r.by_subject || {}).length, 0);
  const totalCats = dsList.reduce((n, [,r]) => n + Object.keys(r.by_category || {}).length, 0);
  const totalQuestions = dsList.reduce((n, [,r]) => n + (r.num || r.total || 0), 0);
  // Weighted average accuracy
  let totalWeight = 0, weightedAcc = 0;
  for (const [,r] of dsList) {
    const w = r.num || r.total || 0;
    if (w && r.accuracy != null) { weightedAcc += r.accuracy * w; totalWeight += w; }
  }
  const avgAcc = totalWeight > 0 ? (weightedAcc / totalWeight).toFixed(1) : null;
  // Collect perf metrics from first dataset that has them
  let pm = null;
  for (const [,r] of dsList) { if (r.perf_metrics) { pm = r.perf_metrics; break; } }
  const cards = [];
  if (avgAcc != null) {
    cards.push({ label: "加权平均准确率", value: avgAcc + "%",
      sub: `${totalQuestions} 题 · ${dsList.length} 数据集` });
  }
  cards.push(
    { label: "数据集", value: dsList.length,
      sub: dsList.map(([k]) => k).join(" · ").slice(0, 60) },
    { label: "学科/子集", value: totalSubjects,
      sub: totalCats ? `${totalCats} 个类别` : "" },
  );
  if (pm) {
    const lat = pm.latency || {};
    const tp = pm.throughput || {};
    cards.push(
      { label: "平均延迟", value: lat.mean != null ? lat.mean.toFixed(2) + "s" : "-",
        sub: `P99 ${lat["99%"] != null ? lat["99%"].toFixed(2) + "s" : "-"}` },
      { label: "输出吞吐", value: tp.avg_output_tps != null ? tp.avg_output_tps.toFixed(1) + " tok/s" : "-",
        sub: pm.n_samples ? `${pm.n_samples} 样本` : "" },
    );
  }
  let cardsHtml = "";
  if (cards.length) {
    cardsHtml = `<div class="metric-grid">` +
      cards.map(c => `<div class="metric">
        <div class="ml">${esc(c.label)}</div>
        <div class="mv">${esc(String(c.value))}</div>
        ${c.sub ? `<div style="font-size:10.5px;color:var(--ink-faint);margin-top:4px;font-family:var(--mono)">${esc(c.sub)}</div>` : ""}
      </div>`).join("") + `</div>`;
    root.innerHTML += cardsHtml;
  }

  for (const [ds, r] of Object.entries(acc)) {
    const block = document.createElement("div");
    block.className = "result-block";

    const shotLabel = r.few_shot ? `${r.few_shot}-shot` : "0-shot";
    const accDisplay = r.accuracy != null ? `${r.accuracy}%` : "见详情";
    const sc = scoreColor(r.accuracy ?? 0);
    const nQuestions = r.num != null ? ` · ${r.num} 题` : (r.total ? ` · 约 ${r.total} 题` : "");

    // ── Dataset description ──
    let descHtml = "";
    if (r.dataset_description) {
      descHtml = `<div class="ds-desc">${esc(r.dataset_description)}</div>`;
    }

    // ── Named metrics (evalscope reports multiple metrics per dataset) ──
    let metricsHtml = "";
    const rawMetrics = (r.raw || {}).metrics || [];
    if (rawMetrics.length > 1) {
      let mRows = rawMetrics.map(m => {
        const mn = m.name || "-";
        const ms = m.score != null ? (typeof m.score === "number" ? (m.score * 100).toFixed(1) + "%" : String(m.score)) : "-";
        return `<span class="metric-chip"><b>${esc(String(mn))}</b> ${ms}</span>`;
      }).join("");
      metricsHtml = `<div class="acc-metrics-row">${mRows}</div>`;
    }

    // ── Subject breakdown table ──
    let subjTable = "";
    const subjs = Object.entries(r.by_subject || {}).sort((a, b) => b[1] - a[1]);
    if (subjs.length) {
      let rows = "";
      for (const [s, v] of subjs) {
        const sc = scoreColor(v);
        // Find num questions from raw data
        let subjNum = "";
        for (const m of rawMetrics) {
          for (const cat of m.categories || []) {
            for (const sub of cat.subsets || []) {
              if (sub.name === s) { subjNum = sub.num || ""; break; }
            }
          }
        }
        rows += `<tr><td class="subj">${esc(s)}</td><td>${subjNum ? subjNum + "题" : ""}</td><td>
          <div class="bar-cell"><span style="color:${sc}">${v}%</span>
          <div class="bar-track"><div class="bar-val" style="width:${Math.min(v,100)}%;background:${sc}"></div></div></div>
          </td></tr>`;
      }
      subjTable = `<details class="result-mini">
        <summary>学科 / 子集得分 (${subjs.length})</summary>
        <div class="table-scroll"><table><thead><tr><th>学科</th><th>题目</th><th>准确率</th></tr></thead><tbody>${rows}</tbody></table></div>
      </details>`;
    }

    // ── Category breakdown ──
    let catTable = "";
    const cats = Object.entries(r.by_category || {}).sort((a, b) => (b[1].score || 0) - (a[1].score || 0));
    if (cats.length) {
      let rows = "";
      for (const [cn, cv] of cats) {
        const cs = cv.score != null ? `${cv.score}%` : "-";
        const ms = cv.macro_score != null ? `${cv.macro_score}%` : "-";
        const sc = scoreColor(cv.score ?? 0);
        rows += `<tr><td class="subj">${esc(cn)}</td>
          <td><span class="badge sm" style="background:${sc};color:#fff">${cs}</span></td>
          <td>${ms}</td>
          <td>${cv.num != null ? cv.num : "-"}</td></tr>`;
      }
      catTable = `<details class="result-mini">
        <summary>类别得分 (${cats.length})</summary>
        <div class="table-scroll"><table><thead><tr><th>类别</th><th>准确率</th><th>macro</th><th>题目数</th></tr></thead><tbody>${rows}</tbody></table></div>
      </details>`;
    }

    // ── Perf metrics summary ──
    let perfHtml = "";
    if (r.perf_metrics) {
      const pm = r.perf_metrics;
      const lat = pm.latency || {};
      const tp = pm.throughput || {};
      const usage = pm.usage || {};
      const inTok = usage.input_tokens || {};
      const outTok = usage.output_tokens || {};
      const totTok = usage.total_tokens || {};

      // Overview
      let overview = `<div class="mini-grid">
        <span>样本数</span><b>${pm.n_samples || "-"}</b>
        <span>输出 tok/s</span><b>${tp.avg_output_tps != null ? tp.avg_output_tps.toFixed(1) : "-"}</b>
        <span>请求/秒</span><b>${tp.avg_req_ps != null ? tp.avg_req_ps.toFixed(3) : "-"}</b>`;
      if (usage.total_input_tokens != null) {
        overview += `<span>总输入 tok</span><b>${usage.total_input_tokens.toLocaleString()}</b>`;
      }
      if (usage.total_output_tokens != null) {
        overview += `<span>总输出 tok</span><b>${usage.total_output_tokens.toLocaleString()}</b>`;
      }
      if (usage.total_tokens_count != null) {
        overview += `<span>总 tok 数</span><b>${usage.total_tokens_count.toLocaleString()}</b>`;
      }
      overview += `</div>`;

      // Latency distribution
      let latSection = "";
      if (lat && Object.keys(lat).length > 0) {
        latSection = `<details class="result-mini">
          <summary>延迟分布 (秒)</summary>
          ${_percentileGrid(lat, "s")}
        </details>`;
      }

      // Token usage distribution
      let tokSection = "";
      if (inTok && Object.keys(inTok).length > 0) {
        tokSection = `<details class="result-mini">
          <summary>Token 用量分布</summary>
          <div style="margin-bottom:6px;color:var(--ink-dim);font-size:11px;font-weight:500">输入 Token</div>
          ${_percentileGrid(inTok, "tok")}`;
        if (outTok && Object.keys(outTok).length > 0) {
          tokSection += `<div style="margin:14px 0 6px;color:var(--ink-dim);font-size:11px;font-weight:500">输出 Token</div>
          ${_percentileGrid(outTok, "tok")}`;
        }
        if (totTok && Object.keys(totTok).length > 0) {
          tokSection += `<div style="margin:14px 0 6px;color:var(--ink-dim);font-size:11px;font-weight:500">总计 Token</div>
          ${_percentileGrid(totTok, "tok")}`;
        }
        tokSection += `</details>`;
      }

      perfHtml = `<details class="result-mini">
        <summary>推理性能统计</summary>
        ${overview}
        ${latSection}
        ${tokSection}
      </details>`;
    }

    // ── Analysis text ──
    let analysisHtml = "";
    if (r.analysis && r.analysis.trim()) {
      analysisHtml = `<details class="result-mini">
        <summary>AI 分析报告</summary>
        <div class="analysis-text">${esc(r.analysis)}</div>
      </details>`;
    }

    // ── Reproduce config ──
    const repro = r.repro || {};
    const reproHtml = repro && Object.keys(repro).length ? `
      <details class="result-mini">
        <summary>复现配置</summary>
        <div class="mini-grid">
          <span>few-shot</span><b>${repro.few_shot ?? r.few_shot ?? 0}</b>
          <span>抽样上限</span><b>${repro.sample_limit || "全量"}</b>
          <span>max_tokens</span><b>${repro.max_tokens ?? "-"}</b>
          <span>temperature</span><b>${repro.temperature ?? "-"}</b>
          <span>输出目录</span><b>${esc(repro.output_dir || "-")}</b>
        </div>
      </details>` : "";

    // ── Wrong samples ──
    let wrong = "";
    if (r.wrong_samples && r.wrong_samples.length) {
      wrong = `<details class="wrong-samples"><summary>错题样本 (${r.wrong_samples.length})</summary>` +
        r.wrong_samples.map(w => `<div class="ws-item">
          <div class="q">Q: ${esc(w.question)}</div>
          <div>正确: <span class="exp">${esc(w.expected)}</span> · 模型: <span class="got">${esc(w.got)}</span></div>
          ${w.raw ? `<div class="ws-raw">原始输出: ${esc(w.raw)}</div>` : ""}
        </div>`).join("") + `</details>`;
    }

    block.innerHTML = `
      <h3>${esc(ds)} <span class="badge" style="color:${sc}">${accDisplay}</span>
        <span style="font-size:12px;color:var(--ink-dim);font-family:var(--mono)">
        ${shotLabel} · evalscope${nQuestions}</span>
        <button class="btn-review-samples" data-ds="${esc(ds)}" style="margin-left:auto;font-size:11px;padding:4px 12px;border-radius:14px;background:var(--bg-3);border:1px solid var(--line);color:var(--ink-dim);cursor:pointer;font-family:var(--sans);transition:.15s">查看答题详情 →</button></h3>
      ${descHtml}
      ${metricsHtml}
      ${catTable}
      ${subjTable}
      ${perfHtml}
      ${analysisHtml}
      ${reproHtml}
      ${wrong}`;
    root.appendChild(block);
  }
  // 精度指标说明
  const guide = document.createElement("details");
  guide.className = "metric-guide";
  guide.innerHTML = `<summary>精度指标说明（点击展开）</summary>
    <div class="mg-grid">
      <div class="mg-item"><b>准确率</b> 由 evalscope 按各数据集官方判分方式计算，对标官方榜单。</div>
      <div class="mg-item"><b>学科 / 子集</b> evalscope 按数据集内置的 subset 分项给出各领域准确率。</div>
      <div class="mg-item"><b>类别</b> 数据集内置的学科大类（如 Humanities / STEM / Social Science），含 macro_score 等聚合指标。</div>
      <div class="mg-item"><b>Few-shot</b> 提示示例数。对标 C-Eval/MMLU 官方分数通常用 5-shot；0 为 zero-shot。</div>
      <div class="mg-item"><b>推理性能统计</b> 评测过程中采集的延迟分位数、吞吐量和 token 用量分布。</div>
      <div class="mg-item"><b>关于权威性</b> 评测由业界标准工具 evalscope 执行，判分方式与官方一致，分数可对标公开榜单。</div>
    </div>`;
  root.appendChild(guide);
}

function renderPerf(p) {
  const rows = p.sweep || [];
  if (!rows.length) return;
  $("#perfEmpty").style.display = "none";
  const root = $("#perfResults");

  const best = p.best || rows.reduce((a, b) => (b.rps > a.rps ? b : a), rows[0]);
  const lowLat = p.lowest_latency;
  const rec = p.recommend;
  const warnings = p.warnings || [];
  const profile = p.profile || {};
  const hasTtft = rows.some(r => r.ttft_avg != null);
  const hasTpot = rows.some(r => r.tpot_avg_ms != null);
  const hasItl = rows.some(r => r.itl_avg_ms != null && r.itl_avg_ms > 0);
  const hasTotalTps = rows.some(r => r.total_tps != null);
  const hasInputTps = rows.some(r => r.input_tps != null && r.input_tps > 0);
  const hasP50 = rows.some(r => r.latency_p50 != null);
  const hasMax = rows.some(r => r.latency_max != null);

  const chart = sweepChart(rows);

  // 各档位摘要卡片
  const cards = rows.map(r => {
    const isBest = r.concurrency === best.concurrency;
    return `<div class="perf-level-card${isBest ? " best" : ""}">
      <div class="plc-head"><span class="plc-conc">并发 ${r.concurrency}</span>${isBest ? '<span class="plc-badge">吞吐最优</span>' : ""}</div>
      <div class="plc-metrics">
        <div class="plc-m"><span class="plc-v">${fmtMetric(r.rps)}</span><span class="plc-l">RPS</span></div>
        <div class="plc-m"><span class="plc-v">${fmtMetric(r.latency_avg)}s</span><span class="plc-l">平均延迟</span></div>
        <div class="plc-m"><span class="plc-v">${fmtMetric(r.latency_p99)}s</span><span class="plc-l">P99 延迟</span></div>
        ${r.ttft_avg != null ? `<div class="plc-m"><span class="plc-v">${fmtMetric(r.ttft_avg)}s</span><span class="plc-l">TTFT</span></div>` : ""}
        <div class="plc-m"><span class="plc-v">${r.success_rate != null ? r.success_rate + "%" : "-"}</span><span class="plc-l">成功率</span></div>
      </div>
    </div>`;
  }).join("");

  // 详细性能指标表
  let head = `<tr>
    <th>并发</th>
    <th title="每秒完成请求数">RPS</th>
    ${hasTotalTps ? '<th title="输入+输出的总 token 吞吐">总tok/s</th>' : ""}
    <th title="输出 token 吞吐">输出tok/s</th>
    ${hasInputTps ? '<th title="输入 token 吞吐">输入tok/s</th>' : ""}
    ${hasTpot ? '<th title="每输出 token 平均耗时(ms)">TPOT</th>' : ""}
    ${hasItl ? '<th title="Token 间延迟(ms)，反映生成停顿">ITL</th>' : ""}
    <th title="单请求平均耗时">延迟avg</th>
    ${hasP50 ? '<th title="50%请求延迟(中位数)">P50</th>' : ""}
    <th title="90%请求延迟低于此值">P90</th>
    <th title="99%请求延迟低于此值">P99</th>
    ${hasMax ? '<th title="最慢请求延迟">Max</th>' : ""}
    ${hasTtft ? '<th title="首 token 平均耗时">TTFT</th>' : ""}
    ${hasTtft ? '<th title="首 token P99">TTFT P99</th>' : ""}
    <th>入tok</th><th>出tok</th>
    <th title="请求成功率">成功率</th></tr>`;
  let body = rows.map(r => {
    const rowCls = [
      r.concurrency === best.concurrency ? "best-row" : "",
      r.success_rate != null && r.success_rate < 99 ? "risk-row" : "",
    ].filter(Boolean).join(" ");
    const degradCls = r.degraded ? " degrad-row" : "";
    return `<tr class="${rowCls}${degradCls}"${!rowCls && !degradCls ? "" : ""}>
    <td>${r.concurrency}${r.degraded ? ` <span class="degrad-tag" title="成功率显示100%，但${r.degraded_pct}%的请求返回了短/空响应（平均输出仅${r.avg_out_tokens}token）">⚠退化</span>` : ""}</td>
    <td>${fmtMetric(r.rps)}</td>
    ${hasTotalTps ? `<td>${fmtMetric(r.total_tps)}</td>` : ""}
    <td>${fmtMetric(r.output_tps)}</td>
    ${hasInputTps ? `<td>${fmtMetric(r.input_tps)}</td>` : ""}
    ${hasTpot ? `<td>${r.tpot_avg_ms != null ? r.tpot_avg_ms + "ms" : "-"}</td>` : ""}
    ${hasItl ? `<td>${r.itl_avg_ms != null ? r.itl_avg_ms + "ms" : "-"}</td>` : ""}
    <td>${fmtMetric(r.latency_avg)}s</td>
    ${hasP50 ? `<td>${fmtMetric(r.latency_p50)}s</td>` : ""}
    <td>${fmtMetric(r.latency_p90)}s</td>
    <td>${fmtMetric(r.latency_p99)}s</td>
    ${hasMax ? `<td>${fmtMetric(r.latency_max)}s</td>` : ""}
    ${hasTtft ? `<td>${r.ttft_avg != null ? fmtMetric(r.ttft_avg) + "s" : "-"}</td>` : ""}
    ${hasTtft ? `<td>${r.ttft_p99 != null ? fmtMetric(r.ttft_p99) + "s" : "-"}</td>` : ""}
    <td>${r.avg_in_tokens || "-"}</td><td class="${r.degraded ? 'degrad-cell' : ''}">${r.avg_out_tokens || "-"}${r.degraded ? ` <span class="degrad-pct">(${r.degraded_pct}%退化)</span>` : ""}</td>
    <td>${r.success_rate != null ? r.success_rate + "%" : "-"}</td></tr>`;
  }).join("");

  // 推荐配置块
  let recHtml = "";
  if (rec || lowLat) {
    recHtml = `<div class="result-block"><h3>推荐配置</h3><div class="rec-grid">
      <div class="rec-card"><div class="rec-label">最高吞吐</div>
        <div class="rec-val">并发 ${best.concurrency}</div>
        <div class="rec-sub">${best.rps} RPS · ${best.output_tps} tok/s</div></div>
      ${lowLat ? `<div class="rec-card"><div class="rec-label">最低延迟</div>
        <div class="rec-val">并发 ${lowLat.concurrency}</div>
        <div class="rec-sub">${lowLat.latency_avg}s 平均延迟</div></div>` : ""}
      ${rec ? `<div class="rec-card highlight"><div class="rec-label">推荐并发区间</div>
        <div class="rec-val">${rec.min}~${rec.max}</div>
        <div class="rec-sub">${esc(rec.basis || "吞吐达峰值90%以上的稳定区")}</div></div>` : ""}
    </div></div>`;
  }

  const warnHtml = warnings.length ? `<div class="perf-warnings">
    ${warnings.map(w => typeof w === "string" ? `<div class="diag-warn">${esc(w)}</div>` : `<div class="diag-warn"><b>${esc(w.title || "风险提示")}</b>${esc(w.message || "")}</div>`).join("")}
  </div>` : "";
  const profileHtml = Object.keys(profile).length ? `<details class="metric-guide">
    <summary>压测配置快照（点击展开）</summary>
    <div class="mg-grid">
      <div class="mg-item"><b>端点</b> <code style="font-size:10px;word-break:break-all">${esc(profile.url || "-")}</code></div>
      <div class="mg-item"><b>并发档位</b> ${(profile.levels || []).join(", ") || "-"}</div>
      <div class="mg-item"><b>每档请求</b> ${profile.requests_per_level ?? "-"}</div>
      <div class="mg-item"><b>数据集</b> ${esc(profile.dataset || "-")}</div>
      <div class="mg-item"><b>模式</b> ${profile.stream ? "流式" : "非流式"}</div>
      <div class="mg-item"><b>输入长度</b> ${profile.context_length > 0 ? profile.context_length + " tokens（长上下文模式）" : (profile.prompt_text ? esc(profile.prompt_text) : "-")}</div>
      <div class="mg-item"><b>max_tokens</b> ${profile.max_tokens ?? "-"}；<b>min_tokens</b> ${profile.min_tokens || "不限"}</div>
      <div class="mg-item"><b>temperature</b> ${profile.temperature ?? "-"}</div>
      <div class="mg-item"><b>请求超时</b> ${profile.request_timeout != null ? profile.request_timeout + "s" : "默认"}</div>
      <div class="mg-item"><b>预热请求</b> ${profile.warmup_requests || "无"}</div>
    </div>
  </details>` : "";

  const ttftHtml = ttftChart(rows) ? `<div class="result-block">
      <h3>TTFT 首 Token 延迟
        <span class="best-tag">交互体验核心指标</span>
      </h3>
      ${ttftChart(rows)}
    </div>` : "";
  const tpsHtml = tpsChart(rows) ? `<div class="result-block">
      <h3>Token 吞吐量
        <span class="best-tag">每秒处理 token 数</span>
      </h3>
      ${tpsChart(rows)}
    </div>` : "";
  const latencyHtml = latencySpreadChart(rows) ? `<div class="result-block">
      <h3>延迟分位数分布
        <span class="best-tag">P50 / P90 / P99 / Max 随并发的扩散</span>
      </h3>
      ${latencySpreadChart(rows)}
    </div>` : "";

  root.innerHTML = `
    <div class="result-block">
      <h3>各档位性能摘要</h3>
      <div class="perf-level-cards">${cards}</div>
    </div>
    <div class="result-block">
      <h3>并发扫描曲线
        <span class="best-tag">吞吐峰值 · 并发 ${best.concurrency} · ${best.rps} RPS</span>
      </h3>
      ${chart}
    </div>
    ${ttftHtml}
    ${tpsHtml}
    ${latencyHtml}
    ${warnHtml}
    ${recHtml}
    <div class="result-block">
      <h3>性能明细表</h3>
      <div class="table-scroll"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>
    </div>
    <div class="result-block" id="perfReqBlock" data-eval-task-id="${esc(p.eval_task_id || "")}">
      <h3>逐请求明细 <span class="best-tag">来自 benchmark_data.db</span></h3>
      <div class="perf-req-toolbar">
        <select id="perfReqLevel">
          ${rows.map(r => `<option value="${r.concurrency}">并发 ${r.concurrency}</option>`).join("")}
        </select>
        <button class="btn ghost" id="btnLoadPerfReq">加载请求明细</button>
        <span class="perf-req-stats" id="perfReqStats"></span>
      </div>
      <div class="table-scroll" id="perfReqTable" style="display:none">
        <table><thead id="perfReqHead"></thead><tbody id="perfReqBody"></tbody></table>
      </div>
      <div class="perf-req-dist" id="perfReqDist"></div>
    </div>
    ${profileHtml}
    <details class="metric-guide"><summary>性能指标说明（点击展开）</summary>
      <div class="mg-grid">
        <div class="mg-item"><b>RPS</b>（Requests Per Second）每秒完成的请求数，衡量整体吞吐能力，越高越好。</div>
        <div class="mg-item"><b>总 tok/s</b> 每秒处理的输入+输出 token 总数，反映整体 token 处理带宽。</div>
        <div class="mg-item"><b>输出/输入 tok/s</b> 每秒生成的输出/处理的输入 token 数。</div>
        <div class="mg-item"><b>TPOT</b>（Time Per Output Token）生成每个输出 token 的平均耗时（毫秒），越低生成越快。</div>
        <div class="mg-item"><b>ITL</b>（Inter-Token Latency）两个连续 token 之间的延迟（毫秒），反映生成流畅度。值高说明生成有停顿。</div>
        <div class="mg-item"><b>TTFT</b>（Time To First Token）从发出请求到收到第一个字的耗时，交互体验的核心指标。</div>
        <div class="mg-item"><b>延迟 avg/P50/P90/P99/Max</b> 请求总耗时的统计分布。P50=中位数，P99=极端慢请求，Max=最差情况。</div>
        <div class="mg-item"><b>输入/输出 tok</b> 单请求的平均输入、输出 token 数。</div>
        <div class="mg-item"><b>成功率</b> 成功请求占比。低于 99% 的档位不建议用于生产。</div>
        <div class="mg-item"><b>推荐并发区间</b> 综合吞吐、成功率和 P99 延迟给出的稳定区间。</div>
      </div>
    </details>`;
}

async function loadPerfRequests() {
  const block = document.getElementById("perfReqBlock");
  if (!block) return;
  const evalTaskId = block.dataset.evalTaskId;
  const level = parseInt(document.getElementById("perfReqLevel")?.value) || 0;
  if (!evalTaskId) { toast("缺少 eval_task_id", "warn");return; }

  const stats = document.getElementById("perfReqStats");
  const table = document.getElementById("perfReqTable");
  const head = document.getElementById("perfReqHead");
  const body = document.getElementById("perfReqBody");
  const dist = document.getElementById("perfReqDist");
  const btn = document.getElementById("btnLoadPerfReq");

  btn.disabled = true; btn.textContent = "加载中...";
  stats.textContent = "";

  try {
    const resp = await fetch(`/api/tasks/_/perf-requests?eval_task_id=${encodeURIComponent(evalTaskId)}&level=${level}`);
    if (!resp.ok) { const t = await resp.text(); toast("加载失败：" + t, "error");return; }
    const data = await resp.json();

    let allRecords = [];
    const itlLevels = [];
    for (const [levelName, lv] of Object.entries(data.levels || {})) {
      // 新版响应：{records, itl_stats}；兼容旧版纯数组
      const records = Array.isArray(lv) ? lv : (lv.records || []);
      if (records.length && records[0].error) continue;
      for (const r of records) {
        r._level = levelName;
        allRecords.push(r);
      }
      if (lv && lv.itl_stats && lv.itl_stats.count) itlLevels.push({ level: levelName, ...lv.itl_stats });
    }

    if (!allRecords.length) {
      stats.textContent = "无请求数据";
      return;
    }

    stats.textContent = `共 ${allRecords.length} 条请求 · ${Object.keys(data.levels || {}).length} 个档位`;

    head.innerHTML = `<tr>
      <th>#</th><th>档位</th><th>输入tok</th><th>输出tok</th>
      <th>TTFT</th><th>TPOT</th><th>总延迟</th>
      <th>ITL 数</th><th>成功</th><th>提示词</th></tr>`;
    body.innerHTML = allRecords.map((r, i) => `<tr class="${r.success ? "" : "risk-row"}">
      <td>${i + 1}</td><td>${esc(r._level)}</td>
      <td>${r.prompt_tokens ?? "-"}</td><td>${r.completion_tokens ?? "-"}</td>
      <td>${r.ttft != null ? fmtMetric(r.ttft) + "s" : "-"}</td>
      <td>${r.tpot != null ? fmtNum(r.tpot) + "s" : "-"}</td>
      <td>${r.latency != null ? fmtMetric(r.latency) + "s" : "-"}</td>
      <td>${r.itl_count || 0}</td>
      <td>${r.success ? "✓" : "✗"}</td>
      <td style="max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.prompt_preview || "")}">${esc(r.prompt_preview || "-")}</td>
    </tr>`).join("");

    table.style.display = "block";

    // ITL 分布：后端已按档聚合（avg/P50/P90/P99/max），前端只做加权合并展示
    if (itlLevels.length) {
      const totalCount = itlLevels.reduce((s, x) => s + x.count, 0);
      const wAvg = itlLevels.reduce((s, x) => s + x.avg * x.count, 0) / totalCount;
      const p99Max = Math.max(...itlLevels.map(x => x.p99 ?? 0));
      const mx = Math.max(...itlLevels.map(x => x.max ?? 0));
      const p50Max = Math.max(...itlLevels.map(x => x.p50 ?? 0));
      const levelLines = itlLevels.map(s => {
        const conc = (s.level.match(/^parallel_(\d+)_/) || [])[1] || s.level;
        return `<span>并发 ${esc(conc)}：${s.count} 间隔 · avg ${s.avg}ms · P50 ${s.p50}ms · P99 ${s.p99}ms · max ${s.max}ms</span>`;
      }).join("");
      dist.innerHTML = `<div class="perf-req-summary">
        <span><b>ITL 分布</b>（${totalCount} 个 token 间隔 · 各档聚合）</span>
        <span>加权avg ${wAvg.toFixed(1)}ms</span>
        <span>P50(最高档) ${p50Max.toFixed(1)}ms</span>
        <span>P99(最高档) ${p99Max.toFixed(1)}ms</span>
        <span>max ${mx.toFixed(1)}ms</span>
      </div>
      <div class="perf-req-summary sub">${levelLines}</div>`;
    }
  } catch (e) {
    stats.textContent = "加载出错：" + e.message;
  } finally {
    btn.disabled = false; btn.textContent = "加载请求明细";
  }
}

// 绘制 SVG 折线图：并发 vs RPS（左轴）+ 平均/P90 延迟（右轴）
function sweepChart(rows) {
  const W = 720, H = 320, padL = 52, padR = 52, padT = 24, padB = 44;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const xs = rows.map(r => r.concurrency);
  const maxX = Math.max(...xs), minX = Math.min(...xs);
  const maxRps = Math.max(...rows.map(r => r.rps || 0), 0.1);
  const maxLat = Math.max(...rows.map(r => r.latency_p90 || r.latency_avg || 0), 0.1);

  const xPos = (x) => padL + (maxX === minX ? plotW / 2 : (x - minX) / (maxX - minX) * plotW);
  const yRps = (v) => padT + plotH - (v / maxRps) * plotH;
  const yLat = (v) => padT + plotH - (v / maxLat) * plotH;

  const line = (pts, color, w = 2) =>
    `<polyline points="${pts.map(p => p.join(",")).join(" ")}" fill="none"
      stroke="${color}" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round"/>`;
  const dots = (pts, color, titleFn) => pts.map((p, i) =>
    `<circle cx="${p[0]}" cy="${p[1]}" r="3.5" fill="${color}" stroke="#ffffff" stroke-width="1.5">${titleFn ? `<title>${esc(titleFn(i))}</title>` : ""}</circle>`).join("");

  const rpsPts = rows.map(r => [xPos(r.concurrency), yRps(r.rps || 0)]);
  const latPts = rows.map(r => [xPos(r.concurrency), yLat(r.latency_avg || 0)]);
  const p90Pts = rows.map(r => [xPos(r.concurrency), yLat(r.latency_p90 || r.latency_avg)]);

  // 网格 + 轴标签
  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const y = padT + (plotH / 4) * i;
    grid += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"
      stroke="#e4e4e7" stroke-width="1"/>`;
    grid += `<text x="${padL - 8}" y="${y + 4}" text-anchor="end" fill="#4f46e5"
      font-size="10" font-family="monospace">${(maxRps * (1 - i / 4)).toFixed(1)}</text>`;
    grid += `<text x="${W - padR + 8}" y="${y + 4}" text-anchor="start" fill="#18181b"
      font-size="10" font-family="monospace">${(maxLat * (1 - i / 4)).toFixed(2)}</text>`;
  }
  // x 轴档位标签
  let xlabels = rows.map(r =>
    `<text x="${xPos(r.concurrency)}" y="${H - padB + 18}" text-anchor="middle"
      fill="#71717a" font-size="10" font-family="monospace">${r.concurrency}</text>`).join("");

  return `
    <div class="chart-legend">
      <span class="lg"><i style="background:#4f46e5"></i>RPS 吞吐</span>
      <span class="lg"><i style="background:#0d9488"></i>平均延迟</span>
      <span class="lg"><i style="background:#18181b"></i>P90 延迟</span>
      <span class="lg-axis">左轴 RPS · 右轴 延迟(s)</span>
    </div>
    <svg viewBox="0 0 ${W} ${H}" class="sweep-svg" xmlns="http://www.w3.org/2000/svg">
      ${grid}
      ${line(latPts, "#0d9488", 1.8)}
      ${line(p90Pts, "#18181b", 1.8)}
      ${line(rpsPts, "#4f46e5", 2.4)}
      ${dots(latPts, "#0d9488", i => `并发 ${rows[i].concurrency} · 平均延迟 ${fmtMetric(rows[i].latency_avg)}s`)}
      ${dots(p90Pts, "#18181b", i => `并发 ${rows[i].concurrency} · P90 延迟 ${fmtMetric(rows[i].latency_p90)}s`)}
      ${dots(rpsPts, "#4f46e5", i => `并发 ${rows[i].concurrency} · RPS ${fmtMetric(rows[i].rps)} · 成功率 ${rows[i].success_rate ?? "-"}%`)}
      ${xlabels}
      <text x="${W / 2}" y="${H - 6}" text-anchor="middle" fill="#71717a"
        font-size="11" font-family="monospace">并发数</text>
    </svg>`;
}

// ── TTFT 曲线：首 token 延迟 avg + P99 vs 并发 ──
function ttftChart(rows) {
  const W = 720, H = 260, padL = 52, padR = 52, padT = 24, padB = 44;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const valid = rows.filter(r => r.ttft_avg != null);
  if (!valid.length) return "";
  const xs = valid.map(r => r.concurrency);
  const maxX = Math.max(...xs), minX = Math.min(...xs);
  const maxTtft = Math.max(...valid.map(r => r.ttft_p99 || r.ttft_avg || 0), 0.1);

  const xPos = x => padL + (maxX === minX ? plotW / 2 : (x - minX) / (maxX - minX) * plotW);
  const yVal = v => padT + plotH - (v / maxTtft) * plotH;

  const line = (pts, color, w) =>
    '<polyline points="' + pts.map(p => p.join(",")).join(" ") + '" fill="none" stroke="' + color + '" stroke-width="' + w + '" stroke-linecap="round" stroke-linejoin="round"/>';
  const dots = (pts, color) => pts.map(p =>
    '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="3.5" fill="' + color + '" stroke="#ffffff" stroke-width="1.5"/>').join("");

  const ttftPts = valid.map(r => [xPos(r.concurrency), yVal(r.ttft_avg || 0)]);
  const ttftP99Pts = valid.map(r => [xPos(r.concurrency), yVal(r.ttft_p99 || r.ttft_avg || 0)]);

  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const y = padT + (plotH / 4) * i;
    grid += '<line x1="' + padL + '" y1="' + y + '" x2="' + (W - padR) + '" y2="' + y + '" stroke="#e4e4e7" stroke-width="1"/>';
    grid += '<text x="' + (padL - 8) + '" y="' + (y + 4) + '" text-anchor="end" fill="#71717a" font-size="9" font-family="monospace">' + (maxTtft * (1 - i / 4)).toFixed(2) + 's</text>';
  }
  let xlabels = valid.map(r =>
    '<text x="' + xPos(r.concurrency) + '" y="' + (H - padB + 18) + '" text-anchor="middle" fill="#71717a" font-size="10" font-family="monospace">' + r.concurrency + '</text>').join("");

  return '<div class="chart-legend">'
    + '<span class="lg"><i style="background:#d97706"></i>TTFT avg</span>'
    + '<span class="lg"><i style="background:#dc2626"></i>TTFT P99</span>'
    + '</div>'
    + '<svg viewBox="0 0 ' + W + ' ' + H + '" class="sweep-svg" xmlns="http://www.w3.org/2000/svg">'
    + grid
    + line(ttftP99Pts, "#dc2626", 1.8)
    + line(ttftPts, "#d97706", 2.4)
    + dots(ttftP99Pts, "#dc2626")
    + dots(ttftPts, "#d97706")
    + xlabels
    + '<text x="' + (W / 2) + '" y="' + (H - 6) + '" text-anchor="middle" fill="#71717a" font-size="11" font-family="monospace">并发数</text>'
    + '</svg>';
}

// ── Token 吞吐曲线：输出/总/输入 tok/s vs 并发 ──
function tpsChart(rows) {
  const W = 720, H = 260, padL = 52, padR = 52, padT = 24, padB = 44;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const valid = rows.filter(r => r.output_tps != null);
  if (!valid.length) return "";
  const xs = valid.map(r => r.concurrency);
  const maxX = Math.max(...xs), minX = Math.min(...xs);
  const hasTotal = valid.some(r => r.total_tps != null);
  const hasInput = valid.some(r => r.input_tps != null && r.input_tps > 0);
  const maxTps = Math.max(
    ...valid.map(r => Math.max(r.output_tps || 0, r.total_tps || 0, r.input_tps || 0)), 0.1
  );

  const xPos = x => padL + (maxX === minX ? plotW / 2 : (x - minX) / (maxX - minX) * plotW);
  const yVal = v => padT + plotH - (v / maxTps) * plotH;

  const line = (pts, color, w) =>
    '<polyline points="' + pts.map(p => p.join(",")).join(" ") + '" fill="none" stroke="' + color + '" stroke-width="' + w + '" stroke-linecap="round" stroke-linejoin="round"/>';
  const dots = (pts, color) => pts.map(p =>
    '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="3.5" fill="' + color + '" stroke="#ffffff" stroke-width="1.5"/>').join("");

  const outPts = valid.map(r => [xPos(r.concurrency), yVal(r.output_tps || 0)]);
  const totalPts = hasTotal ? valid.map(r => [xPos(r.concurrency), yVal(r.total_tps || 0)]) : [];
  const inPts = hasInput ? valid.map(r => [xPos(r.concurrency), yVal(r.input_tps || 0)]) : [];

  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const y = padT + (plotH / 4) * i;
    grid += '<line x1="' + padL + '" y1="' + y + '" x2="' + (W - padR) + '" y2="' + y + '" stroke="#e4e4e7" stroke-width="1"/>';
    grid += '<text x="' + (padL - 8) + '" y="' + (y + 4) + '" text-anchor="end" fill="#71717a" font-size="9" font-family="monospace">' + fmtMetric(maxTps * (1 - i / 4)) + '</text>';
  }
  let xlabels = valid.map(r =>
    '<text x="' + xPos(r.concurrency) + '" y="' + (H - padB + 18) + '" text-anchor="middle" fill="#71717a" font-size="10" font-family="monospace">' + r.concurrency + '</text>').join("");

  let legend = '<span class="lg"><i style="background:#4f46e5"></i>输出 tok/s</span>';
  let lines = line(outPts, "#4f46e5", 2.4);
  let dotsHtml = dots(outPts, "#4f46e5");
  if (hasTotal) {
    legend += '<span class="lg"><i style="background:#a78bfa"></i>总 tok/s</span>';
    lines += line(totalPts, "#a78bfa", 1.8);
    dotsHtml += dots(totalPts, "#a78bfa");
  }
  if (hasInput) {
    legend += '<span class="lg"><i style="background:#34d399"></i>输入 tok/s</span>';
    lines += line(inPts, "#34d399", 1.8);
    dotsHtml += dots(inPts, "#34d399");
  }

  return '<div class="chart-legend">' + legend + '</div>'
    + '<svg viewBox="0 0 ' + W + ' ' + H + '" class="sweep-svg" xmlns="http://www.w3.org/2000/svg">'
    + grid
    + lines
    + dotsHtml
    + xlabels
    + '<text x="' + (W / 2) + '" y="' + (H - 6) + '" text-anchor="middle" fill="#71717a" font-size="11" font-family="monospace">并发数</text>'
    + '</svg>';
}

// ── 延迟分位数分布：P50 / P90 / P99 / Max vs 并发 ──
function latencySpreadChart(rows) {
  const W = 720, H = 260, padL = 52, padR = 52, padT = 24, padB = 44;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const valid = rows.filter(r => r.latency_avg != null);
  if (!valid.length) return "";
  const xs = valid.map(r => r.concurrency);
  const maxX = Math.max(...xs), minX = Math.min(...xs);
  const hasP50 = valid.some(r => r.latency_p50 != null);
  const hasMax = valid.some(r => r.latency_max != null);
  const maxLat = Math.max(
    ...valid.map(r => Math.max(r.latency_max || 0, r.latency_p99 || 0, r.latency_p90 || 0, r.latency_avg || 0)), 0.1
  );

  const xPos = x => padL + (maxX === minX ? plotW / 2 : (x - minX) / (maxX - minX) * plotW);
  const yVal = v => padT + plotH - (v / maxLat) * plotH;

  const line = (pts, color, w, dash) =>
    '<polyline points="' + pts.map(p => p.join(",")).join(" ") + '" fill="none" stroke="' + color + '" stroke-width="' + w + '" stroke-linecap="round" stroke-linejoin="round"' + (dash ? ' stroke-dasharray="' + dash + '"' : '') + '/>';
  const dots = (pts, color) => pts.map(p =>
    '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="3" fill="' + color + '" stroke="#ffffff" stroke-width="1"/>').join("");

  const p50Pts = hasP50 ? valid.map(r => [xPos(r.concurrency), yVal(r.latency_p50 || 0)]) : [];
  const p90Pts = valid.map(r => [xPos(r.concurrency), yVal(r.latency_p90 || 0)]);
  const p99Pts = valid.map(r => [xPos(r.concurrency), yVal(r.latency_p99 || 0)]);
  const maxPts = hasMax ? valid.map(r => [xPos(r.concurrency), yVal(r.latency_max || 0)]) : [];

  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const y = padT + (plotH / 4) * i;
    grid += '<line x1="' + padL + '" y1="' + y + '" x2="' + (W - padR) + '" y2="' + y + '" stroke="#e4e4e7" stroke-width="1"/>';
    grid += '<text x="' + (padL - 8) + '" y="' + (y + 4) + '" text-anchor="end" fill="#71717a" font-size="9" font-family="monospace">' + (maxLat * (1 - i / 4)).toFixed(2) + 's</text>';
  }
  let xlabels = valid.map(r =>
    '<text x="' + xPos(r.concurrency) + '" y="' + (H - padB + 18) + '" text-anchor="middle" fill="#71717a" font-size="10" font-family="monospace">' + r.concurrency + '</text>').join("");

  let legend = '<span class="lg"><i style="background:#4f46e5"></i>P50</span>'
    + '<span class="lg"><i style="background:#0d9488"></i>P90</span>'
    + '<span class="lg"><i style="background:#18181b"></i>P99</span>';
  let lines = '';
  let dotsHtml = '';
  if (hasP50) {
    lines += line(p50Pts, "#4f46e5", 1.5, "4,4");
    dotsHtml += dots(p50Pts, "#4f46e5");
  }
  lines += line(p90Pts, "#0d9488", 2);
  lines += line(p99Pts, "#18181b", 2.4);
  dotsHtml += dots(p90Pts, "#0d9488");
  dotsHtml += dots(p99Pts, "#18181b");
  if (hasMax) {
    legend += '<span class="lg"><i style="background:#dc2626"></i>Max</span>';
    lines += line(maxPts, "#dc2626", 1.2, "2,3");
    dotsHtml += dots(maxPts, "#dc2626");
  }

  return '<div class="chart-legend">' + legend + '</div>'
    + '<svg viewBox="0 0 ' + W + ' ' + H + '" class="sweep-svg" xmlns="http://www.w3.org/2000/svg">'
    + grid
    + lines
    + dotsHtml
    + xlabels
    + '<text x="' + (W / 2) + '" y="' + (H - 6) + '" text-anchor="middle" fill="#71717a" font-size="11" font-family="monospace">并发数</text>'
    + '</svg>';
}

// HSL 色阶：0=红 → 50=黄 → 100=绿
function scoreColor(score) {
  const v = Math.max(0, Math.min(100, Number(score) || 0));
  const h = v * 1.2; // 0→120 on HSL wheel
  return `hsl(${h.toFixed(0)},65%,55%)`;
}

function esc(s) {
  // 转义 &<> 及引号：既可用于文本内容，也可安全用于双引号属性值
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtDur(sec) {
  sec = Math.round(sec || 0);
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m" + (sec % 60) + "s";
  return Math.floor(sec / 3600) + "h" + Math.floor((sec % 3600) / 60) + "m";
}

function fmtNum(n) {
  n = n || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

function fmtMetric(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "-";
  const v = Number(n);
  if (Math.abs(v) >= 100) return v.toFixed(1);
  if (Math.abs(v) >= 10) return v.toFixed(2);
  return v.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

// ============ 性能测试预设 ============
// 性能测试预设 — 对齐 evalscope perf 标准 (number=1000 为 evalscope 默认)
// warmup 支持 ratio 模式：0.05 = 5% 预热，传 evalscope warmup_num 自动识别
// sleep_interval 默认 5s (evalscope 内置，档位间自动间隔)
const PERF_PRESETS = {
  sweep: {
    label: "标准 Sweep",
    levels: "1,5,10,50,100",
    scaleMode: false,
    scaleMult: 10,
    maxTokens: 1024,
    minTokens: 1024,
    contextLength: 2048,
    timeout: 600,
    warmup: 0.05,
    stream: "true",
    temperature: 0,
    hint: "evalscope 标准：并发 1/5/10/50/100 · 每档 1000 请求 · 5% 预热 · 2K入1K出 · 档间 5s 间隔",
  },
  quick: {
    label: "快速压测",
    levels: "1,5,10",
    scaleMode: false,
    scaleMult: 10,
    maxTokens: 256,
    minTokens: 0,
    contextLength: 0,
    timeout: 120,
    warmup: 0,
    stream: "true",
    temperature: 0,
    hint: "evalscope 快速验证：并发 1/5/10 · 每档 200 请求 · 无预热 · 短输入 · 约 60s 完成",
  },
  stress: {
    label: "极限压测",
    levels: "50,100,200",
    scaleMode: false,
    scaleMult: 10,
    maxTokens: 512,
    minTokens: 0,
    contextLength: 0,
    timeout: 600,
    warmup: 0.05,
    stream: "true",
    temperature: 0,
    hint: "evalscope 高压：并发 50/100/200 · 每档 1000 请求 · 5% 预热 · 短输入 · 档间 5s 间隔",
  },
};

function applyPerfPreset(key) {
  const p = PERF_PRESETS[key];
  if (!p) return;
  $("#sweepLevels").value = p.levels;
  $("#perfScaleMode").checked = p.scaleMode;
  $("#perfScaleMult").value = p.scaleMult;
  $("#perfMaxTokens").value = p.maxTokens;
  $("#perfMinTokens").value = p.minTokens;
  $("#contextLength").value = p.contextLength;
  $("#perfTimeout").value = p.timeout;
  $("#perfWarmup").value = p.warmup;
  $("#perfStream").value = p.stream;
  $("#perfTemperature").value = p.temperature;
  // Update scale UI visibility
  updatePerfScalePreview();
  updateConfigSummaries();
  // Highlight active preset
  $$(".perf-preset").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.preset === key);
  });
  // Show hint
  const hintEl = document.getElementById("perfPresetHint");
  if (hintEl) hintEl.textContent = p.hint;
}

$$(".perf-preset").forEach(btn => {
  btn.addEventListener("click", () => applyPerfPreset(btn.dataset.preset));
});

// ============ 推荐参数预设 ============
const PRESETS = {
  thinking: {
    label: "思考型大模型",
    timeout: 600, disableThinking: false, accConcurrency: 4,
    accMaxTokens: 8192, maxRetries: 3, accStream: "true",
    hint: "已设：超时600s · 并发4 · max_tokens8192 · 重试3 · 流式。适合235B/QwQ等强思考模型，避免超时。",
  },
  instruct: {
    label: "普通指令模型",
    timeout: 120, disableThinking: false, accConcurrency: 16,
    accMaxTokens: 0, maxRetries: 2, accStream: "false",
    hint: "已设：超时120s · 并发16 · max_tokens自动 · 重试2 · 非流式。适合7B/14B等常规指令模型，评测更快。",
  },
};

function applyPreset(key) {
  const p = PRESETS[key];
  if (!p) return;
  $("#timeout").value = p.timeout;
  $("#disableThinking").checked = p.disableThinking;
  $("#accConcurrency").value = p.accConcurrency;
  $("#accMaxTokens").value = p.accMaxTokens || "";
  $("#maxRetries").value = p.maxRetries;
  $("#accStream").value = p.accStream;
  const hint = $("#presetHint");
  if (hint) {
    hint.textContent = `✓ 已应用「${p.label}」预设，可继续微调`;
    hint.className = "preset-hint active";
  }
  document.querySelectorAll(".advanced").forEach(d => {
    if (d.querySelector("#accMaxTokens")) d.open = true;
  });
}

$("#presetSelect").addEventListener("change", e => {
  const key = e.target.value;
  const hint = $("#presetHint");
  if (!key) { if (hint) { hint.textContent = ""; hint.className = "preset-hint"; } return; }
  applyPreset(key);
});

// 手动改动预设管理的字段时，下拉回到"不使用预设"，表示已是自定义配置
["#timeout", "#accConcurrency", "#accMaxTokens", "#maxRetries", "#accStream", "#disableThinking"]
  .forEach(sel => {
    const el = $(sel);
    if (el) el.addEventListener("change", () => {
      if ($("#presetSelect").value) {
        $("#presetSelect").value = "";
        const hint = $("#presetHint");
        if (hint) { hint.textContent = ""; hint.className = "preset-hint"; }
      }
    });
  });
const drawer = $("#historyDrawer");
const drawerMask = $("#drawerMask");

// ---- 回收站视图（软删除的任务可追溯/恢复）----
let _trashMode = false;
const drawerTrashBtn = document.getElementById("drawerTrashBtn");
const drawerFiltersEl = document.querySelector(".drawer-filters");

function setTrashMode(on) {
  _trashMode = on;
  if (drawerTrashBtn) {
    drawerTrashBtn.classList.toggle("on", on);
    drawerTrashBtn.textContent = on ? "↩ 返回任务" : "🗑 回收站";
  }
  if (drawerFiltersEl) drawerFiltersEl.style.display = on ? "none" : "";
  if (on) loadTrashList(); else loadTaskList();
}
if (drawerTrashBtn) drawerTrashBtn.addEventListener("click", () => setTrashMode(!_trashMode));

// 抽屉内容刷新：回收站模式刷回收站，否则刷任务列表
function refreshDrawerList() {
  if (_trashMode) loadTrashList(); else loadTaskList();
}

async function loadTrashList() {
  const el = $("#taskList");
  el.innerHTML = '<div class="skeleton skeleton-card"></div>';
  try {
    const r = await fetch("/api/tasks/trash");
    const data = await r.json();
    if (!data.tasks || !data.tasks.length) {
      el.innerHTML = '<div class="loading-sm">回收站为空。删除的任务会保留在此，可随时恢复。</div>';
      return;
    }
    el.innerHTML = "";
    data.tasks.forEach(t => el.appendChild(trashCard(t)));
  } catch (e) {
    el.innerHTML = '<div class="loading-sm">加载失败</div>';
  }
}

function trashCard(t) {
  const card = document.createElement("div");
  card.className = "task-card trash";
  const fmtT = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN",
    { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
  card.innerHTML = `
    <div class="tc-main">
      <div class="tc-name" title="${esc(t.name)}">${esc(t.name)}</div>
      <div class="tc-right"><span class="tc-status stopped">回收站</span></div>
    </div>
    <div class="tc-meta">${esc(t.model || "")} · 创建 ${fmtT(t.created)} · 删除 ${fmtT(t.deleted_at)} · 原始状态 ${STATUS_LABEL[t.status] || t.status}</div>
    <div class="tc-actions">
      <button data-act="restore">恢复</button>
      <button data-act="purge" class="act-stop">彻底删除</button>
    </div>`;
  card.querySelector('[data-act="restore"]').addEventListener("click", async () => {
    const r = await fetch(`/api/tasks/${t.id}/restore`, { method: "POST" });
    if (r.ok) { toast("已恢复任务", "success"); loadTrashList(); }
    else toast("恢复失败", "error");
  });
  card.querySelector('[data-act="purge"]').addEventListener("click", async () => {
    if (!confirm(`彻底删除「${t.name}」？此操作不可恢复。`)) return;
    const r = await fetch(`/api/tasks/${t.id}/purge`, { method: "POST" });
    if (r.ok) { toast("已彻底删除", "success"); loadTrashList(); }
    else toast("删除失败", "error");
  });
  return card;
}

function openDrawer() {
  drawer.classList.add("open");
  drawerMask.classList.add("show");
  if (_trashMode) loadTrashList(); else loadTaskList();
}
function closeDrawer() {
  drawer.classList.remove("open");
  drawerMask.classList.remove("show");
}
$("#btnHistory").addEventListener("click", openDrawer);
$("#drawerClose").addEventListener("click", closeDrawer);
drawerMask.addEventListener("click", closeDrawer);

let _allTasks = [];
let _filterStatus = "";
let _filterSearch = "";

async function loadTaskList() {
  const el = $("#taskList");
  el.innerHTML = '<div class="skeleton skeleton-card"></div><div class="skeleton skeleton-card"></div><div class="skeleton skeleton-card"></div>';
  try {
    const r = await fetch("/api/tasks");
    const data = await r.json();
    if (!data.tasks.length) {
      el.innerHTML = '<div class="loading-sm">暂无历史任务</div>';
      return;
    }
    _allTasks = data.tasks;
    applyFilters();
  } catch (e) {
    el.innerHTML = '<div class="loading-sm">加载失败</div>';
  }
}

function applyFilters() {
  const el = $("#taskList");
  let filtered = _allTasks;
  if (_filterStatus) {
    filtered = filtered.filter(t => t.status === _filterStatus);
  }
  if (_filterSearch) {
    const q = _filterSearch.toLowerCase();
    filtered = filtered.filter(t =>
      (t.name || "").toLowerCase().includes(q) ||
      (t.model || "").toLowerCase().includes(q)
    );
  }
  el.innerHTML = "";
  if (!filtered.length) {
    el.innerHTML = '<div class="loading-sm">无匹配任务</div>';
    return;
  }
  filtered.forEach(t => el.appendChild(taskCard(t)));
}

// Search debounce
let _searchTimer = null;
$("#drawerSearch").addEventListener("input", () => {
  _filterSearch = $("#drawerSearch").value;
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(applyFilters, 300);
});

// Status filter tabs
$("#drawerStatusTabs").addEventListener("click", e => {
  if (e.target.tagName !== "BUTTON") return;
  $$("#drawerStatusTabs button").forEach(b => b.classList.remove("on"));
  e.target.classList.add("on");
  _filterStatus = e.target.dataset.status;
  applyFilters();
});

const STATUS_LABEL = { done: "完成", running: "运行中", error: "错误",
  stopped: "已停止", stopping: "停止中", pending: "等待中" };

function taskCard(t) {
  const card = document.createElement("div");
  card.className = "task-card";
  const time = t.created ? new Date(t.created * 1000).toLocaleString("zh-CN",
    { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";
  // Duration
  let dur = "";
  if (t.duration != null) {
    dur = t.duration < 60 ? `${t.duration}s` : t.duration < 3600
      ? `${Math.floor(t.duration/60)}m${Math.floor(t.duration%60)}s`
      : `${Math.floor(t.duration/3600)}h${Math.floor((t.duration%3600)/60)}m`;
  }
  // Queue position
  let qpos = "";
  if (t.queue_position != null) {
    qpos = ` · 排队 #${t.queue_position}`;
  }
  // 摘要
  let summary = "";
  const hasAccuracy = t.summary && t.summary.accuracy && Object.keys(t.summary.accuracy).length > 0;
  if (t.summary) {
    if (hasAccuracy) {
      summary += Object.entries(t.summary.accuracy)
        .map(([k, v]) => `${k} ${v}%`).join(" · ");
    }
    if (t.summary.perf_best) {
      summary += (summary ? " · " : "") +
        `峰值 ${t.summary.perf_best.rps} RPS@${t.summary.perf_best.concurrency}`;
    }
    if (t.summary.context_scan) {
      summary += (summary ? " · " : "") +
        `上下文 ${t.summary.context_scan.levels} 档`;
    }
    if (t.summary.serving && (t.summary.serving.container || t.summary.serving.label)) {
      summary += (summary ? " · " : "") +
        `🐳 ${t.summary.serving.container || t.summary.serving.label}`;
    }
  }
  card.innerHTML = `
    <div class="tc-main">
      <div class="tc-name" title="${esc(t.name)}">${esc(t.name)}</div>
      <div class="tc-right">
        <span class="tc-status ${t.status}">${STATUS_LABEL[t.status] || t.status}</span>
      </div>
    </div>
    <div class="tc-meta">${esc(t.model || "")} · ${time}${qpos}${dur ? " · " + dur : ""}</div>
    ${summary ? `<div class="tc-summary">${esc(summary)}</div>` : ""}
    <div class="tc-actions">
      <button data-act="view">查看</button>
      <button data-act="copy">复制配置</button>
      ${t.status === "running" ? '<button data-act="stop" class="act-stop">停止</button>' : ""}
      ${(t.status === "stopped" || t.status === "error") ? '<button data-act="rerun">续跑</button>' : ""}
      <button data-act="rename">重命名</button>
      <button data-act="del">删除</button>
    </div>
    ${(t.status === "done" || t.status === "stopped") ? `<div class="tc-export">
      <button data-act="xlsx">导出 Excel</button>
      <button data-act="pdf">导出 PDF</button>
      <button data-act="evalscope" title="${hasAccuracy ? '下载 evalscope 原始 HTML 评测报告' : '仅精度评测任务可导出 evalscope 原始报告'}">Evalscope 原始报告${hasAccuracy ? '' : ' ⊘'}</button>
    </div>${!hasAccuracy ? '<div class="tc-export-note">⚠ evalscope 原始报告仅适用于精度评测，纯性能任务请用 Excel/PDF</div>' : ''}` : ""}`;
  card.querySelector('[data-act="view"]').addEventListener("click", () => viewTask(t.id));
  card.querySelector('[data-act="copy"]').addEventListener("click", () => copyConfig(t.id));
  const stopBtn = card.querySelector('[data-act="stop"]');
  if (stopBtn) stopBtn.addEventListener("click", () => stopTaskFromDrawer(t.id, t.name));
  const xlsxBtn = card.querySelector('[data-act="xlsx"]');
  if (xlsxBtn) xlsxBtn.addEventListener("click", () => exportReport(t.id, "excel"));
  const pdfBtn = card.querySelector('[data-act="pdf"]');
  if (pdfBtn) pdfBtn.addEventListener("click", () => exportReport(t.id, "pdf"));
  const esBtn = card.querySelector('[data-act="evalscope"]');
  if (esBtn) esBtn.addEventListener("click", () => exportReport(t.id, "evalscope"));
  const rerunBtn = card.querySelector('[data-act="rerun"]');
  if (rerunBtn) rerunBtn.addEventListener("click", () => rerunTask(t.id));
  card.querySelector('[data-act="rename"]').addEventListener("click", () => renameTask(t.id, t.name));
  card.querySelector('[data-act="del"]').addEventListener("click", () => delTask(t.id, t.name));
  return card;
}

async function copyConfig(id) {
  // 把历史任务的配置回填到表单，便于改两项后重跑
  try {
    const r = await fetch("/api/tasks/" + id);
    const d = await r.json();
    const c = d.config || {};
    const set = (fid, v) => {
      const el = $("#" + fid);
      if (!el || v === undefined || v === null) return;
      if (el.type === "checkbox") { el.checked = !!v; el.dispatchEvent(new Event("change")); }
      else el.value = v;
    };
    set("baseUrl", c.base_url); set("model", c.model);
    set("apiFormat", c.api_format || "openai");
    set("servingLabel", c.serving_label || (c.serving && c.serving.label) || "");
    set("timeout", c.timeout); set("disableThinking", c.disable_thinking);
    set("sampleLimit", c.sample_limit); set("fewShot", c.few_shot || 0);
    set("accConcurrency", c.acc_concurrency);
    set("accMaxTokens", c.acc_max_tokens || ""); set("accTemperature", c.acc_temperature);
    set("maxRetries", c.max_retries); set("accStream", String(!!c.acc_stream));
    set("accSystem", c.acc_system || ""); set("accTemplate", c.acc_template || "");
    // 数据集勾选
    selectedDatasets = new Set(c.accuracy_datasets || []);
    selectedSubjects = new Map(Object.entries(c.dataset_subjects || {})
      .map(([k, v]) => [k, new Set(v)]));
    await loadDatasets();
    // 性能压测（含新版字段）
    if (c.perf) {
      set("runPerf", !!c.run_performance);
      const lv = Array.isArray(c.perf.levels) ? c.perf.levels.join(",")
               : (c.perf.sweep_levels || "");
      set("sweepLevels", lv);
      set("perfTotal", c.perf.requests_per_level);
      set("perfScaleMode", (c.perf.scale_multiplier || 0) > 0);
      set("perfScaleMult", c.perf.scale_multiplier || 10);
      set("perfMaxTokens", c.perf.max_tokens); set("perfMinTokens", c.perf.min_tokens || 0);
      set("perfStream", String(!!c.perf.stream));
      set("contextLength", c.perf.context_length || 0); set("perfPrompt", c.perf.prompt || "");
      set("perfTemperature", c.perf.temperature); set("perfSystem", c.perf.system || "");
      set("perfTimeout", c.perf.timeout || 300); set("perfWarmup", c.perf.warmup_requests || 0);
    }
    // 上下文扫描
    if (Array.isArray(c.context_lengths) && c.context_lengths.length) {
      set("runCtxScan", true);
      set("ctxLengths", c.context_lengths.join(","));
      set("ctxConcurrency", c.context_concurrency || 8);
      set("ctxRequests", c.context_requests || 20);
      set("ctxMaxTokens", c.context_max_tokens || 256);
      set("ctxStream", String(c.context_stream !== false));
    }
    closeDrawer();
    saveForm();
    toast("配置已复制到表单" + ((c.api_key === "***" || !c.api_key) ? "（API Key 需重新填写）" : ""), "success");
  } catch (e) {
    toast("复制配置失败：" + e.message, "error");
  }
}

async function viewTask(id) {
  try {
    // 查看历史记录时，断开与实时 SSE 流的关联，避免实时日志覆盖历史数据
    currentTaskId = null;
    const r = await fetch("/api/tasks/" + id);
    const d = await r.json();
    _reviewTaskId = id;  // enable review viewer
    closeDrawer();
    // 渲染配置面板
    renderConfigPanel(d);
    // 上下文横幅：正在查看历史任务，可一键返回实时
    showViewBanner(d);
    // 回放日志
    $("#console").innerHTML = "";
    (d.logs || []).forEach(l => addLog(l.level, l.msg, l.ts));
    // 渲染结果
    $("#accResults").innerHTML = ""; $("#perfResults").innerHTML = "";
    $("#ctxResults").innerHTML = "";
    $("#accEmpty").style.display = "block"; $("#perfEmpty").style.display = "block";
    $("#ctxEmpty").style.display = "block";
    sweepRows = (d.sweep_levels || []).map(e => e.row);
    if (d.result) renderResults(d.result);
    setStatus(STATUS_LABEL[d.status] || d.status,
      d.status === "done" ? "done" : d.status === "error" ? "error" : "");
    // 切到精度结果或实时进程
    const hasAcc = d.result && d.result.accuracy && Object.keys(d.result.accuracy).length;
    const _vt = document.querySelector('[data-tab="' + (hasAcc ? 'accuracy' : 'live') + '"]');
    if (_vt) _vt.click();
  } catch (e) {
    toast("\u52a0\u8f7d\u4efb\u52a1\u8be6\u60c5\u5931\u8d25\uff1a" + e.message, "info");
  }
}

// ---- 历史任务查看横幅：标明当前视图上下文，可一键返回实时进程 ----
function showViewBanner(d) {
  const pane = document.getElementById("pane-live");
  const con = document.getElementById("console");
  if (!pane || !con) return;
  let b = document.getElementById("viewBanner");
  if (!b) {
    b = document.createElement("div");
    b.id = "viewBanner";
    b.className = "view-banner";
    pane.insertBefore(b, con);
    b.addEventListener("click", e => {
      if (e.target.closest("#viewBannerBack")) { b.remove(); switchLiveBack(); }
      else if (e.target.closest("#viewBannerClose")) b.remove();
    });
  }
  b.innerHTML = `<span class="vb-icon">👁</span><span class="vb-text">正在查看历史任务：<b>${esc(d.name || d.id)}</b>（${STATUS_LABEL[d.status] || d.status}）</span><button id="viewBannerBack">返回实时进程</button><button id="viewBannerClose" title="关闭">✕</button>`;
}

function switchLiveBack() {
  const running = Object.entries(_taskStreams).filter(([, s]) => !s.done);
  if (running.length) {
    connectStream(running[0][0]);
    switchLiveTask(running[0][0]);
    return;
  }
  // 无运行任务：清空历史视图，回到实时空态
  currentTaskId = null;
  $("#console").innerHTML = '<div class="console-empty">配置模型与测试项后，点击「开始测评」。日志将在此实时显示。</div>';
  $("#accResults").innerHTML = ""; $("#perfResults").innerHTML = ""; $("#ctxResults").innerHTML = "";
  $("#accEmpty").style.display = "block"; $("#perfEmpty").style.display = "block"; $("#ctxEmpty").style.display = "block";
  const _t = document.querySelector('[data-tab="live"]');
  if (_t) _t.click();
  setStatus("就绪", "");
}

function renderConfigPanel(d) {
  const cfg = d.config || {};
  let c = $("#configPanel");
  if (!c) {
    c = document.createElement("div");
    c.id = "configPanel";
    c.className = "config-panel";
    const live = $("#pane-live");
    live.insertBefore(c, live.firstChild);
  }
  const datasets = (cfg.accuracy_datasets || []).slice(0, 10);
  const moreDs = (cfg.accuracy_datasets || []).length - 10;
  const dsTags = datasets.map(ds => `<span class="cfg-tag">${esc(ds)}</span>`).join("")
    + (moreDs > 0 ? `<span class="cfg-tag">+${moreDs}</span>` : "");
  const subjects = cfg.dataset_subjects || {};
  let subjStr = "";
  for (const [ds, subs] of Object.entries(subjects)) {
    if (subs && subs.length) subjStr += `<span class="cfg-tag dim">${esc(ds)}: ${subs.slice(0,3).map(esc).join(",")}${subs.length>3?",...":""}</span>`;
  }
  const perfEnabled = cfg.run_performance || false;
  const pc = cfg.perf || {};
  const ctxEnabled = (cfg.context_lengths || []).length > 0;
  const serving = cfg.serving || {};
  const modelsStr = (serving.models || []).join(", ");
  const duration = d.duration != null
    ? (d.duration < 60 ? `${d.duration}s` : d.duration < 3600
      ? `${Math.floor(d.duration/60)}m${Math.floor(d.duration%60)}s`
      : `${Math.floor(d.duration/3600)}h${Math.floor((d.duration%3600)/60)}m`) : "-";

  c.innerHTML = `<details class="advanced" open>
    <summary>任务配置详情</summary>
    <div class="cfg-grid">
      <div class="cfg-row"><span class="cfg-k">模型</span><span class="cfg-v">${esc(cfg.model || "-")}</span></div>
      <div class="cfg-row"><span class="cfg-k">接口地址</span><span class="cfg-v mono">${esc((cfg.base_url || "").substring(0,60))}</span></div>
      <div class="cfg-row"><span class="cfg-k">接口格式</span><span class="cfg-v">${esc(cfg.api_format || "-")}</span></div>
      <div class="cfg-row"><span class="cfg-k">请求超时</span><span class="cfg-v">${cfg.timeout ?? "-"}s</span></div>
      <div class="cfg-row"><span class="cfg-k">关闭思考</span><span class="cfg-v">${cfg.disable_thinking ? "是" : "否"}</span></div>
      <div class="cfg-row"><span class="cfg-k">运行时长</span><span class="cfg-v">${duration}</span></div>
      ${serving.container || serving.label ? `<div class="cfg-row"><span class="cfg-k">🐳 服务容器</span><span class="cfg-v mono">${esc(serving.container || serving.label || "-")}${serving.image ? ` (${esc(serving.image)})` : ""}${serving.ambiguous ? " · ⚠ 同端口多容器" : ""}</span></div>` : ""}
      ${modelsStr ? `<div class="cfg-row"><span class="cfg-k">目标模型列表</span><span class="cfg-v mono">${esc(modelsStr)}</span></div>` : ""}
      <div class="cfg-row"><span class="cfg-k">精度数据集</span><span class="cfg-v">${dsTags || "无"}</span></div>
      ${subjStr ? `<div class="cfg-row"><span class="cfg-k">学科筛选</span><span class="cfg-v">${subjStr}</span></div>` : ""}
      <div class="cfg-row"><span class="cfg-k">Few-shot</span><span class="cfg-v">${cfg.few_shot ?? 0}</span></div>
      <div class="cfg-row"><span class="cfg-k">Sample limit</span><span class="cfg-v">${cfg.sample_limit || "全量"}</span></div>
      <div class="cfg-row"><span class="cfg-k">精度 max_tokens</span><span class="cfg-v">${cfg.acc_max_tokens || "自动"}</span></div>
      <div class="cfg-row"><span class="cfg-k">精度温度</span><span class="cfg-v">${cfg.acc_temperature ?? 0}</span></div>
      <div class="cfg-row"><span class="cfg-k">评测并发</span><span class="cfg-v">${cfg.acc_concurrency ?? "-"}</span></div>
      <div class="cfg-row"><span class="cfg-k">评测模式</span><span class="cfg-v">${cfg.acc_stream ? "流式" : "非流式"}</span></div>
      <div class="cfg-row"><span class="cfg-k">失败重试</span><span class="cfg-v">${cfg.max_retries ?? "-"} 次</span></div>
      ${cfg.acc_system ? `<div class="cfg-row"><span class="cfg-k">System Prompt</span><span class="cfg-v">${esc(cfg.acc_system.substring(0, 80))}</span></div>` : ""}
      ${cfg.acc_template ? `<div class="cfg-row"><span class="cfg-k">答题模板</span><span class="cfg-v">${esc(cfg.acc_template.substring(0, 80))}</span></div>` : ""}
      ${perfEnabled ? `<div class="cfg-row"><span class="cfg-k">性能并发档</span><span class="cfg-v">${(pc.levels||[]).join(",") || "-"} · ${pc.requests_per_level||"-"} 请求/档</span></div>
      <div class="cfg-row"><span class="cfg-k">性能参数</span><span class="cfg-v">max_tokens ${pc.max_tokens||"-"}${pc.min_tokens ? " · min_tokens "+pc.min_tokens : ""} · ${pc.stream ? "流式" : "非流式"} · 超时 ${pc.timeout||"-"}s${pc.warmup_requests ? " · 预热 "+pc.warmup_requests : ""}${pc.context_length ? " · 长上下文 "+pc.context_length+" tok" : ""}</span></div>
      ${pc.prompt ? `<div class="cfg-row"><span class="cfg-k">压测提示词</span><span class="cfg-v">${esc(pc.prompt.substring(0, 60))}</span></div>` : ""}` : ""}
      ${ctxEnabled ? `<div class="cfg-row"><span class="cfg-k">上下文扫描</span><span class="cfg-v">${(cfg.context_lengths||[]).join(", ")} tokens · 并发 ${cfg.context_concurrency||"-"} · ${cfg.context_requests||"-"} 请求/档 · max_tokens ${cfg.context_max_tokens||"-"} · ${cfg.context_stream !== false ? "流式" : "非流式"}</span></div>` : ""}
    </div>
  </details>`;
}

async function exportReport(id, fmt) {
  const url = `/api/tasks/${id}/export/${fmt}`;
  try {
    const r = await fetch(url);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      const msg = e.detail || r.status;
      if (fmt === "evalscope") {
        toast("无法导出 evalscope 原始报告\n\n" + msg + "\n\n说明：evalscope 原始 HTML 报告仅由精度评测（eval）生成。\n纯性能压测（perf）请使用 Excel 或 PDF 导出。", "error");
      } else {
        toast("导出失败：" + msg, "error");
      }
      return;
    }
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    const cd = r.headers.get("content-disposition") || "";
    const m = cd.match(/filename="?([^"]+)"?/);
    a.download = m ? decodeURIComponent(m[1]) : `report.${fmt === "excel" ? "xlsx" : "pdf"}`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(a.href);
  } catch (e) {
    toast("导出失败：" + e.message, "error");
  }
}

async function stopTaskFromDrawer(id, name) {
  if (!confirm(`确定停止任务「${name}」？已完成的进度会保存，可稍后续跑。`)) return;
  try {
    await fetch(`/api/tasks/${id}/stop`, { method: "POST" });
    refreshDrawerList();
  } catch (e) {
    toast("停止失败：" + e.message, "error");
  }
}

let _rerunTaskId = null;

async function rerunTask(id) {
  _rerunTaskId = id;
  try {
    const r = await fetch("/api/tasks/" + id);
    const d = await r.json();
    const cfg = d.config || {};
    const datasets = (cfg.accuracy_datasets || []).slice(0, 8);
    const moreDs = (cfg.accuracy_datasets || []).length - 8;
    const pc = cfg.perf || {};
    const ctxEnabled = (cfg.context_lengths || []).length > 0;
    let html = `<div class="rerun-config"><h4>原任务配置</h4>
      <table>
        <tr><td class="rk">模型</td><td>${esc(cfg.model || "-")}</td></tr>
        <tr><td class="rk">接口</td><td class="mono">${esc((cfg.base_url || "").substring(0,60))}</td></tr>
        <tr><td class="rk">格式</td><td>${esc(cfg.api_format || "-")} / Few-shot ${cfg.few_shot ?? 0}</td></tr>
        <tr><td class="rk">数据集</td><td>${datasets.map(d => esc(d)).join(", ") || "无"}${moreDs > 0 ? " ...+" + moreDs : ""}</td></tr>
        ${cfg.run_performance ? `<tr><td class="rk">性能</td><td>并发 ${(pc.levels||[]).join(",")} / ${pc.requests_per_level||"-"} 请求/档</td></tr>` : ""}
        ${ctxEnabled ? `<tr><td class="rk">上下文</td><td>${(cfg.context_lengths||[]).join(", ")} tokens</td></tr>` : ""}
      </table>
      <div class="field" style="margin-top:12px">
        <label>API Key <span class="opt">原 key 已脱敏，需重新输入</span></label>
        <input id="rerunApiKey" type="password" placeholder="sk-...">
      </div></div>`;
    document.getElementById("rerunBody").innerHTML = html;
    document.getElementById("rerunMask").classList.add("show");
    document.getElementById("rerunModal").classList.add("open");
  } catch (e) {
    toast("加载任务配置失败：" + e.message, "error");
  }
}

async function doRerun() {
  if (!_rerunTaskId) return;
  const apiKey = document.getElementById("rerunApiKey").value.trim();
  try {
    const r = await fetch(`/api/tasks/${_rerunTaskId}/rerun`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey }),
    });
    const data = await r.json();
    if (!r.ok) { toast("续跑失败：" + (data.detail || ""), "error");return; }
    closeRerunModal();
    closeDrawer();
    $("#console").innerHTML = "";
    $("#progressOverview").innerHTML = "";
    $("#accResults").innerHTML = ""; $("#perfResults").innerHTML = "";
    $("#ctxResults").innerHTML = "";
    sweepRows = [];
    $("#accEmpty").style.display = "block"; $("#perfEmpty").style.display = "block";
    $("#ctxEmpty").style.display = "block";
    $("#btnStart").style.display = "none"; $("#btnStop").style.display = "block";
    setStatus("运行中", "running");
    const _rt = document.querySelector('[data-tab="live"]');
    if (_rt) _rt.click();
    currentTaskId = data.task_id;
    addLog("info", "从断点续跑任务…");
    const st2 = _ensureStream(currentTaskId);
    st2.config = { model: data.name || currentTaskId };
    switchLiveTask(currentTaskId);
  } catch (e) {
    toast("续跑失败：" + e.message, "error");
  }
}

function closeRerunModal() {
  document.getElementById("rerunMask").classList.remove("show");
  document.getElementById("rerunModal").classList.remove("open");
  _rerunTaskId = null;
}
document.getElementById("rerunCancel").addEventListener("click", closeRerunModal);
document.getElementById("rerunClose").addEventListener("click", closeRerunModal);
document.getElementById("rerunMask").addEventListener("click", closeRerunModal);
document.getElementById("rerunConfirm").addEventListener("click", doRerun);

async function renameTask(id, oldName) {
  const name = prompt("重命名任务：", oldName);
  if (!name || !name.trim()) return;
  await fetch(`/api/tasks/${id}/rename`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim() }),
  });
  refreshDrawerList();
}

async function delTask(id, name) {
  if (!confirm(`确定删除任务「${name}」？可在 5 秒内撤销，也可稍后在回收站恢复。`)) return;
  try {
    const r = await fetch("/api/tasks/" + id, { method: "DELETE" });
    if (!r.ok) { toast("删除失败", "error"); return; }
    refreshDrawerList();
    toast(`已删除「${name}」`, "success", 5000, "撤销", async () => {
      const rr = await fetch(`/api/tasks/${id}/restore`, { method: "POST" });
      if (rr.ok) { toast("已恢复任务", "success"); refreshDrawerList(); }
      else toast("恢复失败", "error");
    });
  } catch (e) {
    toast("删除失败：" + e.message, "error");
  }
}


// ============ 任务对比 ============
// ============ 答题详情查看器 ============
let _reviewTaskId = null;
let _reviewDataset = null;
let _reviewPage = 1;
let _reviewFilter = "";

// Delegate click for "查看答题详情" buttons (they may be dynamically rendered)
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".btn-review-samples");
  if (!btn) return;
  const ds = btn.dataset.ds;
  // Find current task ID from the config panel or from viewed task
  if (!_reviewTaskId) {
    // Try to get task ID from the currently viewed task detail
    toast("请先从历史任务中查看任务详情", "warn");
    return;
  }
  openReviewModal(_reviewTaskId, ds);
});

async function openReviewModal(taskId, dataset) {
  _reviewTaskId = taskId;
  _reviewDataset = dataset;
  _reviewPage = 1;
  _reviewFilter = "";
  document.getElementById("reviewFilter").value = "";
  document.getElementById("reviewTitle").textContent = `答题详情 · ${dataset}`;
  document.getElementById("reviewMask").classList.add("show");
  document.getElementById("reviewModal").classList.add("open");
  await loadReviewSamples();
}

function closeReviewModal() {
  document.getElementById("reviewMask").classList.remove("show");
  document.getElementById("reviewModal").classList.remove("open");
  _reviewTaskId = null;
  _reviewDataset = null;
}
document.getElementById("reviewClose").addEventListener("click", closeReviewModal);
document.getElementById("reviewMask").addEventListener("click", closeReviewModal);

document.getElementById("reviewFilter").addEventListener("change", async () => {
  _reviewFilter = document.getElementById("reviewFilter").value;
  _reviewPage = 1;
  await loadReviewSamples();
});

document.getElementById("reviewPrev").addEventListener("click", async () => {
  if (_reviewPage > 1) { _reviewPage--; await loadReviewSamples(); }
});
document.getElementById("reviewNext").addEventListener("click", async () => {
  _reviewPage++; await loadReviewSamples();
});

async function loadReviewSamples() {
  const body = document.getElementById("reviewBody");
  body.innerHTML = '<div class="skeleton skeleton-block"></div><div class="skeleton skeleton-block"></div>';
  try {
    const params = new URLSearchParams({page: _reviewPage, page_size: 50});
    if (_reviewFilter) params.set("filter", _reviewFilter);
    const r = await fetch(`/api/tasks/${_reviewTaskId}/samples/${encodeURIComponent(_reviewDataset)}?${params}`);
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      body.innerHTML = `<div class="review-error">${esc(err.detail || '加载失败')}</div>`;
      return;
    }
    const data = await r.json();
    renderSamples(data);
  } catch (e) {
    body.innerHTML = `<div class="review-error">加载失败：${esc(e.message)}</div>`;
  }
}

function renderSamples(data) {
  const body = document.getElementById("reviewBody");
  const samples = data.samples || [];
  if (!samples.length) {
    body.innerHTML = '<div class="review-empty">暂无答题记录</div>';
    document.getElementById("reviewMeta").textContent = "0 条";
    document.getElementById("reviewPrev").disabled = true;
    document.getElementById("reviewNext").disabled = true;
    return;
  }
  body.innerHTML = samples.map(s => {
    const cls = s.is_correct ? "correct" : "wrong";
    const verdict = s.is_correct ? "✓ 正确" : "✗ 错误";
    const targetKey = s.target ? s.target.trim().toUpperCase() : "";
    const predKey = s.prediction ? s.prediction.trim().toUpperCase() : "";

    // Options with highlighting
    let optsHtml = "";
    if (s.options && s.options.length) {
      optsHtml = `<div class="sample-opts">${s.options.map(o => {
        const k = o.key.trim().toUpperCase();
        let optCls = "";
        if (k === targetKey && k === predKey) optCls = "both";
        else if (k === targetKey) optCls = "target";
        else if (k === predKey) optCls = "pred";
        return `<div class="sample-opt ${optCls}"><b>${esc(o.key)}</b>${esc(o.text)}</div>`;
      }).join("")}</div>`;
    }

    // Answer line
    let answerHtml = "";
    if (targetKey) {
      answerHtml = `<div class="sample-answer">正确答案：<b>${esc(targetKey)}</b>`;
      if (predKey && predKey !== targetKey) {
        answerHtml += ` · 模型回答：<b class="wrong">${esc(predKey)}</b>`;
      }
      answerHtml += "</div>";
    }

    // Model text output
    let outputHtml = "";
    if (s.answer_text) {
      outputHtml = `<div class="sample-output">${esc(s.answer_text)}</div>`;
    }

    // Reasoning
    let reasoningHtml = "";
    if (s.reasoning) {
      const short = s.reasoning.length > 2000 ? s.reasoning.slice(-1500) : s.reasoning;
      reasoningHtml = `<div class="sample-reasoning">${esc(short)}</div>`;
    }

    return `<div class="sample-card ${cls}">
      <div class="sample-head">
        <span class="sample-idx">#${s.index != null ? s.index + 1 : "?"}</span>
        <span class="sample-subset">${esc(s.subset || "")}</span>
        <span class="sample-verdict ${cls}">${verdict}</span>
      </div>
      <div class="sample-q">${esc(s.question)}</div>
      ${optsHtml}
      ${answerHtml}
      ${outputHtml}
      ${reasoningHtml}
    </div>`;
  }).join("");

  const totalPages = Math.ceil(data.total / data.page_size);
  document.getElementById("reviewMeta").textContent =
    `共 ${data.total} 条 · 第 ${data.page}/${totalPages || 1} 页`;
  document.getElementById("reviewPrev").disabled = data.page <= 1;
  document.getElementById("reviewNext").disabled = !data.has_more;
}

// ============ 表单记忆（localStorage）：刷新/重开自动回填上次配置 ============
// 注意：apiKey 不纳入持久化，避免明文密钥留在 localStorage
const FORM_FIELDS = ["baseUrl","model","apiFormat","servingLabel","timeout","disableThinking",
  "accConcurrency","accMaxTokens","accTemperature","maxRetries","accStream","fewShot",
  "accSystem","accTemplate","sampleLimit","runPerf","sweepLevels","perfTotal",
  "perfScaleMode","perfScaleMult","perfMaxTokens","perfMinTokens","perfStream",
  "contextLength","perfPrompt","perfTemperature","perfSystem","perfTimeout","perfWarmup",
  "runCtxScan","ctxLengths","ctxConcurrency","ctxRequests","ctxMaxTokens","ctxStream"];
const LS_KEY = "evalbench_form_v1";
function saveForm() {
  const data = {};
  FORM_FIELDS.forEach(id => {
    const el = $("#" + id);
    if (el) data[id] = el.type === "checkbox" ? el.checked : el.value;
  });
  try { localStorage.setItem(LS_KEY, JSON.stringify(data)); } catch (e) {}
}
function loadForm() {
  let data = {};
  try { data = JSON.parse(localStorage.getItem(LS_KEY) || "{}"); } catch (e) {}
  if ("apiKey" in data) {  // 清理旧版本遗留的明文 apiKey
    delete data.apiKey;
    try { localStorage.setItem(LS_KEY, JSON.stringify(data)); } catch (e) {}
  }
  FORM_FIELDS.forEach(id => {
    if (!(id in data)) return;
    const el = $("#" + id);
    if (!el) return;
    if (el.type === "checkbox") { el.checked = !!data[id]; el.dispatchEvent(new Event("change")); }
    else el.value = data[id];
  });
}
FORM_FIELDS.forEach(id => {
  const el = $("#" + id);
  if (el) { el.addEventListener("input", saveForm); el.addEventListener("change", saveForm); }
});
loadForm();

// ============ 刷新页面后自动重连运行中的任务（多任务） ============
async function resumeRunningTasks() {
  try {
    const r = await fetch("/api/tasks");
    const data = await r.json();
    const running = (data.tasks || []).filter(t => t.status === "running");
    if (!running.length) return;
    for (const t of running) {
      try {
        const dr = await fetch("/api/tasks/" + t.id);
        const d = await dr.json();
        const st = _ensureStream(t.id);
        st.config = d.config || null;
        (d.logs || []).forEach(l => {
          if (l.level) st.logs.push({ level: l.level, msg: l.msg, ts: l.ts });
        });
        if (d.sweep_levels) st.sweepRows = d.sweep_levels.map(e => e.row);
      } catch (e) { /* 单个任务失败不影响其他 */ }
    }
    connectStream(running[0].id);
    addLog("info", `页面刷新，已重新连接 ${running.length} 个运行中的任务`);
    _renderTaskSwitcher();
  } catch (e) { /* 忽略，按无任务处理 */ }
}
resumeRunningTasks();

// ─────────── 模型档案：配置持久化（服务端 /app/data/profiles.json） ───────────
async function loadProfiles(selectName) {
  try {
    const r = await fetch("/api/profiles");
    const d = await r.json();
    window._profiles = d.profiles || {};
    const sel = $("#profileSelect");
    if (!sel) return;
    sel.innerHTML = '<option value="">不使用档案（手动配置）</option>';
    Object.keys(window._profiles).sort().forEach(n => {
      const o = document.createElement("option");
      o.value = n; o.textContent = n;
      sel.appendChild(o);
    });
    if (selectName && window._profiles[selectName]) sel.value = selectName;
    renderBatchList();
  } catch (e) { /* 档案服务不可用不影响主流程 */ }
}

function applyProfile(name) {
  const p = (window._profiles || {})[name];
  if (!p) return;
  const set = (id, v) => { if (v === undefined || v === null) return; const el = $("#" + id); if (el) el.value = v; };
  set("baseUrl", p.base_url);
  set("apiKey", p.api_key);
  set("model", p.model);
  set("apiFormat", p.api_format || "openai");
  set("timeout", p.timeout || 120);
  set("accConcurrency", p.acc_concurrency);
  set("accMaxTokens", p.acc_max_tokens || "");
  set("accTemperature", p.acc_temperature);
  set("accStream", p.acc_stream ? "true" : "false");
  $("#disableThinking").checked = !!p.disable_thinking;
  const hint = $("#presetHint");
  if (hint) { hint.textContent = `已载入档案「${name}」`; hint.classList.add("active"); }
}

async function saveProfile() {
  const defName = $("#model").value.trim();
  const name = (prompt("保存为模型档案（同名覆盖）：", defName) || "").trim();
  if (!name) return;
  const cfg = {
    base_url: $("#baseUrl").value.trim(),
    api_key: $("#apiKey").value.trim(),
    model: $("#model").value.trim(),
    api_format: $("#apiFormat").value,
    timeout: parseFloat($("#timeout").value) || 120,
    disable_thinking: $("#disableThinking").checked,
    acc_concurrency: parseInt($("#accConcurrency").value) || 4,
    acc_max_tokens: parseInt($("#accMaxTokens").value) || 0,
    acc_temperature: parseFloat($("#accTemperature").value) || 0,
    acc_stream: $("#accStream").value === "true",
  };
  try {
    const r = await fetch("/api/profiles", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, config: cfg }),
    });
    const d = await r.json();
    if (!r.ok) { toast("保存失败：" + (d.detail || r.status), "error");return; }
    await loadProfiles(name);
    const hint = $("#presetHint");
    if (hint) { hint.textContent = `档案「${name}」已保存`; hint.classList.add("active"); }
  } catch (e) { toast("保存失败：" + e, "error"); }
}

async function deleteProfile() {
  const name = $("#profileSelect").value;
  if (!name) return;
  if (!confirm(`删除模型档案「${name}」？`)) return;
  try {
    await fetch("/api/profiles/" + encodeURIComponent(name), { method: "DELETE" });
    await loadProfiles();
  } catch (e) { /* 忽略 */ }
}

$("#profileSelect").addEventListener("change", (e) => {
  if (e.target.value) applyProfile(e.target.value);
});
$("#btnProfileSave").addEventListener("click", saveProfile);
$("#btnProfileDel").addEventListener("click", deleteProfile);
loadProfiles();

// ─────────── 成绩单：模型 × 数据集 分数矩阵 ───────────
async function loadBoard() {
  const body = $("#boardBody");
  if (!body) return;
  try {
    const r = await fetch("/api/leaderboard");
    const d = await r.json();
    renderBoard(d);
  } catch (e) {
    body.innerHTML = '<div class="empty-state">成绩单加载失败</div>';
  }
}

function renderBoard(d) {
  const body = $("#boardBody");
  const models = d.models || [], datasets = d.datasets || [], cells = d.cells || {};
  if (!models.length || !datasets.length) {
    body.innerHTML = '<div class="empty-state">还没有已完成的评测任务，跑一个试试。</div>';
    return;
  }
  // 每列最佳分（用于高亮；单模型列不高亮）
  const best = {};
  datasets.forEach(ds => {
    let b = -1, cnt = 0;
    models.forEach(m => {
      const c = cells[m + "|" + ds];
      if (c) { cnt++; if (c.score > b) b = c.score; }
    });
    best[ds] = { score: b, multi: cnt > 1 };
  });
  let html = '<div class="table-scroll"><table class="board-table"><thead><tr><th>模型</th>';
  datasets.forEach(ds => { html += `<th>${esc(ds)}</th>`; });
  html += "</tr></thead><tbody>";
  models.forEach(m => {
    html += `<tr><td class="board-model">${esc(m)}</td>`;
    datasets.forEach(ds => {
      const c = cells[m + "|" + ds];
      if (!c) { html += '<td class="board-empty">—</td>'; return; }
      const isBest = best[ds].multi && c.score >= best[ds].score;
      let delta = "";
      if (c.prev !== null && c.prev !== undefined) {
        const diff = +(c.score - c.prev).toFixed(2);
        if (diff > 0) delta = `<span class="bd-up">▲${diff}</span>`;
        else if (diff < 0) delta = `<span class="bd-down">▼${Math.abs(diff)}</span>`;
        else delta = '<span class="bd-flat">=</span>';
      }
      const dt = c.finished_at ? new Date(c.finished_at * 1000) : null;
      const dstr = dt ? `${dt.getMonth() + 1}/${dt.getDate()}` : "";
      html += `<td class="board-cell${isBest ? " best" : ""}" data-task="${c.task_id}"` +
        ` title="${esc(c.task_name || "")}&#10;${dstr} · ${c.n} 样本 · 累计 ${c.runs} 次评测&#10;点击查看任务">` +
        `<div class="bd-score">${c.score}</div>` +
        `<div class="bd-meta">${delta}<span class="bd-n">n=${c.n}</span></div></td>`;
    });
    html += "</tr>";
  });
  html += "</tbody></table></div>";
  body.innerHTML = html;
  body.querySelectorAll(".board-cell").forEach(td => {
    td.addEventListener("click", () => {
      if (td.dataset.task) viewTask(td.dataset.task);
    });
  });
}

const _boardTab = document.querySelector('[data-tab="board"]');
if (_boardTab) _boardTab.addEventListener("click", loadBoard);
const _btnBoardRefresh = $("#btnBoardRefresh");
if (_btnBoardRefresh) _btnBoardRefresh.addEventListener("click", loadBoard);
loadBoard();

// ─────────── 批量评测：多模型 × 同一套评测配置 ───────────
window._batchModels = window._batchModels || new Set();

function renderBatchList() {
  const list = $("#batchList");
  if (!list) return;
  const names = Object.keys(window._profiles || {}).sort();
  // 清理已删除档案的勾选
  [...window._batchModels].forEach(n => { if (!names.includes(n)) window._batchModels.delete(n); });
  const hint = $("#batchHint");
  if (hint) hint.style.display = window._batchModels.size ? "block" : "none";
  if (!names.length) {
    list.innerHTML = '<span class="batch-empty">暂无档案，先在上方保存模型档案</span>';
    return;
  }
  list.innerHTML = "";
  names.forEach(n => {
    const chip = document.createElement("span");
    chip.className = "batch-chip" + (window._batchModels.has(n) ? " on" : "");
    chip.textContent = n;
    chip.title = "点击勾选/取消";
    chip.addEventListener("click", () => {
      if (window._batchModels.has(n)) window._batchModels.delete(n);
      else window._batchModels.add(n);
      chip.classList.toggle("on");
      if (hint) hint.style.display = window._batchModels.size ? "block" : "none";
    });
    list.appendChild(chip);
  });
}

async function batchStart(cfg) {
  if (cfg.accuracy_datasets.length === 0 && !cfg.run_performance && cfg.context_lengths.length === 0) {
    toast("请至少选择一个精度数据集、性能压测或上下文扫描", "warn");return;
  }
  const names = [...window._batchModels];
  if (!names.length) return;
  const now = new Date();
  const ts = `${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")} ${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
  const results = [];
  for (const n of names) {
    const p = (window._profiles || {})[n];
    if (!p) { results.push({ n, ok: false, err: "档案不存在" }); continue; }
    const c = Object.assign({}, cfg, {
      base_url: p.base_url || "",
      api_key: p.api_key || "",
      model: p.model || "",
      api_format: p.api_format || "openai",
      timeout: p.timeout || cfg.timeout,
      disable_thinking: !!p.disable_thinking,
      task_name: `${p.model || n} · 批量 · ${ts}`,
    });
    if (!c.base_url) { results.push({ n, ok: false, err: "档案缺少 base_url" }); continue; }
    if (!["openai", "vllm", "anthropic"].includes(c.api_format)) {
      results.push({ n, ok: false, err: "接口格式不支持正式评测" }); continue;
    }
    try {
      const r = await fetch("/api/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(c),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "启动失败");
      results.push({ n, ok: true, id: d.task_id, pos: d.queue_position });
    } catch (e) {
      results.push({ n, ok: false, err: String(e.message || e) });
    }
  }
  // 汇总反馈
  const oks = results.filter(r => r.ok);
  const fails = results.filter(r => !r.ok);
  $("#console").innerHTML = "";
  addLog("info", `批量评测：已提交 ${oks.length}/${results.length} 个任务（队列最多同时跑 3 个）`);
  oks.forEach(r => addLog("success", `✔ ${r.n} → 任务 ${r.id}${r.pos > 0 ? `（排队 #${r.pos}）` : "（已启动）"}`));
  fails.forEach(r => addLog("error", `✘ ${r.n} → ${r.err}`));
  if (oks.length) {
    setStatus("运行中", "running");
    currentTaskId = oks[0].id;
    const st = _ensureStream(currentTaskId);
    st.config = cfg;
    switchLiveTask(currentTaskId);
    const lt = document.querySelector('[data-tab="live"]');
    if (lt) lt.click();
    // 任务跑完后成绩单可见：自动刷新一次
    setTimeout(loadBoard, 5000);
  }
}

// ─────────── Toast 通知（替代 alert） ───────────
function toast(msg, type = "info", ms = 4200, actionLabel = null, actionFn = null) {
  let wrap = $("#toastWrap");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.id = "toastWrap";
    wrap.className = "toast-wrap";
    document.body.appendChild(wrap);
  }
  const icons = { info: "ℹ", success: "✓", warn: "⚠", error: "✕" };
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.innerHTML = `<span class="t-ico">${icons[type] || icons.info}</span><span class="t-msg"></span>${actionLabel ? '<button class="t-act"></button>' : ""}<button class="t-close" title="关闭">✕</button>`;
  el.querySelector(".t-msg").textContent = msg;
  if (actionLabel) {
    const a = el.querySelector(".t-act");
    a.textContent = actionLabel;
    a.addEventListener("click", () => { dismiss(); if (actionFn) actionFn(); });
  }
  el.querySelector(".t-close").addEventListener("click", () => dismiss());
  wrap.appendChild(el);
  let dismissed = false;
  function dismiss() {
    if (dismissed) return;
    dismissed = true;
    el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  }
  if (ms > 0) setTimeout(dismiss, ms);
  return dismiss;
}

// ─────────── 任务完成标题闪烁提醒 ───────────
const _origTitle = document.title;
let _titleTimer = null;
function flashTitle(msg) {
  if (!document.hidden) return;   // 页面在前台则不打扰
  stopFlashTitle();
  let on = false;
  _titleTimer = setInterval(() => {
    document.title = on ? _origTitle : msg;
    on = !on;
  }, 1200);
}
function stopFlashTitle() {
  if (_titleTimer) { clearInterval(_titleTimer); _titleTimer = null; }
  if (document.title !== _origTitle) document.title = _origTitle;
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) stopFlashTitle(); });

