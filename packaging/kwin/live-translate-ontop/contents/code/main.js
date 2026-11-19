// Keep the Live Translate overlay above normal windows on every virtual
// desktop.  KWin on Wayland ignores Qt's WindowStaysOnTopHint, so the setting
// has to come from the compositor side.
//
// The window is matched by its xdg-toplevel app_id, which Qt fills from
// QGuiApplication.setDesktopFileName("live-translate").

function isLiveTranslate(window) {
    var cls = String(window.resourceClass || "").toLowerCase();
    var name = String(window.resourceName || "").toLowerCase();
    return cls.indexOf("live-translate") !== -1 || name.indexOf("live-translate") !== -1;
}

function applyAlwaysOnTop(window) {
    if (!window || !isLiveTranslate(window)) {
        return;
    }
    window.keepAbove = true;
    window.onAllDesktops = true;
}

var existing = workspace.windowList();
for (var i = 0; i < existing.length; i++) {
    applyAlwaysOnTop(existing[i]);
}

workspace.windowAdded.connect(applyAlwaysOnTop);
