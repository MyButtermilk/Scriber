(function () {
  var key = "scriber-ui-locale";
  var stored = null;
  try {
    stored = window.localStorage.getItem(key);
  } catch (_error) {
    stored = null;
  }
  var browserLanguages = navigator.languages && navigator.languages.length
    ? navigator.languages
    : [navigator.language || ""];
  var locale = stored === "de" || stored === "en" ? stored : "en";
  if (stored !== "de" && stored !== "en") {
    for (var index = 0; index < browserLanguages.length; index += 1) {
      var language = String(browserLanguages[index]).toLowerCase();
      if (language.indexOf("de") === 0) {
        locale = "de";
        break;
      }
      if (language.indexOf("en") === 0) {
        locale = "en";
        break;
      }
    }
  }
  document.documentElement.lang = locale;
  var bootShell = document.querySelector(".boot-shell");
  if (bootShell) {
    bootShell.setAttribute("aria-label", locale === "de" ? "Scriber wird gestartet" : "Scriber is starting");
  }
})();
