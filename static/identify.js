// Identify view (identify.html): upload/capture a photo, POST it to
// /api/identify (see src/api.py + src/identification.py), show up to 5
// candidate species ranked by confidence. This page only exists at all when
// the backend has ENABLE_IDENTIFY on (see api.py's /identify route and
// applyFeatureFlags() in lang.js, which un-hides the nav link for it) --
// CLAUDE.md has the full "IDENTIFY feature isolation" design decision.
//
// Mirrors the server's own limits for fast client-side feedback (no round
// trip needed to reject an obviously-bad file), but the server re-checks
// both independently -- these are a UX nicety, not the real validation.
const MAX_FILE_SIZE_BYTES = 8 * 1024 * 1024;
const ALLOWED_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

const state = {
  lang: "en",
  file: null,
};

async function init() {
  try {
    await loadTranslations();
  } catch (err) {
    document.getElementById("identify-status").hidden = false;
    document.getElementById("identify-status").textContent = "Could not load this page.";
    return;
  }

  state.lang = initLangSwitch((lang) => {
    state.lang = lang;
    document.documentElement.lang = lang;
    document.title = t("page.identify_title", lang);
    applyStaticTranslations(lang);
    renderFooter(lang);
  });
  document.documentElement.lang = state.lang;
  document.title = t("page.identify_title", state.lang);
  applyStaticTranslations(state.lang);
  renderFooter(state.lang);
  applyFeatureFlags();

  document.getElementById("identify-camera-input").addEventListener("change", onFileInputChange);
  document.getElementById("identify-file-input").addEventListener("change", onFileInputChange);
  document.getElementById("identify-submit-btn").addEventListener("click", onSubmitClick);
}

function onFileInputChange(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;

  const statusEl = document.getElementById("identify-status");
  statusEl.hidden = true;

  if (!ALLOWED_TYPES.has(file.type)) {
    statusEl.hidden = false;
    statusEl.textContent = t("identify.error_unsupported_type", state.lang);
    return;
  }
  if (file.size > MAX_FILE_SIZE_BYTES) {
    statusEl.hidden = false;
    statusEl.textContent = t("identify.error_too_large", state.lang);
    return;
  }

  state.file = file;
  hideResults();

  const preview = document.getElementById("identify-preview");
  preview.src = URL.createObjectURL(file);
  document.getElementById("identify-preview-wrap").hidden = false;
}

async function onSubmitClick() {
  if (!state.file) return;

  hideResults();
  const statusEl = document.getElementById("identify-status");
  statusEl.hidden = false;
  statusEl.textContent = t("identify.identifying", state.lang);
  document.getElementById("identify-submit-btn").disabled = true;

  const formData = new FormData();
  formData.append("file", state.file);

  try {
    const response = await fetch("/api/identify", { method: "POST", body: formData });
    if (!response.ok) {
      statusEl.textContent = errorMessageFor(response.status);
      return;
    }
    const result = await response.json();
    statusEl.hidden = true;
    renderResult(result);
  } catch (err) {
    statusEl.textContent = t("identify.error_generic", state.lang);
  } finally {
    document.getElementById("identify-submit-btn").disabled = false;
  }
}

function errorMessageFor(status) {
  if (status === 413) return t("identify.error_too_large", state.lang);
  if (status === 415) return t("identify.error_unsupported_type", state.lang);
  if (status === 422) return t("identify.error_undecodable", state.lang);
  if (status === 429) return t("identify.error_rate_limited", state.lang);
  if (status === 503) return t("identify.error_unavailable", state.lang);
  return t("identify.error_generic", state.lang);
}

function hideResults() {
  document.getElementById("identify-results").hidden = true;
  document.getElementById("identify-low-confidence").hidden = true;
}

function commonNameFor(sp, lang) {
  return pickLabel({ pt: sp.common_name_pt, es: sp.common_name_es, en: sp.common_name_en }, lang);
}

// The primary, "here's a real answer" view -- only shown when the top
// candidate clears identification.CONFIDENCE_THRESHOLD server-side (see
// result.confident). Never rendered for a low-confidence result: showing a
// confidence bar next to a candidate list implies "pick one of these", which
// isn't honest when the model itself isn't sure -- see renderLowConfidence.
function renderResult(result) {
  if (result.confident) {
    renderConfidentList(result.candidates);
    document.getElementById("identify-results").hidden = false;
  } else {
    renderLowConfidenceList(result.candidates);
    document.getElementById("identify-low-confidence").hidden = false;
  }
}

function renderConfidentList(candidates) {
  const listEl = document.getElementById("identify-results-list");
  listEl.innerHTML = "";
  for (const candidate of candidates) {
    listEl.appendChild(renderCandidateCard(candidate));
  }
}

function renderCandidateCard(candidate) {
  const li = document.createElement("li");
  li.className = "identify-candidate";

  const a = document.createElement("a");
  a.className = "identify-candidate-link";
  a.href = `species.html?id=${candidate.id}`;

  a.appendChild(buildPhotoThumb(candidate, "card-thumb identify-candidate-thumb"));

  const info = document.createElement("span");
  info.className = "identify-candidate-info";

  const name = document.createElement("span");
  name.className = "card-name";
  name.textContent = candidate.gbif_name;
  info.appendChild(name);

  const common = commonNameFor(candidate, state.lang);
  if (common) {
    const commonEl = document.createElement("span");
    commonEl.className = "card-common";
    commonEl.textContent = common;
    info.appendChild(commonEl);
  }

  const confidenceWrap = document.createElement("span");
  confidenceWrap.className = "identify-confidence";
  const pct = Math.round(candidate.score * 100);

  const bar = document.createElement("span");
  bar.className = "identify-confidence-bar";
  const fill = document.createElement("span");
  fill.className = "identify-confidence-fill";
  fill.style.width = `${pct}%`;
  bar.appendChild(fill);
  confidenceWrap.appendChild(bar);

  const pctLabel = document.createElement("span");
  pctLabel.className = "identify-confidence-label";
  pctLabel.textContent = `${pct}%`;
  confidenceWrap.appendChild(pctLabel);
  confidenceWrap.setAttribute("aria-label", t("identify.confidence_aria", state.lang, { pct }));

  info.appendChild(confidenceWrap);

  const credit = buildPhotoCredit(candidate, state.lang);
  if (credit) info.appendChild(credit);

  a.appendChild(info);
  li.appendChild(a);
  return li;
}

// Deliberately plain: no thumbnails, no confidence bars, no per-row link
// styling that would read as "one of these is the answer" -- see
// CLAUDE.md/the task this shipped from: a low-confidence result must not be
// presented as if it were a confident one, just made available for the
// curious.
function renderLowConfidenceList(candidates) {
  const listEl = document.getElementById("identify-low-confidence-list");
  listEl.innerHTML = "";
  for (const candidate of candidates) {
    const li = document.createElement("li");
    const common = commonNameFor(candidate, state.lang);
    const pct = Math.round(candidate.score * 100);
    li.textContent = common
      ? `${candidate.gbif_name} (${common}) — ${pct}%`
      : `${candidate.gbif_name} — ${pct}%`;
    listEl.appendChild(li);
  }
}

init();
