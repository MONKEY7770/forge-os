// Forge OS: live preview for Settings, Appearance, Dock. Draws the chosen dock style on the mock desktop:
// the stock Plasma panel (Default), the floating panel (Floating clean) or Krema with hover zoom (Animated).
// Styles are only set through element.style; nothing here builds HTML from data.
(function () {
  "use strict";
  const SCREEN = 1280;                  // the mock desktop stands for a screen this many pixels wide
  const PANEL = "30, 32, 36";            // Breeze Dark panel color (Bazzite's default look)
  const live = new WeakMap();            // dock element -> the values its hover zoom uses now
  const RESET = ["left", "right", "top", "bottom", "transform", "width", "height", "flexDirection", "justifyContent",
                 "alignItems", "gap", "padding", "borderRadius", "background", "backdropFilter", "boxShadow",
                 "opacity", "transition", "maxWidth", "maxHeight"];
  const SPEED = {natural: [180, 240], quick: [100, 100], relaxed: [300, 400], instant: [0, 0]};

  const px = n => Math.round(n * 100) / 100 + "px";
  const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, Number(n) || 0));

  function child(parent, tag, cls) {
    let n = parent.querySelector("." + cls);
    if (!n) {
      n = document.createElement(tag);
      n.className = cls;
      parent.appendChild(n);
    }
    return n;
  }

  function place(node, pos, gap, align, fill) {
    // Puts a bar on an edge: pos is the edge, gap the space to it, align where it sits along it.
    const vertical = pos === "left" || pos === "right";
    node.style[pos] = px(gap);
    node.style[pos === "bottom" ? "top" : pos === "top" ? "bottom" : pos === "left" ? "right" : "left"] = "auto";
    const a = vertical ? ["top", "bottom"] : ["left", "right"];
    if (fill) {
      node.style[a[0]] = px(gap);
      node.style[a[1]] = px(gap);
      node.style.transform = "none";
    } else if (align === "left") {
      node.style[a[0]] = px(Math.max(gap, 6));
      node.style[a[1]] = "auto";
      node.style.transform = "none";
    } else if (align === "right") {
      node.style[a[1]] = px(Math.max(gap, 6));
      node.style[a[0]] = "auto";
      node.style.transform = "none";
    } else {
      node.style[a[0]] = "50%";
      node.style[a[1]] = "auto";
      node.style.transform = vertical ? "translateY(-50%)" : "translateX(-50%)";
    }
    node.style.flexDirection = vertical ? "column" : "row";
  }

  function zoomAt(dock, ev) {
    const st = live.get(dock);
    const icons = Array.from(dock.querySelectorAll("i"));
    if (!st || !st.zoom) {
      icons.forEach(i => { i.style.transform = ""; i.style.margin = ""; });
      return;
    }
    const at = st.vertical ? ev.clientY : ev.clientX;
    icons.forEach(i => {
      const r = i.getBoundingClientRect();
      const center = st.vertical ? r.top + r.height / 2 : r.left + r.width / 2;
      const d = Math.abs(at - center);
      const reach = st.inPlace ? st.icon * 0.75 : st.icon * 2.5;
      const k = Math.max(0, 1 - (d / reach) * (d / reach));
      const scale = 1 + (st.factor - 1) * k;
      i.style.transition = "transform " + st.inMs + "ms ease-out, margin " + st.inMs + "ms ease-out";
      i.style.transformOrigin = st.origin;
      i.style.transform = "scale(" + Math.round(scale * 1000) / 1000 + ")";
      // Wave zoom makes room for the bigger icons; in place lets them grow over their neighbors.
      const room = st.inPlace ? 0 : (scale - 1) * st.icon / 2;
      i.style.margin = st.vertical ? px(room) + " 0" : "0 " + px(room);
    });
  }

  function zoomOut(dock) {
    const st = live.get(dock);
    Array.from(dock.querySelectorAll("i")).forEach(i => {
      i.style.transition = "transform " + (st ? st.outMs : 0) + "ms ease-in, margin " + (st ? st.outMs : 0) + "ms ease-in";
      i.style.transform = "";
      i.style.margin = "";
    });
  }

  forgeAppearance.registerPreview("dock", (el, ctx) => {
    const v = ctx.values, dock = ctx.dock;
    if (!dock) return;
    const s = (el.clientWidth || 400) / SCREEN;
    const style = v.style === "floating" || v.style === "animated" ? v.style : "default";
    const pos = style === "default" ? "bottom" : (["bottom", "top", "left", "right"].includes(v.position) ? v.position : "bottom");
    const vertical = pos === "left" || pos === "right";
    const icons = Array.from(dock.querySelectorAll("i"));
    const tray = child(dock, "span", "fa-dock-tray");
    const bar = child(el, "div", "fa-dock-bar");
    RESET.forEach(k => { dock.style[k] = ""; });
    dock.style.display = "flex";
    dock.style.alignItems = "center";
    dock.style.justifyContent = "center";
    // The other Plasma panel (Animated with a slim bar or the panel left as it is). Hidden otherwise.
    bar.style.position = "absolute";
    bar.style.display = "none";
    bar.style.background = "rgba(" + PANEL + ", .92)";
    bar.style.left = bar.style.right = "0px";
    bar.style.top = bar.style.bottom = "auto";
    tray.style.display = "block";
    tray.style.marginLeft = "auto";
    tray.style.width = px(110 * s);
    tray.style.height = px(14 * s);
    tray.style.borderRadius = px(4 * s);
    tray.style.background = "rgba(255, 255, 255, .28)";
    tray.style.flex = "none";

    let size, gapEdge, reduced = false;
    if (style === "default") {
      // Stock Bazzite panel: full width along the bottom, app list from the left, tray and clock on the right.
      size = 30;
      place(dock, "bottom", 0, "center", true);
      dock.style.height = px(44 * s);
      dock.style.justifyContent = "flex-start";
      dock.style.padding = "0 " + px(10 * s);
      dock.style.gap = px(8 * s);
      dock.style.borderRadius = "0px";
      dock.style.background = "rgba(" + PANEL + ", .94)";
      dock.style.boxShadow = "none";
      dock.style.backdropFilter = "none";
    } else if (style === "floating") {
      size = clamp(v.icon_size, 24, 96);
      gapEdge = 8 * s;                    // Plasma's floating gap comes from the theme
      const fill = v.panel_width === "fill";
      place(dock, pos, gapEdge, v.alignment, fill);
      dock.style[vertical ? "width" : "height"] = px((size + 8) * s);
      dock.style.padding = vertical ? px(8 * s) + " 0" : "0 " + px(8 * s);
      dock.style.gap = px(6 * s);
      dock.style.justifyContent = fill ? "flex-start" : "center";
      dock.style.borderRadius = px(12 * s + 4);
      const alpha = v.panel_look === "opaque" ? 1 : v.panel_look === "adaptive" ? 0.8 : 0.6;
      dock.style.background = "rgba(" + PANEL + ", " + alpha + ")";
      dock.style.backdropFilter = alpha < 1 ? "blur(8px)" : "none";
      dock.style.boxShadow = "0 2px 10px rgba(0, 0, 0, .3)";
      // The tray and clock: at the far end on a full-width bar, right after the icons otherwise.
      tray.style.marginLeft = !vertical && fill ? "auto" : "0";
      tray.style.marginTop = vertical && fill ? "auto" : "0";
      if (vertical) {
        tray.style.width = px(14 * s);
        tray.style.height = px(70 * s);
      }
      reduced = v.visibility !== "always";
    } else {
      size = clamp(v.icon_size, 24, 96);
      gapEdge = clamp(v.gap, 0, 128) * s;
      tray.style.display = "none";
      // Where the Plasma panel goes while Krema runs.
      const sp = v.stock_panel;
      if (sp === "top_bar" || sp === "keep") {
        const edge = sp === "keep" ? "bottom" : pos === "top" ? "bottom" : "top";
        bar.style.display = "block";
        bar.style[edge] = "0px";
        bar.style.height = px((sp === "keep" ? 44 : 30) * s);
        if (sp === "keep" && pos === "bottom") gapEdge += 44 * s;   // Krema sits above a panel on its edge
      }
      place(dock, pos, gapEdge, "center", false);
      dock.style.padding = px(6 * s + 2);
      dock.style.gap = px(clamp(v.spacing, 0, 64) * s);
      dock.style.borderRadius = px(clamp(v.radius, 0, 24) * s * 1.6);
      const a = clamp(v.opacity, 0, 100) / 100;
      const pct = Math.round(a * 100) + "%";
      const base = v.color_source === "accent" ? "var(--accent)" : "rgb(" + PANEL + ")";
      const tint = v.color_source === "custom" && /^#[0-9a-f]{6}$/i.test(v.tint) ? v.tint : base;
      const color = v.background === "tinted" ? tint : base;
      dock.style.background = v.background === "transparent" ? "transparent"
        : "color-mix(in srgb, " + color + " " + pct + ", transparent)";
      dock.style.backdropFilter = v.background === "theme" ? "blur(8px) contrast(1.1)"
        : v.background === "acrylic" ? "blur(14px) saturate(1.4)" : "none";
      dock.style.boxShadow = v.shadow ? "0 " + px(8 * s + 2) + " " + px(24 * s + 6) + " rgba(0, 0, 0, "
        + (clamp(v.shadow_strength, 0, 100) / 100 + 0.1).toFixed(2) + ")" : "none";
      reduced = v.visibility === "autohide";
    }
    if (reduced) dock.style.opacity = ".55";   // hidden until needed: shown faded in the preview

    const icon = Math.max(6, size * s);
    icons.forEach(i => {
      i.style.width = px(icon);
      i.style.height = px(icon);
      i.style.borderRadius = px(Math.max(3, icon / 4));
      i.style.flex = "none";
      i.style.transform = "";
      i.style.margin = "";
    });

    const speed = v.zoom_speed === "custom" ? [clamp(v.zoom_in_ms, 0, 1000), clamp(v.zoom_out_ms, 0, 1000)]
      : SPEED[v.zoom_speed] || SPEED.natural;
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    live.set(dock, {
      zoom: style === "animated" && clamp(v.zoom, 100, 200) > 100, factor: clamp(v.zoom, 100, 200) / 100,
      inPlace: v.zoom_style === "in_place", vertical, icon,
      inMs: still ? 0 : speed[0], outMs: still ? 0 : speed[1],
      origin: pos === "bottom" ? "50% 100%" : pos === "top" ? "50% 0%" : pos === "left" ? "0% 50%" : "100% 50%",
    });
    if (!dock.dataset.forgeZoom) {
      dock.dataset.forgeZoom = "1";
      dock.addEventListener("mousemove", ev => zoomAt(dock, ev));
      dock.addEventListener("mouseleave", () => zoomOut(dock));
    }
  });
})();
