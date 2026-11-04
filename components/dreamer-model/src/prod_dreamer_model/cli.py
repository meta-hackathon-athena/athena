"""Command line for the production dreamer.

    uv run python -m prod_dreamer_model.cli all          # reset, seed, dream, report
    uv run python -m prod_dreamer_model.cli seed --days 14
    uv run python -m prod_dreamer_model.cli dream --rounds 4
    uv run python -m prod_dreamer_model.cli dream --offline   # rules only, no Muse
    uv run python -m prod_dreamer_model.cli report
    uv run python -m prod_dreamer_model.cli reset
    uv run python -m prod_dreamer_model.cli load export.jsonl   # context-engine JSONL -> graph

Shared Vixy graph (written by the Vixy runtime at the monorepo root; never reset, no frames),
run from the monorepo root:

    uv run python -m prod_dreamer_model.cli --layout unified --user demo \
        dream --offline --history-db data/vixy_events.sqlite3
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from .bridge import EventBridge, EventHistory, bundles_from_records, load_export_jsonl
from .config import DreamerConfig, unified_graph_name
from .dreamer import Dreamer, ResponsesMuseClient
from .graph import open_graph
from .mock_data import synthetic_days
from .report import collect, render_text, write_outputs
from .tools import GraphToolBox


def build_config(args: argparse.Namespace) -> DreamerConfig:
    layout = getattr(args, "layout", None)
    user = getattr(args, "user", None)
    graph = getattr(args, "graph", None)
    if graph is None and layout == "unified" and user:
        graph = unified_graph_name(user)
    session = getattr(args, "session", None)
    if session is None and layout == "unified" and user:
        session = user                      # Vixy convention: default session id = user id
    return DreamerConfig.from_env(
        graph_name=graph,
        session_id=session,
        tz=getattr(args, "tz", None),
        graph_layout=layout,
        user_id=user,
    )


def cmd_seed(args: argparse.Namespace, config: DreamerConfig) -> int:
    days = synthetic_days(
        start_date=args.start_date, days=args.days, seed=args.seed, tz_name=config.tz
    )
    dates = [d.date for d in days]
    print(f"seeding {len(days)} mock days ({dates[0]} .. {dates[-1]})")
    print(f"  frames: {sum(len(d.frames) for d in days)}  "
          f"notes: {sum(len(d.notes) for d in days)}")

    export_path = Path(args.out_dir) / "dreamer_export.jsonl"
    export_path.parent.mkdir(parents=True, exist_ok=True)
    with EventBridge(config.events_db, config.session_id, config.tz) as bridge:
        accepted = duplicates = 0
        for mock_day in days:
            stats = bridge.ingest_mock_day(mock_day)
            accepted += stats.accepted
            duplicates += stats.duplicates
        end = dates[-1] + timedelta(days=1)
        written = bridge.export_jsonl(export_path, dates[0], end)
        bundles = bridge.bundles_for_range(dates[0], end)
    print(f"  context-engine: {accepted} accepted, {duplicates} duplicate")
    print(f"  export ({written} records): {export_path}")

    graph = open_graph(config)
    graph.bootstrap()
    totals = graph.load_bundles(bundles)
    print(f"  graph {config.graph_name}: {totals['days']} days, "
          f"{totals['frames']} frames, {totals['notes']} notes loaded")
    return 0


def cmd_load(args: argparse.Namespace, config: DreamerConfig) -> int:
    """context-engine JSONL export -> legacy graph (no re-ingestion, no new SQLite)."""
    records = load_export_jsonl(args.export)
    session = config.session_id if args.session_filter else None
    bundles = bundles_from_records(records, graph_tz(config), session)
    graph = open_graph(config)
    graph.bootstrap()
    totals = graph.load_bundles(bundles)
    print(f"loaded {len(records)} records from {args.export}: {totals['days']} days, "
          f"{totals['frames']} frames, {totals['notes']} notes into {config.graph_name}")
    return 0


def graph_tz(config: DreamerConfig):
    from zoneinfo import ZoneInfo

    return ZoneInfo(config.tz)


def _history(args: argparse.Namespace) -> EventHistory | None:
    if getattr(args, "history_jsonl", None):
        return EventHistory.from_jsonl(args.history_jsonl)
    if getattr(args, "history_db", None):
        return EventHistory.from_sqlite(args.history_db)
    return None


def cmd_dream(args: argparse.Namespace, config: DreamerConfig) -> int:
    graph = open_graph(config)
    graph.bootstrap()
    toolbox = GraphToolBox(graph, config)
    stats = graph.stats()
    if config.unified:
        if stats.visits == 0 and stats.notes == 0:
            print(f"{config.graph_name} has no visits or notes yet — run the Vixy runtime first",
                  file=sys.stderr)
            return 2
    elif stats.frames == 0:
        print("graph has no frames — run `seed` first", file=sys.stderr)
        return 2
    history = _history(args) if config.unified else None
    if config.unified and history is None:
        print("no --history-db/--history-jsonl: notes cannot be verified against "
              "context-engine and will not be consolidated", file=sys.stderr)

    client = None
    if args.offline or not getattr(args, "online", False):
        print("dreaming offline (deterministic rules only)")
    elif not config.has_model:
        print("MODEL_API_KEY not set — dreaming offline", file=sys.stderr)
    else:
        client = ResponsesMuseClient(config)
        print(f"dreaming with {config.muse_model} "
              f"({args.rounds or config.max_rounds} rounds, "
              f"{config.max_turns_per_round} turns each)")

    extractor = None
    if config.unified and getattr(args, "model_extraction", False):
        # opt-in, budgeted, validated, cached: claims the rules miss (paid API; needs MODEL_API_KEY)
        from .model_extract import ModelExtractor, model_from_env

        model = model_from_env("DREAMER_EXTRACT", model=config.muse_model,
                              allow_contributor_model=config.allow_contributor_model and not config.unified)
        if model is None:
            print("--model-extraction needs MODEL_API_KEY; extracting with rules only", file=sys.stderr)
        else:
            extractor = ModelExtractor(model, tz=config.tz)
            print(f"model-assisted extraction with {model.model_id} (max {model.max_calls} calls)")

    dreamer = Dreamer(toolbox, config, client=client, history=history, extractor=extractor)
    try:
        result = dreamer.dream(rounds=args.rounds, allow_fallback=True)
    finally:
        if history is not None:
            history.close()
    print(json.dumps(result.as_dict(), indent=2, default=str))
    patterns = graph.fetch_patterns()
    memories = graph.fetch_memories()
    print(f"stored: {len(patterns)} patterns, {len(memories)} long-term memories")
    return 0


def cmd_report(args: argparse.Namespace, config: DreamerConfig) -> int:
    graph = open_graph(config)
    data = collect(graph)
    print(render_text(data, show_evidence=not args.no_evidence))
    paths = write_outputs(graph, args.out_dir)
    for kind, path in paths.items():
        print(f"wrote {kind}: {path}")
    return 0


def cmd_reset(args: argparse.Namespace, config: DreamerConfig) -> int:
    graph = open_graph(config)
    try:
        graph.reset()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"cleared graph {config.graph_name}")
    if args.events:
        db = Path(config.events_db)
        if db.exists():
            db.unlink()
            print(f"removed event store {db}")
    return 0


def cmd_all(args: argparse.Namespace, config: DreamerConfig) -> int:
    if config.unified:
        print("`all` resets and seeds mock frames; it is legacy-only. On the shared "
              "graph run `dream` (the runtime writes the observations).", file=sys.stderr)
        return 2
    graph = open_graph(config)
    if not args.keep:
        graph.reset()
        print(f"cleared graph {config.graph_name}")
    code = cmd_seed(args, config)
    if code:
        return code
    code = cmd_dream(args, config)
    if code:
        return code
    return cmd_report(args, config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prod_dreamer_model",
        description="Vixy production dreamer: context-engine -> FalkorDB -> Muse.",
    )
    parser.add_argument("--graph", help=f"FalkorDB graph name (default: vixy_dreamer)")
    parser.add_argument("--session", help="session/user id")
    parser.add_argument("--tz", help="local timezone (default: Asia/Singapore)")
    parser.add_argument("--layout", choices=["legacy", "unified"],
                        help="graph layout (default: $DREAMER_GRAPH_LAYOUT or legacy)")
    parser.add_argument("--user", help="Vixy user id; with --layout unified the graph is vixy_<user>")
    parser.add_argument("--out-dir", default="dream_output", help="output directory")

    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="mock wearable data -> context-engine -> graph")
    seed.add_argument("--days", type=int, default=14, help="number of mock days")
    seed.add_argument("--start-date", help="YYYY-MM-DD (default: previous Monday)")
    seed.add_argument("--seed", type=int, default=7, help="random seed")
    seed.set_defaults(func=cmd_seed)

    dream = sub.add_parser("dream", help="run the Muse dream loop")
    dream.add_argument("--rounds", type=int, default=None,
                       help="focus rounds (max 4); default: config max_rounds")
    dream.add_argument("--offline", action="store_true",
                       help="skip Muse and use the deterministic rules only")
    dream.add_argument("--online", action="store_true",
                       help="explicitly enable paid Meta tool-loop inference (key alone never enables it)")
    dream.add_argument("--history-db", help="unified: context-engine SQLite to verify notes against")
    dream.add_argument("--history-jsonl", help="unified: context-engine JSONL export instead")
    dream.add_argument("--model-extraction", action="store_true",
                       help="unified: also extract claims with the Meta model (paid; budgeted)")
    dream.set_defaults(func=cmd_dream)

    load = sub.add_parser("load", help="context-engine JSONL export -> graph (legacy layout)")
    load.add_argument("export", help="vixy.dreamer_event.v1 JSONL file")
    load.add_argument("--session-filter", action="store_true",
                      help="only load records whose session_id matches --session")
    load.set_defaults(func=cmd_load)

    report = sub.add_parser("report", help="print patterns + long-term memories")
    report.add_argument("--no-evidence", action="store_true")
    report.set_defaults(func=cmd_report)

    reset = sub.add_parser("reset", help="clear the graph")
    reset.add_argument("--events", action="store_true", help="also delete the SQLite event store")
    reset.set_defaults(func=cmd_reset)

    allcmd = sub.add_parser("all", help="reset, seed, dream, report")
    allcmd.add_argument("--days", type=int, default=14)
    allcmd.add_argument("--start-date")
    allcmd.add_argument("--seed", type=int, default=7)
    allcmd.add_argument("--rounds", type=int, default=None)
    allcmd.add_argument("--offline", action="store_true")
    allcmd.add_argument("--online", action="store_true", help="explicitly enable paid Meta inference")
    allcmd.add_argument("--keep", action="store_true", help="do not reset first")
    allcmd.add_argument("--no-evidence", action="store_true")
    allcmd.set_defaults(func=cmd_all)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = build_config(args)
    return args.func(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
