// Shared language-switch logic used by every page (map.html, atlas.html, species.html).
// Plain <script> include, no bundler -- must load before any page script that
// calls these functions.

const LANGS = ["pt", "es", "en"];
const LANG_STORAGE_KEY = "nidatlas-lang";

function detectDefaultLang() {
  let saved = null;
  try {
    saved = localStorage.getItem(LANG_STORAGE_KEY);
  } catch (err) {
    // localStorage unavailable (private browsing, disabled storage, etc.)
  }
  if (saved && LANGS.includes(saved)) return saved;

  const browserLang = (navigator.language || "en").slice(0, 2).toLowerCase();
  return LANGS.includes(browserLang) ? browserLang : "en";
}

function persistLang(lang) {
  try {
    localStorage.setItem(LANG_STORAGE_KEY, lang);
  } catch (err) {
    // ignore -- language just won't persist this session
  }
}

// chosen language -> English -> null (caller falls back to scientific name only)
function pickLabel(names, lang) {
  return names[lang] || names.en || null;
}

// --- UI translations (static/i18n.json) ---
//
// Single source of truth for every hardcoded UI string across every page:
// {"key.path": {"en": "...", "pt": "...", "es": "..."}, ...}. Loaded once
// and cached module-level (not per-page state) since every page loads
// lang.js the same way and none of them needs its own copy.
let I18N = null;

async function loadTranslations() {
  if (I18N) return I18N;
  const resp = await fetch("/i18n.json");
  I18N = await resp.json();
  return I18N;
}

// t(key, lang, vars): resolves a translation key for the given language
// (falling back to English, then to the raw key itself if the key is
// missing entirely -- a visible "some.missing.key" in the UI is a much
// easier bug to spot and fix than silently rendering nothing) and
// substitutes any {token} placeholders from vars.
function t(key, lang, vars) {
  const entry = I18N && I18N[key];
  let text = entry ? pickLabel(entry, lang) : key;
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.replace(`{${name}}`, value);
    }
  }
  return text;
}

// Simple singular/other pluralization: en/pt/es all just need two forms
// here (no dual, no complex Slavic-style count classes), selected by
// whether the relevant count is exactly 1. Callers pass a base key and get
// "<base>_one" or "<base>_other" resolved.
function tPlural(baseKey, count, lang, vars) {
  return t(`${baseKey}_${count === 1 ? "one" : "other"}`, lang, vars);
}

// Applies every declarative data-i18n(-*) binding on the current page --
// covers all the static (not JS-rendered) text on every page, so most
// markup never needs a matching line of JS to translate it.
function applyStaticTranslations(lang) {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n, lang);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder, lang);
  });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((el) => {
    el.setAttribute("aria-label", t(el.dataset.i18nAriaLabel, lang));
  });
}

// Footer: identical on both pages, but its two lines mix translated prose
// with fixed links (proper nouns/URLs, language-invariant) -- variable
// substitution the declarative data-i18n system above doesn't attempt, so
// it's rendered directly instead of via a data-i18n attribute. The snapshot
// date (itself translated -- "28 August 2026" vs "28 de agosto de 2026")
// matches README.md's occurrence-cube citation and
// scripts/prepare_cube.py's MIN_YEAR-driven CC0/CC-BY-only filtering.
function renderFooter(lang) {
  const dataLine = document.getElementById("footer-data-line");
  if (dataLine) {
    dataLine.innerHTML =
      t("footer.data_line", lang, { date: t("footer.snapshot_date", lang) }) +
      ` <a href="https://doi.org/10.15468/dl.kb6cwg">https://doi.org/10.15468/dl.kb6cwg</a>`;
  }

  const attributionLine = document.getElementById("footer-attribution-line");
  if (attributionLine) {
    attributionLine.innerHTML = t("footer.attribution_line", lang, {
      osm: `<a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>`,
      odbl: `<a href="https://opendatacommons.org/licenses/odbl/">ODbL</a>`,
      bioclip: `<a href="https://github.com/Imageomics/bioclip-2">BioCLIP 2</a>`,
      imageomics: "Imageomics Institute",
    });
  }
}

// Wires up every .lang-btn on the page, restores the saved/detected language,
// and calls onChange(lang) whenever the user picks a different one. Returns
// the initial language so the caller can use it before onChange ever fires.
function initLangSwitch(onChange) {
  const lang = detectDefaultLang();
  const buttons = document.querySelectorAll(".lang-btn");

  buttons.forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.lang === lang);
    btn.addEventListener("click", () => {
      persistLang(btn.dataset.lang);
      buttons.forEach((b) => b.classList.toggle("active", b === btn));
      onChange(btn.dataset.lang);
    });
  });

  return lang;
}
