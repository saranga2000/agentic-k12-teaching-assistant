/* Ledger repaint (docs/ROADMAP.md's M9), 2026-09-01. Loaded as an ordinary
   blocking <script src> at the very top of <head>, before tokens.css -- so a
   previously-chosen light/dark override (see _theme_toggle.html) is applied
   to <html> before the stylesheet is even parsed, and there is no flash of
   the wrong theme on a repeat visit. Shared as one physical file between both
   apps the same way tokens.css itself is, since both mount /static at
   src/k12ta/design/. */
(function () {
  try {
    var stored = localStorage.getItem("k12ta-theme");
    if (stored === "light" || stored === "dark") {
      document.documentElement.setAttribute("data-theme", stored);
    }
  } catch (e) {
    /* Private browsing / storage disabled: fall back to prefers-color-scheme,
       same as a first-ever visit. */
  }
})();
