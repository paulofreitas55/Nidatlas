// Privacy view (privacy.html): entirely static content, translated via
// data-i18n bindings except the contact line, which needs the {email}
// token substituted with a real mailto link -- t()'s {token} substitution
// doesn't escape/build markup, so that one line is rendered directly here
// the same way lang.js's renderFooter() builds its own meta line.

const state = { lang: "en" };

async function init() {
  try {
    await loadTranslations();
  } catch (err) {
    return;
  }

  state.lang = initLangSwitch((lang) => {
    state.lang = lang;
    document.documentElement.lang = lang;
    document.title = t("page.privacy_title", lang);
    applyStaticTranslations(lang);
    renderFooter(lang);
    renderContactBody(lang);
  });
  document.documentElement.lang = state.lang;
  document.title = t("page.privacy_title", state.lang);
  applyStaticTranslations(state.lang);
  renderFooter(state.lang);
  renderContactBody(state.lang);
  applyFeatureFlags();
}

function renderContactBody(lang) {
  const el = document.getElementById("privacy-contact-body");
  if (!el) return;
  const contactEmail = "paulo.afonso.freitas.2003@gmail.com";
  el.innerHTML = t("privacy.contact_body", lang, { email: `<a href="mailto:${contactEmail}">${contactEmail}</a>` });
}

init();
