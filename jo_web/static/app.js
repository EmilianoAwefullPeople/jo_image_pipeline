const STAGES = ["queued", "inventorying", "extracting", "thumbnailing", "grouping", "evaluating", "complete"];
const POLL_INTERVAL_MS = 1200;

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const pickButton = document.getElementById("pick");
const uploadStatus = document.getElementById("upload-status");
const uploadBar = document.getElementById("upload-bar");
const uploadDetail = document.getElementById("upload-detail");
const dropzoneHint = document.getElementById("dropzone-hint");

let busy = false;

pickButton.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => submit(Array.from(fileInput.files)));

dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropzone.classList.add("over");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("over"));
dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("over");
  submit(Array.from(event.dataTransfer.files));
});

function show(id, visible) {
  document.getElementById(id).hidden = !visible;
}

function text(id, value) {
  document.getElementById(id).textContent = value;
}

function stamp(isoValue) {
  if (!isoValue) return "unknown";
  return new Date(isoValue).toISOString().replace("T", " ").slice(0, 19) + " UTC";
}

async function submit(files) {
  if (busy || !files.length) return;
  busy = true;
  pickButton.disabled = true;
  ["progress-panel", "error-panel", "summary-panel", "groups-panel", "reliability-panel"].forEach((id) => show(id, false));

  try {
    const created = await postJson("/api/runs");
    dropzoneHint.textContent = `Accepting up to ${created.limits.max_files} files`;
    show("upload-status", true);
    const accepted = await uploadAll(created.run_id, files.slice(0, created.limits.max_files));
    if (!accepted) throw new Error("None of those files could be used. Upload HEIC, JPEG, PNG or TIFF images.");
    await postJson(`/api/runs/${created.run_id}/start`);
    show("upload-status", false);
    show("progress-panel", true);
    text("run-id", created.run_id.slice(0, 12));
    text("run-created", stamp(created.created_utc));
    await poll(created.run_id);
  } catch (error) {
    show("upload-status", false);
    show("error-panel", true);
    text("error-detail", error.message);
  } finally {
    busy = false;
    pickButton.disabled = false;
    fileInput.value = "";
  }
}

async function uploadAll(runId, files) {
  let accepted = 0;
  for (let index = 0; index < files.length; index += 1) {
    uploadDetail.textContent = `Uploading ${index + 1} of ${files.length}: ${files[index].name}`;
    uploadBar.style.width = `${Math.round((index / files.length) * 100)}%`;
    const body = new FormData();
    body.append("file", files[index]);
    const response = await fetch(`/api/runs/${runId}/files`, { method: "POST", body });
    if (response.ok) {
      const result = await response.json();
      if (result.accepted) accepted += 1;
    }
  }
  uploadBar.style.width = "100%";
  uploadDetail.textContent = `${accepted} of ${files.length} files accepted`;
  return accepted;
}

async function postJson(url) {
  const response = await fetch(url, { method: "POST" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || "Request failed");
  }
  return response.json();
}

