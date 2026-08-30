// Shared language-switch logic used by every page (index.html, species.html, ...).
// Plain <script> include, no bundler -- must load before any page script that
// calls these functions.

const LANGS = ["pt", "es", "en"];
const LANG_STORAGE_KEY = "nidario-lang";

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
