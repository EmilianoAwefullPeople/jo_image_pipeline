const STAGES = ["queued", "inventorying", "extracting", "thumbnailing", "grouping", "evaluating", "refining", "complete"];
const POLL_INTERVAL_MS = 1200;
const MEGABYTE = 1024 * 1024;
const RESULT_PANELS = ["progress-panel", "error-panel", "summary-panel", "groups-panel", "evaluations-panel", "reliability-panel"];
const EXTRACTION_FIELDS = [
  { label: "1. General description", value: (item) => escapeHtml(item.general_description) },
  { label: "2. Scene / setting type", value: (item) => tags(withOther(item.scene_setting)) },
  { label: "3. Landmark / point of interest", value: (item) => landmarkValue(item.landmark) },
  { label: "4. Notable subjects", value: (item) => tags(item.notable_subjects) },
  { label: "5. Focal point type", value: (item) => tags(item.focal_points) },
  { label: "6. Activity depicted", value: (item) => tags(withOther(item.activity)) },
  { label: "7. Environment & cultural style", value: (item) => environmentValue(item.environment) },
  { label: "8. Composition / framing", value: (item) => tags(item.composition) },
  { label: "9. Visual weather condition", value: (item) => tags(item.weather) },
  { label: "10. Keyword tags", value: (item) => tags(item.keyword_tags) },
  { label: "11. Photographic style", value: (item) => tags(withOther(item.photographic_style)) },
  { label: "Screenshot or document", value: (item) => screenshotValue(item.screenshot) },
  { label: "Keep signal", value: (item) => `<span class="tag">${escapeHtml(item.memory.keep_signal)}</span><span class="tag">${item.memory.confidence.toFixed(2)}</span>${note(item.memory.reason)}` },
  { label: "Representative quality", value: (item) => `<span class="tag">${item.representative_quality.score.toFixed(2)}</span>${note(item.representative_quality.reasoning)}` },
];

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const pickButton = document.getElementById("pick");
const uploadStatus = document.getElementById("upload-status");
const uploadBar = document.getElementById("upload-bar");
const uploadDetail = document.getElementById("upload-detail");
const dropzoneHint = document.getElementById("dropzone-hint");
const setGrid = document.getElementById("set-grid");
const processButton = document.getElementById("process");
const promptSystem = document.getElementById("prompt-system");
const promptUser = document.getElementById("prompt-user");
const promptReset = document.getElementById("prompt-reset");
const promptState = document.getElementById("prompt-state");

let currentSet = null;
let uploading = false;
let processing = false;
let defaultPrompt = null;

pickButton.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => addFiles(Array.from(fileInput.files)));
processButton.addEventListener("click", process);
promptSystem.addEventListener("input", renderPromptState);
promptUser.addEventListener("input", renderPromptState);
promptReset.addEventListener("click", () => {
  if (!defaultPrompt) return;
  promptSystem.value = defaultPrompt.system;
  promptUser.value = defaultPrompt.user_template;
  renderPromptState();
});

loadPrompt();

dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropzone.classList.add("over");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("over"));
dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("over");
  addFiles(Array.from(event.dataTransfer.files));
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

async function loadPrompt() {
  const response = await fetch("/api/prompt");
  if (!response.ok) {
    text("prompt-detail", "The default prompt could not be loaded, runs will use the prompt stored on the server.");
    return;
  }
  defaultPrompt = await response.json();
  promptSystem.value = defaultPrompt.system;
  promptUser.value = defaultPrompt.user_template;
  renderPromptState();
}

function promptEdited() {
  if (!defaultPrompt) return false;
  return promptSystem.value.trim() !== defaultPrompt.system.trim() || promptUser.value.trim() !== defaultPrompt.user_template.trim();
}

function renderPromptState() {
  const edited = promptEdited();
  promptState.textContent = edited ? "edited" : "default";
  promptState.className = edited ? "prompt-state edited" : "prompt-state";
  text("prompt-detail", edited ? "The next run will use this prompt." : "");
}

