// Forge OS dock: built from a fixed template; the only data is the JSON literal on the next line.
var forge = {"saved": [{"id": 1, "props": {"alignment": "center", "floating": true, "floatingApplets": false, "height": 44, "hiding": "none", "lengthMode": "fill", "location": "bottom", "maximumLength": 1920, "minimumLength": 1920, "offset": 0, "opacity": "adaptive"}}, {"id": 7, "props": {"alignment": "left", "floating": false, "floatingApplets": true, "height": 28, "hiding": "dodgewindows", "lengthMode": "custom", "location": "top", "maximumLength": 800, "minimumLength": 600, "offset": 12, "opacity": "opaque"}}], "style": {"alignment": "center", "floating": true, "height": 56, "hiding": "none", "lengthMode": "fit", "location": "bottom", "opacity": "translucent"}};
var ORDER = ["location", "height", "lengthMode", "maximumLength", "minimumLength", "offset", "alignment",
             "hiding", "opacity", "floating", "floatingApplets"];
var TASKS = ["org.kde.plasma.taskmanager", "org.kde.plasma.icontasks", "org.kde.plasma.expandingiconstaskmanager"];
var all = panels();
function hasTasks(p) {
  var w = p.widgets();
  for (var i = 0; i < w.length; i++) {
    if (TASKS.indexOf(w[i].type) >= 0) return true;
  }
  return false;
}
function put(p, props) {
  for (var i = 0; i < ORDER.length; i++) {
    if (Object.prototype.hasOwnProperty.call(props, ORDER[i])) p[ORDER[i]] = props[ORDER[i]];
  }
}
var targets = [];
if (forge.style !== null) {
  for (var i = 0; i < all.length; i++) {
    if (hasTasks(all[i])) targets.push(all[i].id);
  }
  if (targets.length === 0 && all.length > 0) targets.push(all[0].id);
}
var restored = 0;
for (var j = 0; j < all.length; j++) {
  var p = all[j], props = {}, k;
  // Default (style null) puts every saved panel back. A style touches only the panels it styles.
  if (forge.style !== null && targets.indexOf(p.id) < 0) continue;
  for (k = 0; k < forge.saved.length; k++) {
    if (forge.saved[k].id === p.id) {
      props = JSON.parse(JSON.stringify(forge.saved[k].props));
      restored++;
    }
  }
  if (forge.style !== null) {
    for (k in forge.style) props[k] = forge.style[k];
  }
  put(p, props);
}
print("FORGE-BEGIN" + JSON.stringify({restored: restored, changed: targets.length}) + "FORGE-END");
