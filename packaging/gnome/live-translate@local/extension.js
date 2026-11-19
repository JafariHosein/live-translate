// GNOME Shell helper: keep the Live Translate overlay above other windows and
// visible on all workspaces, without depending on a third-party extension.
//
// GNOME's Wayland compositor (Mutter) does not expose window rules.  The
// overlay's WM class / app_id is "live-translate" because main.py calls
// QGuiApplication.setDesktopFileName("live-translate").

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const APP_ID = 'live-translate';

export default class LiveTranslateAlwaysOnTop extends Extension {
    enable() {
        this._applied = new Set();
        this._displaySignal = global.display.connect('window-created', (_display, window) => {
            this._watch(window);
        });
        for (const actor of global.get_window_actors())
            this._watch(actor.meta_window);
    }

    disable() {
        if (this._displaySignal) {
            global.display.disconnect(this._displaySignal);
            this._displaySignal = 0;
        }
        for (const window of this._applied) {
            try {
                window.unmake_above();
                window.unstick();
            } catch (error) {
                // The window may already have been destroyed.
            }
        }
        this._applied.clear();
    }

    _watch(window) {
        if (!window)
            return;

        if (this._matches(window)) {
            this._apply(window);
            return;
        }

        // On Wayland the app_id can arrive slightly after window-created.
        const id = window.connect('notify::wm-class', () => {
            if (this._matches(window))
                this._apply(window);
        });
        window.connect('unmanaged', () => {
            try {
                window.disconnect(id);
            } catch (error) {
                // Already disconnected.
            }
        });
    }

    _matches(window) {
        const wmClass = (window.get_wm_class() || '').toLowerCase();
        return wmClass === APP_ID ||
            wmClass.startsWith(`${APP_ID}.`) ||
            wmClass.startsWith(`${APP_ID}-`);
    }

    _apply(window) {
        if (this._applied.has(window))
            return;
        window.make_above();
        window.stick();
        this._applied.add(window);
    }
}