async function applyPrompt(runId) {
  if (!promptEdited()) return;
  const response = await fetch(`/api/runs/${runId}/prompt`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ system: promptSystem.value, user_template: promptUser.value }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || "That prompt was refused");
  }
  text("prompt-detail", "This run is using the edited prompt.");
}

async function addFiles(files) {
  if (uploading || processing || !files.length) return;
  uploading = true;
  pickButton.disabled = true;
  renderProcessButton();

  try {
    if (!currentSet || currentSet.started) await openSet();
    show("upload-status", true);
    await uploadAll(files);
    show("set-panel", true);
  } catch (error) {
    show("error-panel", true);
    text("error-detail", error.message);
  } finally {
    show("upload-status", false);
    uploading = false;
    pickButton.disabled = false;
    fileInput.value = "";
    renderProcessButton();
  }
}

async function openSet() {
  const created = await postJson("/api/runs");
  currentSet = { runId: created.run_id, createdUtc: created.created_utc, limits: created.limits, accepted: 0, skipped: [], started: false };
  dropzoneHint.textContent = `Accepting up to ${created.limits.max_files} files`;
  setGrid.innerHTML = "";
  RESULT_PANELS.forEach((id) => show(id, false));
  renderSetCount();
}

async function uploadAll(files) {
  const room = Math.max(0, currentSet.limits.max_files - currentSet.accepted);
  if (files.length > room) {
    currentSet.skipped.push({ filename: `${files.length - room} file(s)`, reason: `the set is capped at ${currentSet.limits.max_files} photos` });
  }
  const queued = files.slice(0, room);
  for (let index = 0; index < queued.length; index += 1) {
    uploadDetail.textContent = `Uploading ${index + 1} of ${queued.length}: ${queued[index].name}`;
    uploadBar.style.width = `${Math.round((index / queued.length) * 100)}%`;
    const body = new FormData();
    body.append("file", queued[index]);
    const response = await fetch(`/api/runs/${currentSet.runId}/files`, { method: "POST", body });
    const result = await response.json().catch(() => ({ detail: response.statusText }));
    if (response.ok && result.accepted) {
      currentSet.accepted += 1;
      setGrid.insertAdjacentHTML("beforeend", setTile(currentSet.runId, result));
    } else {
      currentSet.skipped.push({ filename: queued[index].name, reason: result.reason || result.detail || "refused" });
    }
    renderSetCount();
  }
  uploadBar.style.width = "100%";
}

function setTile(runId, uploaded) {
  const image = uploaded.thumbnail ? `<img src="/api/runs/${runId}/thumbnails/${uploaded.thumbnail}" alt="" loading="lazy">` : `<img alt="" loading="lazy">`;
  return `<div class="shot">${image}<div class="caption">${escapeHtml(uploaded.filename)}<div class="member-reason">${(uploaded.size_bytes / MEGABYTE).toFixed(1)} MB</div></div></div>`;
}

function renderSetCount() {
  const count = currentSet ? currentSet.accepted : 0;
  const skipped = currentSet ? currentSet.skipped : [];
  text("set-count", `${count} photo${count === 1 ? "" : "s"}`);
  text("set-skipped", skipped.length ? `${skipped.length} file(s) not added: ${skipped.map((item) => `${item.filename} (${item.reason})`).join(", ")}` : "");
}

function renderProcessButton() {
  processButton.disabled = uploading || processing || !currentSet || currentSet.started || !currentSet.accepted;
}

