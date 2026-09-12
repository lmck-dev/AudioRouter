"""Command line front end - the headless half of the product.

The GUI will drive `Engine` directly rather than shelling out to this, so
nothing here may contain logic of its own: every subcommand is an argument
parse, one engine call, and some printing. If a command here needs to think,
the thinking belongs in the engine where the GUI can reach it too.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from typing import Any

from .channels import ChannelError
from .config import ConfigError, config_path
from .effects import EffectError, all_specs
from . import install
from .engine import AutoRouter, DaemonRecord, Engine, EngineError, MoveResult, daemon_pid
from .pwgraph import PwError
from .routing import MATCH_FIELDS, RoutingError

USER_ERRORS = (
    EngineError, ChannelError, ConfigError, EffectError, RoutingError, PwError,
    install.InstallError,
)


def _parse_params(pairs: list[str]) -> dict[str, float]:
    """`freq=1000 q=0.7` from the command line into a parameter dict."""
    values: dict[str, float] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise EffectError(f"expected key=value, got {pair!r}")
        try:
            values[key.strip()] = float(raw)
        except ValueError as exc:
            raise EffectError(f"{key}: {raw!r} is not a number") from exc
    return values


def _print_table(rows: list[tuple[str, ...]], headers: tuple[str, ...]) -> None:
    if not rows:
        print("(none)")
        return
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    line = "  ".join(str(h).ljust(w) for h, w in zip(headers, widths, strict=True))
    print(line.rstrip())
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())


# -- commands -------------------------------------------------------------


def cmd_devices(engine: Engine, args: argparse.Namespace) -> int:
    rows = [(name, label) for name, label in engine.device_choices()]
    _print_table(rows, ("node.name", "device"))
    return 0


def cmd_effects(engine: Engine, args: argparse.Namespace) -> int:
    for spec in all_specs():
        mark = "" if spec.available else "   [unavailable]"
        print(f"{spec.kind:<12} {spec.label}{mark}")
        print(f"{'':<12} {spec.summary}")
        for param in spec.params:
            unit = f" {param.unit}" if param.unit else ""
            print(
                f"{'':<14} {param.key:<14} {param.label:<12} "
                f"default {param.default:g}{unit}  range {param.minimum:g}..{param.maximum:g}"
            )
        for missing in spec.unsatisfied():
            print(f"{'':<14} needs: {missing.explain()}")
    return 0


def cmd_status(engine: Engine, args: argparse.Namespace) -> int:
    status = engine.status()
    if args.json:
        print(json.dumps(status, indent=2))
        return 0

    print(f"auto-route: {'on' if status['auto_route'] else 'off'}")
    print()
    print("Channels")
    rows = []
    for channel in status["channels"]:
        state = "running" if channel["running"] else ("off" if not channel["enabled"] else "stopped")
        if channel["needs_restart"]:
            state += " (stale)"
        device = channel["device"] or "(default)"
        if not channel["device_present"]:
            device += " [missing]"
        rows.append(
            (
                channel["slug"],
                channel["name"],
                state,
                device,
                ", ".join(channel["effects"]) or "-",
            )
        )
    _print_table(rows, ("id", "name", "state", "output", "effects"))

    print()
    print("Playing")
    rows = []
    for stream in status["streams"]:
        rows.append(
            (
                stream["id"],
                stream["app"],
                (stream["title"] or "")[:32],
                stream["channel"] or (stream["sink"] or "-"),
                stream["rule_channel"] or "-",
            )
        )
    _print_table(rows, ("id", "app", "title", "now on", "rule says"))

    if status["rules"]:
        print()
        print("Rules (first match wins)")
        for index, rule in enumerate(status["rules"]):
            print(f"  {index}. {rule}")

    for problem in status["problems"]:
        print(f"! {problem}")
    for orphan in status["orphans"]:
        print(f"! orphaned channel process: {orphan} (run 'apply' to clean up)")
    return 0


def cmd_init(engine: Engine, args: argparse.Namespace) -> int:
    channels = engine.adopt_devices()
    for channel in channels:
        print(f"added channel {channel.slug} -> {channel.device}")
    print(f"saved {config_path()}")
    return 0


def cmd_channel_add(engine: Engine, args: argparse.Namespace) -> int:
    channel = engine.create_channel(args.slug, name=args.name or "", device=args.device or "")
    print(f"added channel {channel.slug} ({channel.name}) -> {channel.device or '(default sink)'}")
    return 0


def cmd_channel_rm(engine: Engine, args: argparse.Namespace) -> int:
    engine.delete_channel(args.slug)
    print(f"removed channel {args.slug}")
    return 0


def cmd_channel_set(engine: Engine, args: argparse.Namespace) -> int:
    if args.device is not None:
        engine.set_device(args.slug, args.device)
    if args.name is not None:
        engine.rename_channel(args.slug, args.name)
    if args.enabled is not None:
        engine.set_enabled(args.slug, args.enabled)
    channel = engine.channel(args.slug)
    print(f"{channel.slug}: {channel.name} -> {channel.device or '(default sink)'}"
          f" {'enabled' if channel.enabled else 'disabled'}")
    return 0


def cmd_effect_add(engine: Engine, args: argparse.Namespace) -> int:
    effect = engine.add_effect(args.slug, args.kind, _parse_params(args.params), index=args.at)
    print(f"{args.slug}: added {effect.kind} {effect.resolved()}")
    return 0


def cmd_effect_rm(engine: Engine, args: argparse.Namespace) -> int:
    effect = engine.remove_effect(args.slug, args.index)
    print(f"{args.slug}: removed {effect.kind}")
    return 0


def cmd_effect_set(engine: Engine, args: argparse.Namespace) -> int:
    effect = engine.set_effect_params(args.slug, args.index, _parse_params(args.params))
    print(f"{args.slug}: {effect.kind} {effect.resolved()}")
    return 0


def cmd_effect_list(engine: Engine, args: argparse.Namespace) -> int:
    channel = engine.channel(args.slug)
    rows = []
    for index, effect in enumerate(channel.effects):
        values = " ".join(f"{k}={v:g}" for k, v in effect.resolved().items())
        rows.append((index, effect.kind, "on" if effect.enabled else "off", values))
    _print_table(rows, ("#", "effect", "state", "settings"))
    return 0


def cmd_effect_mv(engine: Engine, args: argparse.Namespace) -> int:
    effect = engine.move_effect(args.slug, args.index, args.to)
    print(f"{args.slug}: moved {effect.kind} to position {args.to}")
    return 0


def cmd_rule_add(engine: Engine, args: argparse.Namespace) -> int:
    rule = engine.add_rule(args.field, args.pattern, args.channel, index=args.at)
    print(f"added rule: {rule.describe()}")
    return 0


def cmd_rule_rm(engine: Engine, args: argparse.Namespace) -> int:
    rule = engine.remove_rule(args.index)
    print(f"removed rule: {rule.describe()}")
    return 0


def cmd_rule_mv(engine: Engine, args: argparse.Namespace) -> int:
    rule = engine.move_rule(args.index, args.to)
    print(f"moved rule to {args.to}: {rule.describe()}")
    return 0


def cmd_apply(engine: Engine, args: argparse.Namespace) -> int:
    report = engine.apply()
    for line in report.describe():
        print(line)
    if not report.changed and report.ok:
        print("already up to date")
    if args.route and report.ok:
        cmd_route(engine, args)
    return 0 if report.ok else 1


def cmd_start(engine: Engine, args: argparse.Namespace) -> int:
    print(engine.start_channel(args.slug).describe())
    return 0


def cmd_stop(engine: Engine, args: argparse.Namespace) -> int:
    if args.slug:
        print(engine.stop_channel(args.slug).describe())
        return 0
    report = engine.stop_all()
    for line in report.describe() or ["nothing was running"]:
        print(line)
    return 0


def cmd_route(engine: Engine, args: argparse.Namespace) -> int:
    results = engine.route()
    if not results:
        print("nothing is playing")
    for result in results:
        mark = "moved" if result.moved else "kept "
        print(f"{mark} {result.stream} -> {result.channel or '-'} ({result.reason})")
    return 0


def cmd_send(engine: Engine, args: argparse.Namespace) -> int:
    result = engine.send(args.stream, args.channel)
    print(f"moved {result.stream} -> {result.channel}")
    return 0


def cmd_watch(engine: Engine, args: argparse.Namespace) -> int:
    """Start the channels and keep routing new streams until interrupted."""
    report = engine.apply()
    for line in report.describe():
        print(line)

    stop = threading.Event()

    def _report(result: MoveResult) -> None:
        print(f"routed {result.stream} -> {result.channel}", flush=True)

    def _signal(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)
    code = 0
    with DaemonRecord(), AutoRouter(engine, on_move=_report, follow_config=True) as auto:
        print("watching for new streams; Ctrl-C to stop", flush=True)
        while not stop.wait(1.0):
            if auto.ended:
                # Exit non-zero so a service manager starts us again, which
                # re-applies the channels PipeWire just lost.
                print("lost the PipeWire graph; exiting", file=sys.stderr, flush=True)
                code = 1
                break
    if args.stop_channels:
        engine.stop_all()
    print("stopped")
    return code


def cmd_launcher(engine: Engine, args: argparse.Namespace) -> int:
    if args.remove:
        print("removed" if install.remove_launcher() else "no launcher was installed")
        return 0
    print(f"installed {install.install_launcher()}")
    return 0


def cmd_login(engine: Engine, args: argparse.Namespace) -> int:
    if args.state == "on":
        print(f"enabled {install.enable_login_service()}")
    elif args.state == "off":
        print("disabled" if install.disable_login_service() else "was not enabled")
    else:
        pid = daemon_pid()
        print(f"start at login: {'on' if install.login_service_enabled() else 'off'}")
        print(f"routing daemon: {f'running (pid {pid})' if pid else 'not running'}")
    return 0


# -- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audiorouter",
        description="Route each application's audio to its own channel, with its own effects.",
    )
    parser.add_argument("--config", help="path to config.json (default: ~/.config/audiorouter)")
    parser.add_argument(
        "--dry-run", action="store_true", help="say what would happen; change nothing"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list real output devices").set_defaults(func=cmd_devices)
    sub.add_parser("effects", help="list available effects").set_defaults(func=cmd_effects)
    sub.add_parser("init", help="create one channel per output device").set_defaults(func=cmd_init)

    status = sub.add_parser("status", help="show channels, streams and rules")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    channel = sub.add_parser("channel", help="manage channels").add_subparsers(
        dest="channel_command", required=True
    )
    add = channel.add_parser("add", help="create a channel")
    add.add_argument("slug")
    add.add_argument("--name")
    add.add_argument("--device")
    add.set_defaults(func=cmd_channel_add)
    remove = channel.add_parser("rm", help="delete a channel")
    remove.add_argument("slug")
    remove.set_defaults(func=cmd_channel_rm)
    change = channel.add_parser("set", help="change a channel")
    change.add_argument("slug")
    change.add_argument("--name")
    change.add_argument("--device")
    change.add_argument("--enable", dest="enabled", action="store_true", default=None)
    change.add_argument("--disable", dest="enabled", action="store_false")
    change.set_defaults(func=cmd_channel_set)

    effect = sub.add_parser("effect", help="manage a channel's effect chain").add_subparsers(
        dest="effect_command", required=True
    )
    eadd = effect.add_parser("add", help="append an effect")
    eadd.add_argument("slug")
    eadd.add_argument("kind")
    eadd.add_argument("params", nargs="*", help="key=value settings")
    eadd.add_argument("--at", type=int, help="insert at this position instead of the end")
    eadd.set_defaults(func=cmd_effect_add)
    erm = effect.add_parser("rm", help="remove an effect by position")
    erm.add_argument("slug")
    erm.add_argument("index", type=int)
    erm.set_defaults(func=cmd_effect_rm)
    eset = effect.add_parser("set", help="change an effect's settings")
    eset.add_argument("slug")
    eset.add_argument("index", type=int)
    eset.add_argument("params", nargs="+", help="key=value settings")
    eset.set_defaults(func=cmd_effect_set)
    elist = effect.add_parser("list", help="show a channel's chain")
    elist.add_argument("slug")
    elist.set_defaults(func=cmd_effect_list)
    emv = effect.add_parser("mv", help="reorder the chain")
    emv.add_argument("slug")
    emv.add_argument("index", type=int)
    emv.add_argument("to", type=int)
    emv.set_defaults(func=cmd_effect_mv)

    rule = sub.add_parser("rule", help="manage routing rules").add_subparsers(
        dest="rule_command", required=True
    )
    radd = rule.add_parser("add", help="add a rule")
    radd.add_argument("field", choices=MATCH_FIELDS)
    radd.add_argument("pattern")
    radd.add_argument("channel")
    radd.add_argument("--at", type=int)
    radd.set_defaults(func=cmd_rule_add)
    rrm = rule.add_parser("rm", help="remove a rule by position")
    rrm.add_argument("index", type=int)
    rrm.set_defaults(func=cmd_rule_rm)
    rmv = rule.add_parser("mv", help="reorder rules")
    rmv.add_argument("index", type=int)
    rmv.add_argument("to", type=int)
    rmv.set_defaults(func=cmd_rule_mv)

    apply_cmd = sub.add_parser("apply", help="start, restart or stop channels to match the config")
    apply_cmd.add_argument("--route", action="store_true", help="also apply rules afterwards")
    apply_cmd.set_defaults(func=cmd_apply)

    start = sub.add_parser("start", help="start one channel")
    start.add_argument("slug")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop one channel, or all of them")
    stop.add_argument("slug", nargs="?")
    stop.set_defaults(func=cmd_stop)

    sub.add_parser("route", help="apply the rules to what is playing now").set_defaults(
        func=cmd_route
    )

    send = sub.add_parser("send", help="move one stream to a channel")
    send.add_argument("stream", type=int, help="stream id from 'status'")
    send.add_argument("channel")
    send.set_defaults(func=cmd_send)

    watch = sub.add_parser("watch", help="run as a daemon: apply, then route new streams")
    watch.add_argument(
        "--stop-channels", action="store_true", help="also stop channels when interrupted"
    )
    watch.set_defaults(func=cmd_watch)

    launcher = sub.add_parser("launcher", help="add the window to the application menu")
    launcher.add_argument("--remove", action="store_true", help="take it out again")
    launcher.set_defaults(func=cmd_launcher)

    login = sub.add_parser("login", help="keep routing in the background, from login")
    login.add_argument("state", nargs="?", choices=("on", "off", "status"), default="status")
    login.set_defaults(func=cmd_login)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    from pathlib import Path

    engine = Engine.load(Path(args.config) if args.config else None, dry_run=args.dry_run)
    try:
        return int(args.func(engine, args))
    except USER_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _entry() -> Any:  # pragma: no cover - console-script shim
    raise SystemExit(main())
