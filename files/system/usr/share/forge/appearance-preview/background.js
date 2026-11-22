// Forge OS: preview of the Background section (Settings, Appearance).
// The page already puts the chosen picture or video (or the color) in the mock's wallpaper. This adds what
// the desktop would do with it: how it fills the screen, blur, a gradient, a slideshow picture, and a short
// label for what the preview can't play (Wallpaper Engine). Styles only through element.style, text only
// through textContent. The wallpaper of the Dock and Bootup previews is painted the same way.
(function () {
  "use strict";
  const MARK = "forgeBg";
  const SCALE = 0.22;      // the preview is about a fifth of a real screen: blur less
  const FIT = {crop: "cover", fit: "contain", fit_blur: "contain", stretch: "fill", center: "none",
               center_blur: "none", tile: "cover"};
  const VIDEO_FIT = {crop: "cover", fit: "contain", stretch: "fill"};
  const DIRECTION = {down: "to bottom", right: "to right", diagonal: "to bottom right", diagonal_up: "to top right"};
  const HEX = /^#[0-9a-f]{6}$/;

  function clearOwn(box) {
    box.querySelectorAll("[data-forge-bg]").forEach(n => n.remove());
  }

  function own(tag) {
    const n = document.createElement(tag);
    n.dataset[MARK] = "1";
    return n;
  }

  function badge(el, text) {
    const b = own("div");
    b.textContent = text;
    const s = b.style;
    s.position = "absolute"; s.left = "3%"; s.top = "4%"; s.zIndex = "2"; s.maxWidth = "70%";
    s.padding = "2px 7px"; s.borderRadius = "5px"; s.font = "600 11px/1.5 system-ui, sans-serif";
    s.color = "#fff"; s.background = "rgba(0, 0, 0, .55)";
    s.whiteSpace = "nowrap"; s.overflow = "hidden"; s.textOverflow = "ellipsis";
    el.append(b);
  }

  function blurLayer(wall, url) {
    // The blurred copy behind a picture that does not fill the screen ("blurred edges").
    const d = own("div");
    const s = d.style;
    s.position = "absolute"; s.inset = "-12%"; s.zIndex = "0";
    s.backgroundImage = "url(\"" + url + "\")"; s.backgroundSize = "cover"; s.backgroundPosition = "center";
    s.filter = "blur(" + Math.round(64 * SCALE) + "px)";
    wall.prepend(d);
  }

  function picture(wall, img, url, fill) {
    img.style.position = "relative"; img.style.zIndex = "1";
    img.style.objectFit = FIT[fill] || "cover"; img.style.objectPosition = "center";
    img.style.visibility = fill === "tile" ? "hidden" : "";
    if (fill === "tile") {
      wall.style.backgroundImage = "url(\"" + url + "\")";
      wall.style.backgroundRepeat = "repeat";
      wall.style.backgroundSize = "18%";
    } else if (fill === "fit_blur" || fill === "center_blur") {
      blurLayer(wall, url);
    } else if (fill !== "crop" && fill !== "stretch") {
      wall.style.background = "#000";
    }
  }

  function paint(el, ctx, withBadge) {
    const v = (ctx.all && ctx.all.background) || {};
    const wall = ctx.wall;
    if (!wall) return;
    clearOwn(wall);
    clearOwn(el);
    wall.style.backgroundImage = ""; wall.style.backgroundRepeat = "";
    wall.style.backgroundSize = ""; wall.style.overflow = "hidden";
    const img = wall.querySelector("img");
    const video = wall.querySelector("video");
    const item = id => (id && ctx.media[id]) || null;
    const where = v.screen === "one" && v.screen_connector ? " " + t("look.prev.only_on", {name: v.screen_connector}) : "";
    let label = "";

    if (v.type === "picture") {
      const m = item(v.picture);
      if (img && m && m.url) picture(wall, img, m.url, v.fill);
      if (!m) label = t("look.prev.no_picture");
    } else if (v.type === "slideshow") {
      const pics = Object.values(ctx.media).filter(m => m.url && (m.kind === "image" || m.kind === "animated-image"));
      if (pics.length) {
        const first = own("img");
        first.alt = ""; first.src = pics[0].url;
        first.style.width = "100%"; first.style.height = "100%"; first.style.display = "block";
        wall.append(first);
        picture(wall, first, pics[0].url, v.fill);
        label = tn("look.prev.slideshow", pics.length, {min: v.slide_interval});
      } else {
        label = t("look.prev.slideshow_empty");
      }
    } else if (v.type === "video") {
      if (video) {
        video.style.objectFit = VIDEO_FIT[v.video_fill] || "cover";
        if (typeof v.speed === "number" && v.speed > 0) video.playbackRate = v.speed;
        video.muted = true;    // the preview never plays sound
        if (VIDEO_FIT[v.video_fill] !== "cover") wall.style.background = "#000";
      } else {
        label = t("look.prev.no_video");
      }
      if (video) {
        // Blur the video itself, a little enlarged, so its edges don't fade into the page.
        const on = v.video_blur === "always";
        video.style.filter = on ? "blur(" + Math.max(1, Math.round((v.video_blur_radius || 32) * SCALE)) + "px)" : "";
        video.style.transform = on ? "scale(1.08)" : "";
      }
      if (video && v.volume > 0) label = t("look.prev.sound", {n: v.volume});
    } else if (v.type === "gradient") {
      if (HEX.test(v.color) && HEX.test(v.color2)) {
        wall.style.background = "linear-gradient(" + (DIRECTION[v.gradient_direction] || "to bottom") + ", " +
                                v.color + ", " + v.color2 + ")";
      }
    } else if (v.type === "wallpaper_engine") {
      const sel = document.querySelector('[data-look="background"][data-key="we_item"] select');
      const opt = sel && sel.selectedIndex > 0 ? sel.options[sel.selectedIndex].textContent : "";
      label = "Wallpaper Engine: " + (opt || v.we_item || t("ui.none_chosen"));
    }
    if (withBadge && (label || where)) badge(el, (label || "") + where);
  }

  forgeAppearance.registerPreview("background", (el, ctx) => paint(el, ctx, true));
  forgeAppearance.registerPreview("dock", (el, ctx) => paint(el, ctx, false));
  forgeAppearance.registerPreview("boot", (el, ctx) => paint(el, ctx, false));
})();