async function poll(runId) {
  while (true) {
    const response = await fetch(`/api/runs/${runId}`);
    if (!response.ok) throw new Error("This run has expired or was removed.");
    const state = await response.json();
    renderProgress(state);
    if (state.status === "failed") {
      show("error-panel", true);
      text("error-detail", state.failure_detail || "The run failed.");
      return;
    }
    if (state.status === "complete") {
      render(runId, state);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }
}

function renderProgress(state) {
  const current = STAGES.indexOf(state.progress.stage);
  const list = document.getElementById("stages");
  list.innerHTML = "";
  STAGES.forEach((stage, index) => {
    const item = document.createElement("li");
    item.textContent = stage;
    if (index < current) item.className = "done";
    if (index === current) item.className = "active";
    list.appendChild(item);
  });
  const { done, total, queue_depth: depth } = state.progress;
  if (state.progress.stage === "queued" && depth > 1) {
    text("progress-detail", `Waiting behind ${depth - 1} other run(s).`);
  } else {
    text("progress-detail", total ? `${done} of ${total}` : "");
  }
}

function render(runId, state) {
  renderSummary(state);
  renderGroups(runId, state);
  renderReliability(state);
}

function renderSummary(state) {
  const llm = state.llm.summary;
  const tiles = [
    { label: "Files received", value: state.files.length },
    { label: "Images analysed", value: state.extraction.images_analysed },
    { label: "Moments", value: state.groups.length },
    { label: "Evaluated", value: llm ? llm.images_evaluated : 0 },
    { label: "Model cost", value: llm ? `$${llm.total_cost_usd.toFixed(3)}` : "$0.000" },
  ];
  document.getElementById("summary-tiles").innerHTML = tiles
    .map((tile) => `<div class="tile"><div class="value">${tile.value}</div><div class="label">${tile.label}</div></div>`)
    .join("");

  const notes = [];
  const failures = Object.keys(state.extraction.failures).length;
  if (state.skipped.length) notes.push(`${state.skipped.length} file(s) skipped: ${state.skipped.map((item) => `${item.filename} (${item.reason})`).join(", ")}`);
  if (failures) notes.push(`${failures} file(s) could not be read`);
  if (llm && llm.request_failed) notes.push(`${llm.request_failed} image(s) failed evaluation`);
  if (!state.groups.length) notes.push("No moments could be proposed. Images with no capture timestamp cannot be placed on a timeline.");
  text("skipped-detail", notes.join(". "));
  show("summary-panel", true);
}

function renderGroups(runId, state) {
  const evaluations = {};
  state.llm.records.forEach((record) => {
    evaluations[record.sha256.slice(0, 16)] = record.evaluation;
  });

  document.getElementById("groups").innerHTML = state.groups
    .map((group) => {
      const located = group.evidence.located_members || 0;
      const unlocated = group.evidence.unlocated_members || 0;
      const shots = group.members
        .map((member) => {
          const key = state.thumbnails[member.relative_path];
          const image = key
            ? `<img src="/api/runs/${runId}/thumbnails/${key}" alt="" loading="lazy">`
            : `<img alt="" loading="lazy">`;
          const badge = member.membership === "member" ? "" : `<span class="badge ${member.membership}">${member.membership}</span> `;
          return `<div class="shot">${image}<div class="caption">${badge}${escapeHtml(member.relative_path)}${renderEvaluation(evaluations[key])}</div></div>`;
        })
        .join("");
      return `<div class="group">
        <div class="group-head"><h3>${escapeHtml(group.label)}</h3>
        <span class="group-meta">${group.members.length} photos, score ${group.score.toFixed(2)}, ${located} located, ${unlocated} unlocated</span></div>
        <div class="shots">${shots}</div>
      </div>`;
    })
    .join("");
  show("groups-panel", state.groups.length > 0);
}

function renderEvaluation(evaluation) {
  if (!evaluation) return "";
  const emotions = evaluation.emotions.map((item) => `<span class="tag">${escapeHtml(item.label)}</span>`).join("");
  const landmark = evaluation.landmark.name ? `<p><strong>${escapeHtml(evaluation.landmark.name)}</strong></p>` : "";
  return `<div class="evaluation">
    <p>${escapeHtml(evaluation.caption)}</p>
    ${landmark}
    <p class="tags"><span class="tag">${escapeHtml(evaluation.scene.scene_type)}</span><span class="tag">keep: ${escapeHtml(evaluation.memory.keep_signal)}</span>${emotions}</p>
  </div>`;
}

function renderReliability(state) {
  const body = document.querySelector("#reliability tbody");
  body.innerHTML = state.extraction.rows
    .map((row) => {
      const reasons = Object.entries(row.unknown_reasons).sort((left, right) => right[1] - left[1]);
      const leading = reasons.length ? reasons[0][0] : "";
      const rate = Math.round(row.presence_rate * 100);
      return `<tr><td>${escapeHtml(row.category)}</td><td>${escapeHtml(row.field)}</td><td>${row.present}/${row.total}</td>
        <td class="${rate < 50 ? "rate-low" : ""}">${rate}%</td><td>${escapeHtml(leading || "")}</td></tr>`;
    })
    .join("");
  show("reliability-panel", state.extraction.rows.length > 0);
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = value === null || value === undefined ? "" : String(value);
  return node.innerHTML;
}