async function process() {
  if (uploading || processing || !currentSet || currentSet.started || !currentSet.accepted) return;
  processing = true;
  pickButton.disabled = true;
  renderProcessButton();
  show("error-panel", false);
  const set = currentSet;

  try {
    await applyPrompt(set.runId);
    await postJson(`/api/runs/${set.runId}/start`);
    set.started = true;
    show("progress-panel", true);
    text("run-id", set.runId.slice(0, 12));
    text("run-created", stamp(set.createdUtc));
    await poll(set.runId);
  } catch (error) {
    show("error-panel", true);
    text("error-detail", error.message);
  } finally {
    processing = false;
    pickButton.disabled = false;
    renderProcessButton();
  }
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
  renderEvaluations(runId, state);
  renderReliability(state);
}

function renderSummary(state) {
  const llm = state.llm.summary;
  const dropped = state.groups.reduce((total, group) => total + (group.evidence.excluded_by_signal || []).length, 0);
  const tiles = [
    { label: "Files received", value: state.files.length },
    { label: "Images analysed", value: state.extraction.images_analysed },
    { label: "Moments", value: state.groups.length },
    { label: "Left out by model", value: dropped },
    { label: "Evaluated", value: llm ? llm.images_evaluated : 0 },
    { label: "Model cost", value: llm ? `$${llm.total_cost_usd.toFixed(3)}` : "$0.000" },
    { label: "Prompt", value: escapeHtml(state.llm.prompt_version) },
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

  renderRefinementDetail(state);

  document.getElementById("groups").innerHTML = state.groups
    .map((group) => {
      const located = group.evidence.located_members || 0;
      const unlocated = group.evidence.unlocated_members || 0;
      const shots = group.members.map((member) => shot(runId, state, evaluations, member)).join("");
      const baseline = group.evidence.baseline_score;
      const scoreNote = baseline === undefined
        ? `score ${group.score.toFixed(2)}`
        : `score ${group.score.toFixed(2)} (was ${baseline.toFixed(2)})`;
      return `<div class="group">
        <div class="group-head"><h3>${escapeHtml(group.label)}</h3>
        <span class="group-meta">${group.members.length} photos, ${scoreNote}, ${located} located, ${unlocated} unlocated</span></div>
        ${groupReasons(group)}
        <div class="shots">${shots}</div>
        ${renderExcluded(runId, state, group)}
      </div>`;
    })
    .join("");
  show("groups-panel", state.groups.length > 0);
}

function shot(runId, state, evaluations, member) {
  const key = state.thumbnails[member.relative_path];
  const image = key ? `<img src="/api/runs/${runId}/thumbnails/${key}" alt="" loading="lazy">` : `<img alt="" loading="lazy">`;
  const badge = member.membership === "member" ? "" : `<span class="badge ${member.membership}">${member.membership}</span> `;
  return `<div class="shot">${image}<div class="caption">${badge}${escapeHtml(member.relative_path)}${memberReason(member)}${renderEvaluation(evaluations[key])}</div></div>`;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "unknown";
  if (seconds < 90) return `${Math.round(seconds)} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function metres(value) {
  if (value === null || value === undefined) return "unknown";
  if (value < 1000) return `${Math.round(value)} m`;
  return `${(value / 1000).toFixed(1)} km`;
}

function memberReason(member) {
  const evidence = member.evidence || {};
  const lines = [];
  if (member.membership === "representative" && evidence.reason) {
    lines.push(`<div class="member-reason">${escapeHtml(evidence.reason)}</div>`);
  }
  if (evidence.canonical) {
    lines.push(`<div class="member-reason link">${attachmentReason(evidence)}</div>`);
  }
  return lines.join("");
}

function attachmentReason(evidence) {
  if (evidence.gap_seconds !== undefined) {
    return `burst frame of ${escapeHtml(evidence.canonical)}, ${duration(evidence.gap_seconds)} apart and ${evidence.hash_distance} bits different`;
  }
  if (evidence.hash_distance !== undefined) {
    return `near copy of ${escapeHtml(evidence.canonical)}, ${evidence.hash_distance} bits different`;
  }
  return `identical file content to ${escapeHtml(evidence.canonical)}`;
}

function boundaryReason(boundary) {
  if (boundary.kind === "time_gap") {
    return `a gap of ${duration(boundary.gap_seconds)} after ${escapeHtml(boundary.after)}, longer than the ${duration(boundary.window_seconds)} window in force there`;
  }
  return `a move of ${metres(boundary.distance_metres)} after ${escapeHtml(boundary.after)}, past the ${metres(boundary.threshold_metres)} place threshold`;
}

function timeReasons(evidence) {
  const reasons = [];
  if (evidence.primary_count > 1) {
    reasons.push(`${evidence.primary_count} photos taken over ${duration(evidence.span_seconds)}, with nothing between them far enough apart to break the moment`);
  } else {
    reasons.push("one photo on its own, with nothing close enough in time to join it");
  }
  if (evidence.closest_call) {
    const closest = evidence.closest_call;
    reasons.push(`nearest it came to splitting: a gap of ${duration(closest.gap_seconds)} against the ${duration(closest.window_seconds)} window the shooting cadence allowed there`);
  }
  return reasons;
}

function placeReason(evidence) {
  if (evidence.located_members === 0) {
    return `no photo here carries a coordinate, so location neither held the moment together nor split it`;
  }
  if (evidence.located_members === 1) {
    return `only one photo here carries a coordinate, so location could not be used to split the moment`;
  }
  const unlocated = evidence.unlocated_members ? `, and ${evidence.unlocated_members} without a coordinate stayed on capture time alone` : "";
  return `the located photos span ${metres(evidence.max_distance_metres)}, inside the ${metres(evidence.place_threshold_metres)} place threshold${unlocated}`;
}

function groupReasons(group) {
  const evidence = group.evidence;
  if (evidence.span_seconds === undefined) {
    return `<ul class="reasons"><li>${escapeHtml(evidence.reason || "no grouping evidence was recorded")}</li></ul>`;
  }

  const reasons = timeReasons(evidence);
  reasons.push(placeReason(evidence));
  if (evidence.attached_count) {
    reasons.push(`${evidence.attached_count} frame(s) attached to a photo already here as a duplicate or burst frame`);
  }
  reasons.push(evidence.opened_by ? `split from the moment before it by ${boundaryReason(evidence.opened_by)}` : "first moment in the set, nothing came before it");
  reasons.push(evidence.closed_by ? `closed by ${boundaryReason(evidence.closed_by)}` : "last moment in the set, nothing came after it");
  return `<ul class="reasons">${reasons.map((reason) => `<li>${reason}</li>`).join("")}</ul>`;
}

function renderExcluded(runId, state, group) {
  const excluded = group.evidence.excluded_by_signal || [];
  const flagged = group.evidence.screenshots_flagged || [];
  if (!excluded.length && !flagged.length) return "";

  const rows = excluded.map((entry) => {
    const key = state.thumbnails[entry.relative_path];
    const image = key ? `<img src="/api/runs/${runId}/thumbnails/${key}" alt="" loading="lazy">` : `<img alt="" loading="lazy">`;
    return `<div class="shot">${image}<div class="caption"><span class="badge duplicate">left out</span> ${escapeHtml(entry.relative_path)}
      <div class="evaluation"><p>${escapeHtml(entry.reason)}</p></div></div></div>`;
  }).join("");

  const flagNote = flagged.length
    ? `<p class="muted">${flagged.length} screenshot or document flagged for review, kept in the moment: ${flagged.map((item) => escapeHtml(item.relative_path)).join(", ")}</p>`
    : "";
  const excludedNote = excluded.length ? `<p class="muted">The model read these as not worth keeping. They stay available and the traveller can put them back.</p>${`<div class="shots">${rows}</div>`}` : "";
  return `<div class="evaluation">${excludedNote}${flagNote}</div>`;
}

function renderRefinementDetail(state) {
  const refined = state.groups;
  const baseline = state.baseline_groups;
  if (!state.llm.summary || !baseline.length) {
    text("refinement-detail", "");
    return;
  }

  const dropped = refined.reduce((total, group) => total + (group.evidence.excluded_by_signal || []).length, 0);
  const reelected = refined.filter((group) =>
    group.members.some((member) => member.membership === "representative"
      && member.evidence.previous_representative
      && member.evidence.previous_representative !== member.relative_path)
  ).length;
  const flagged = refined.reduce((total, group) => total + (group.evidence.screenshots_flagged || []).length, 0);
  text(
    "refinement-detail",
    `The model changed this result: ${baseline.length} proposals became ${refined.length}, ${dropped} photo(s) dropped as not worth keeping, `
      + `${reelected} representative(s) re-elected on composition rather than sharpness, ${flagged} screenshot(s) flagged but kept.`
  );
}

function renderEvaluation(evaluation) {
  if (!evaluation) return "";
  const landmark = evaluation.landmark.name ? `<p><strong>${escapeHtml(evaluation.landmark.name)}</strong></p>` : "";
  return `<div class="evaluation">
    <p>${escapeHtml(evaluation.general_description)}</p>
    ${landmark}
    <p class="tags">${tags(withOther(evaluation.scene_setting))}<span class="tag">keep: ${escapeHtml(evaluation.memory.keep_signal)}</span></p>
  </div>`;
}

function withOther(selection) {
  return selection.types.map((type) => (type === "other" && selection.other_detail ? `other: ${selection.other_detail}` : type));
}

function tags(values) {
  if (!values || !values.length) return `<span class="empty">none</span>`;
  return values.map((value) => `<span class="tag">${escapeHtml(value)}</span>`).join("");
}

function note(value) {
  return value ? `<span class="note">${escapeHtml(value)}</span>` : "";
}

function landmarkValue(landmark) {
  if (!landmark.name) return `<span class="empty">none identified</span>`;
  return `<span class="tag strong">${escapeHtml(landmark.name)}</span><span class="tag">${escapeHtml(landmark.confidence_tier)} confidence</span>${note(landmark.evidence)}`;
}

function environmentValue(environment) {
  return `${tags(withOther(environment))}${note(environment.specific_style)}`;
}

function screenshotValue(screenshot) {
  if (!screenshot.is_screenshot_or_document) return `<span class="empty">no</span>`;
  const kind = screenshot.document_kind ? `, ${screenshot.document_kind}` : "";
  return `<span class="tag">${escapeHtml(screenshot.travel_relevance)}</span>${note(`screenshot or document${kind}`)}`;
}

function renderEvaluations(runId, state) {
  document.getElementById("evaluations").innerHTML = state.llm.records.map((record) => reviewCard(runId, state, record)).join("");
  show("evaluations-panel", state.llm.records.length > 0);
}

function reviewCard(runId, state, record) {
  const key = state.thumbnails[record.relative_path];
  const image = key ? `<img src="/api/runs/${runId}/thumbnails/${key}" alt="" loading="lazy">` : `<img alt="" loading="lazy">`;
  const head = `<div class="review-head"><span class="mono">${escapeHtml(record.relative_path)}</span>
    <span class="badge ${escapeHtml(record.validation_status)}">${escapeHtml(record.validation_status)}</span></div>`;
  if (!record.evaluation) {
    return `<div class="review">${image}<div class="review-body">${head}
      <p class="muted">${escapeHtml(record.failure_detail || "The model returned nothing that matched the schema for this image.")}</p></div></div>`;
  }

  const rows = EXTRACTION_FIELDS.map((field) =>
    `<div class="field"><div class="field-label">${escapeHtml(field.label)}</div><div class="field-value">${field.value(record.evaluation)}</div></div>`
  ).join("");
  return `<div class="review">${image}<div class="review-body">${head}<div class="fields">${rows}</div></div></div>`;
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
