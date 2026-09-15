# AudioRouter

Send each application's audio to its own **channel**, and give every channel its
own independent effect chain.

A channel is a named sink backed by its own PipeWire filter-chain process, bound
to one real output device. Because each channel is a separate process with a
node name we choose, there is no limit on how many can run, and each one appears
in the desktop's ordinary sound settings under its own name.

This is the headless half: an engine plus a CLI. The GUI drives the same
`Engine` object.

## Why not EasyEffects

EasyEffects finds its own sink by the hard-coded name `easyeffects_sink`, so a
second instance wires its effect chain to the first instance's sink instead of
its own. At most one instance can ever work, and its chain is global. That was
measured, reproduced on two and three instances, and confirmed in the 8.1.6
source before this design was chosen.

## Installing (Fedora, Nobara)

Install `audiorouter-<version>.fc44.x86_64.rpm` - double-click it, or

```sh
sudo dnf install ./audiorouter-0.1.0-1.fc44.x86_64.rpm
```

That brings everything it needs: PyQt6, PipeWire's LV2 loader, the LSP plugins
and the effects toolbox (the toolbox packs are weak dependencies, so they can be
removed). Then open **Audio Router** from the menu. The first time it opens it
switches on routing from login for that user, and offers to create a channel for
each output.

To build the package (needs only podman):

```sh
packaging/build-rpm.sh          # -> dist/audiorouter-<version>-1.fc44.x86_64.rpm
```

It builds in a clean Fedora container and runs the test suite there.

## Requirements (running from a checkout)

- PipeWire (with `pw-dump`, `pw-metadata`, `pipewire` on `PATH`)
- Python 3.11+
- Optional: `pipewire-module-filter-chain-lv2` and `lsp-plugins-lv2` for the
  compressor and limiter. Everything else is built into PipeWire. Run
  `audiorouter effects` to see what this machine can actually load.

## The window

```sh
python -m audiorouter.gui.main        # or: audiorouter-gui
```

Channels are on the left with their effect chain on the right, and everything
currently making sound is listed underneath with a menu to send it elsewhere.
Moving something takes effect immediately; **Always send this app here**
remembers it, and the remembered apps can be forgotten again in the same window.

Edits apply themselves. Changing an effect restarts that channel behind the
scenes, and anything playing through it is put back afterwards.

## Quick start

```sh
python -m audiorouter init                     # one channel per output device
python -m audiorouter effect add speakers highpass frequency=80 poles=2
python -m audiorouter apply                    # start/restart channels to match
python -m audiorouter rule add app firefox headphones
python -m audiorouter route                    # place what is playing now
python -m audiorouter watch                    # ...and everything that starts later
```

`status` shows the whole picture: channels, what is playing, where it is, and
where the rules say it should be.

```
python -m audiorouter status
python -m audiorouter send 143 speakers        # move one stream by hand
```

## How routing behaves

Rules are ordered and the first match wins. A pattern with `*` or `?` is a glob;
anything else matches as a substring, case-insensitively. Three fields can be
matched:

| field    | reads                        | note                                      |
|----------|------------------------------|-------------------------------------------|
| `app`    | `application.name`           | the friendly name; usually what you want   |
| `binary` | `application.process.binary` | the real executable - `paplay` is `pacat`  |
| `title`  | `media.name`                 | what is playing, not the app               |

`watch` places each stream **once**, when it first appears. If you then move it
yourself, it stays where you put it.

## Development

```sh
python -m unittest discover -s tests -t .
```

The tests need neither PipeWire nor audio hardware.
