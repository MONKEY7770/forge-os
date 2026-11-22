// Forge OS: live preview for Settings, Appearance, Theme. Shows light or dark windows, the accent color and
// the chosen font on the mock desktop. Styles are only set through element.style; no HTML is built from data.
(function () {
  "use strict";
  const LIGHT = {win: "rgba(239, 240, 241, .96)", bar: "#dee0e2", line: "rgba(0, 0, 0, .12)", text: "#232629"};
  const DARK = {win: "rgba(41, 44, 48, .96)", bar: "#202326", line: "rgba(255, 255, 255, .14)", text: "#fcfcfc"};

  function child(parent, tag, cls, first) {
    let n = parent.querySelector(":scope > ." + cls);
    if (!n) {
      n = document.createElement(tag);
      n.className = cls;
      n.setAttribute("aria-hidden", "true");
      if (first) parent.insertBefore(n, parent.firstChild); else parent.appendChild(n);
    }
    return n;
  }

  function palette(v) {
    if (v.scheme_mode === "dark") return DARK;
    if (v.scheme_mode === "light") return LIGHT;
    if (v.scheme_mode === "custom" && v.scheme) return /dark|night|black|vgui/i.test(v.scheme) ? DARK : LIGHT;
    return null;                        // "Leave as it is": the mock keeps its own look
  }

  forgeAppearance.registerPreview("theme", (el, ctx) => {
    const v = ctx.values, win = ctx.window;
    if (!win) return;
    const pal = palette(v);
    const title = child(win, "div", "fa-theme-title", true);
    const sel = child(win, "div", "fa-theme-sel");
    const sample = child(win, "div", "fa-theme-sample");
    sample.textContent = "Aa กข 123";
    title.style.position = "absolute";    // sits on the window's own top strip
    title.style.left = title.style.right = title.style.top = "0";
    title.style.height = "11%";
    title.style.background = pal ? pal.bar : "";
    title.style.borderBottom = pal ? "1px solid " + pal.line : "";
    win.style.background = pal ? pal.win : "";
    win.querySelectorAll(":scope > i").forEach(i => { i.style.background = pal ? pal.line : ""; });

    const accent = v.accent === "custom" ? v.accent_color : "";
    sel.style.height = "9%";
    sel.style.margin = "7% 10% 0";
    sel.style.width = "45%";
    sel.style.borderRadius = "3px";
    sel.style.background = accent || "var(--accent)";
    const dot = ctx.dock ? ctx.dock.querySelector("i") : null;
    if (dot) dot.style.background = accent || "";

    const size = /^\d+$/.test(v.font_size) ? Number(v.font_size) : 10;
    sample.style.margin = "6% 10% 0";
    sample.style.fontFamily = v.font ? JSON.stringify(v.font) + ", system-ui, sans-serif" : "";
    sample.style.fontSize = Math.round(size * 1.1) + "px";
    sample.style.lineHeight = "1.3";
    sample.style.whiteSpace = "nowrap";
    sample.style.overflow = "hidden";
    sample.style.color = pal ? pal.text : "#232629";
  });
})();
