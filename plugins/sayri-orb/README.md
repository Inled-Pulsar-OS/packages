# sayri-orb — Default GTK4 Orb UI Plugin

The default visual interface for Sayri: a transparent Siri-style orb that
lives at the top-right corner of the screen.

## How it works

- Connects to the Sayri daemon over the local IPC socket
  (`sayri-daemon.sock`).
- Listens for state / audio-level / transcription events and renders them
  as colour + glow on a borderless, always-on-top, transparent GTK4 window.
- Clicking the orb toggles the microphone; pressing Escape interrupts
  generation; Q starts a new conversation.

## Lifecycle

The daemon can start the orb automatically when a client connects (the
existing GTK overlay) or you can run it manually:

```bash
sayri orb start    # launch the orb plugin (daemon must be running)
sayri orb stop     # stop a running orb plugin
```

The orb is managed like any other gateway instance — it appears in
`sayri gateway list`.

## State → colour mapping

| State       | Colour              |
|-------------|----------------------|
| idle        | dim green            |
| listening   | bright green + ring  |
| thinking    | pulsing orange       |
| speaking    | pulsing blue         |
| error       | flash red            |

## Dependencies

- GTK4 (`gi.repository.Gtk` 4.0)
- `gtk4-layer-shell` (optional, for Wayland always-on-top positioning)
- No other Python dependencies — the IPC client is self-contained.
