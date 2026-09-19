# Sayri Desktop UI plugin

Sayri's main desktop interface — the GTK4/WebKit assistant window, tray icon,
overlay and full conversation UI — exposed as a first-class Sayri plugin.

## Install / launch

```
sayri ui default        # or: sayri ui desktop
sayri ui desktop status
sayri ui desktop stop
```

Installed plugins live in `~/.config/sayri/plugins/` (user copy) with the
system defaults in `/usr/share/sayri/plugins/`. Setting `ui.default_ui` in
`~/.config/sayri/sayri.toml` changes what `sayri ui default` opens.

## How it works

The manifest id is `sayri-desktop` (`ui` section → picked up as a UI plugin).
The real UI never moved: `gateway.py` just re-executes the built-in app via
`python3 -m sayri --launch-ui`, so the desktop window, tray, autostart and
iPad-style search surface all behave exactly as the standalone installation.

Uninstall by removing the plugin dir from `~/.config/sayri/plugins/`.