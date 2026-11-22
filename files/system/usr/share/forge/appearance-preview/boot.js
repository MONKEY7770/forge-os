// Forge OS: preview for Appearance, Bootup. The boot splash as it will look, with a mock of the
// disk password box, and a clear note about the admin password and the restart.
// No eval, no HTML strings, no outside resources: elements are made with the DOM and styled through CSSOM.
forgeAppearance.registerPreview("boot", (el, ctx) => {
  const v = ctx.values;
  const boot = ctx.boot;
  const make = (tag, text) => { const e = document.createElement(tag); if (text) e.textContent = text; return e; };

  // Our own layer inside the boot mock, made once and updated on every change.
  let layer = boot.querySelector(".fb-layer");
  if (!layer) {
    layer = make("div");
    layer.className = "fb-layer";
    Object.assign(layer.style, {position: "absolute", inset: "0", display: "flex", flexDirection: "column",
                                alignItems: "center", justifyContent: "center", gap: "4%"});
    boot.append(layer);
  }
  boot.querySelectorAll(":scope > b, :scope > .fa-progress").forEach(x => { x.style.display = "none"; });

  const on = v.splash === "on";
  const item = on && v.splash_media ? ctx.media[v.splash_media] : null;
  boot.style.background = on ? v.splash_background : "#000000";

  const key = [on, v.splash_media, v.splash_size, v.splash_logo, v.splash_play, v.splash_length].join("|");
  if (layer.dataset.key !== key) {
    layer.dataset.key = key;
    layer.replaceChildren();
    const shrink = {small: 0.125, medium: 0.1875, large: 0.25}[v.splash_size] || 0.1875;

    if (!on || v.splash_logo) {
      const logo = make("div", on ? "PC maker logo" : "Bazzite");
      Object.assign(logo.style, {fontSize: "11px", letterSpacing: ".4px", color: "rgba(255,255,255,.75)",
                                 border: "1px dashed rgba(255,255,255,.35)", borderRadius: "4px", padding: "3px 8px"});
      layer.append(logo);
    }

    const box = make("div");
    Object.assign(box.style, {width: (shrink * 100 * 1.6) + "%", aspectRatio: "1 / 1", display: "flex",
                              alignItems: "center", justifyContent: "center", overflow: "hidden"});
    if (!on) {
      const spin = make("div");
      Object.assign(spin.style, {width: "22px", height: "22px", borderRadius: "50%",
                                 border: "3px solid rgba(255,255,255,.25)", borderTopColor: "#ffffff"});
      box.append(spin);
    } else if (!item) {
      const msg = make("div", t("look.prev.choose_media"));
      Object.assign(msg.style, {fontSize: "11px", color: "rgba(255,255,255,.7)", textAlign: "center"});
      box.append(msg);
    } else if (!item.url) {
      const msg = make("div", t("ui.svg_no_preview"));
      Object.assign(msg.style, {fontSize: "11px", color: "rgba(255,255,255,.7)"});
      box.append(msg);
    } else if (item.kind === "video") {
      const vid = make("video");
      vid.muted = true; vid.playsInline = true; vid.preload = "metadata";
      vid.setAttribute("aria-hidden", "true");
      Object.assign(vid.style, {maxWidth: "100%", maxHeight: "100%", objectFit: "contain"});
      const length = Number(v.splash_length) || 2;
      vid.addEventListener("timeupdate", () => {
        if (vid.currentTime < length) return;
        if (v.splash_play === "once") vid.pause(); else vid.currentTime = 0;
      });
      if (!matchMedia("(prefers-reduced-motion: reduce)").matches) vid.autoplay = true;
      vid.src = item.url;
      box.append(vid);
    } else {
      const img = make("img");
      img.alt = "";
      Object.assign(img.style, {maxWidth: "100%", maxHeight: "100%", objectFit: "contain"});
      img.src = item.url;
      box.append(img);
    }
    layer.append(box);

    // The disk password box (Plymouth's two-step plugin draws it from the system's own images).
    const entry = make("div");
    Object.assign(entry.style, {display: "flex", alignItems: "center", gap: "6px", padding: "4px 8px",
                                background: "rgba(255,255,255,.92)", borderRadius: "4px", minWidth: "34%"});
    entry.setAttribute("aria-label", t("look.prev.disk_box"));
    const lock = make("span");
    Object.assign(lock.style, {width: "9px", height: "10px", borderRadius: "2px", background: "#3a3a3a",
                               flex: "none"});
    entry.append(lock);
    for (let i = 0; i < 6; i++) {
      const dot = make("span");
      Object.assign(dot.style, {width: "5px", height: "5px", borderRadius: "50%", background: "#202020"});
      entry.append(dot);
    }
    layer.append(entry);

    const tag = make("div", on ? t("look.prev.after_restart") : t("look.prev.bazzite_splash"));
    Object.assign(tag.style, {position: "absolute", left: "8px", top: "8px", fontSize: "10.5px", color: "#fff",
                              background: "rgba(0,0,0,.55)", borderRadius: "4px", padding: "1px 6px"});
    layer.append(tag);
  }

  // Say clearly, next to the buttons, what asks for a password and what needs a restart.
  const look = el.closest(".look");
  const note = look && look.querySelector(".look-foot .note");
  if (note) {
    note.textContent = t("look.prev.boot_note");
  }
  const cap = el.parentElement && el.parentElement.querySelector(".cap");
  if (cap) cap.textContent = t("look.prev.boot_cap");
});
