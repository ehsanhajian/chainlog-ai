"""Command line for chainlog-ai."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from chainlog_ai import __version__
from chainlog_ai.case import CaseError, CaseNotFoundError, Event, load_case
from chainlog_ai.classify import CatalogError, classify_case
from chainlog_ai.ingest import SOURCES, InputError, ingest
from chainlog_ai.patterns import SignatureError, find_patterns, match_signature, write_signature
from chainlog_ai.style import paint_redacted
from chainlog_ai.timeline import TimelineError, render_timeline
from chainlog_ai.why import explain


class _ClientInput(argparse.Action):
    """Attach a client name to the preceding file of the source in ``const``."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | None,
        option_string: str | None = None,
    ) -> None:
        inputs = getattr(namespace, "inputs", None) or []
        for item in reversed(inputs):
            if item[0] == self.const and item[2] is None:
                item[2] = values
                return
        parser.error(f"{option_string} needs a preceding --{self.const} file")


class _FileInput(argparse.Action):
    """Record a log file under the source named by ``const``."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | None,
        option_string: str | None = None,
    ) -> None:
        inputs = getattr(namespace, "inputs", None)
        if not inputs:
            inputs = []
            setattr(namespace, "inputs", inputs)
        inputs.append([self.const, values, None, None])


def _preceding_kube(
    parser: argparse.ArgumentParser,
    namespace: argparse.Namespace,
    option_string: str | None,
) -> list[object]:
    inputs = getattr(namespace, "inputs", None) or []
    for item in reversed(inputs):
        if item[0] == "kubernetes":
            if item[3] is None:
                item[3] = {}
            return item
    parser.error(f"{option_string} needs a preceding --kube file")
    raise AssertionError


class _KubeValue(argparse.Action):
    """Attach pod, container, namespace, or restart to the preceding ``--kube`` file."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | None,
        option_string: str | None = None,
    ) -> None:
        item = _preceding_kube(parser, namespace, option_string)
        meta = item[3]
        assert isinstance(meta, dict)
        meta[self.dest] = values


class _KubePrevious(argparse.Action):
    """Mark the preceding ``--kube`` file as the previous container."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | None,
        option_string: str | None = None,
    ) -> None:
        item = _preceding_kube(parser, namespace, option_string)
        meta = item[3]
        assert isinstance(meta, dict)
        meta["previous"] = True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chainlog-ai",
        description="Explain why blockchain infrastructure failed, from the logs you already have.",
    )
    parser.add_argument("--version", action="version", version=f"chainlog-ai {__version__}")
    commands = parser.add_subparsers(dest="command")

    ingest_parser = commands.add_parser(
        "ingest",
        help="Read logs into a local case",
        description=(
            "Read plain text, JSON lines, a container log, or a Kubernetes events file "
            "into a case under ~/.chainlog-ai, or CHAINLOG_AI_HOME when that is set."
        ),
    )
    ingest_parser.set_defaults(func=_cmd_ingest)
    ingest_parser.add_argument(
        "--case",
        help="Append to this case. The case is created when it does not exist.",
    )
    ingest_parser.add_argument(
        "--follow",
        action="store_true",
        help="Read new lines from the local files until this command is stopped",
    )
    _add_case_inputs(ingest_parser)

    classify_parser = commands.add_parser(
        "classify",
        help="Which failure classes are present?",
        description=(
            "Label events in a local case from the rule catalog and print the counts. "
            "An event with no matching rule stays unclassified. Classify does not call a model."
        ),
    )
    classify_parser.add_argument("--case", required=True, help="Case id printed by ingest")
    classify_parser.add_argument(
        "--catalog",
        help="Rule catalog JSON. The built-in catalog is used when this is omitted.",
    )
    classify_parser.set_defaults(func=_cmd_classify)

    timeline_parser = commands.add_parser(
        "timeline",
        help="What happened, in order?",
        description=(
            "Print a local case in time order across every source. "
            "--since and --until narrow the window. "
            "Rows that share a timestamp stay in source order: "
            "cluster, execution, consensus, validator, builder. "
            "A row shows the slot, epoch, and block printed on the line. "
            "A missing slot is filled from a consensus line in the same few seconds. "
            "The timeline names the last healthy slot and the first bad slot. "
            "An execution engine error is paired with the consensus line that requested the payload. "
            "A terminal colors severity, source, clock gaps, the slot boundary, and [redacted], "
            "and moves a long excerpt to the next line."
        ),
    )
    timeline_parser.add_argument("--case", required=True, help="Case id printed by ingest")
    timeline_parser.add_argument("--since", help="Include events at or after this UTC time")
    timeline_parser.add_argument("--until", help="Include events at or before this UTC time")
    timeline_parser.set_defaults(func=_cmd_timeline)

    why_parser = commands.add_parser(
        "why",
        help="Why did it fail?",
        description=(
            "Classify a local case from the rule catalog, read the timeline, and state one trigger. "
            "Contributors are conditions that made it worse. Symptoms are the later misses and restarts. "
            "A doppelganger or a slashable duty is named critical before the trigger. "
            "Pass a case id, or the same files as ingest. "
            "When the evidence is not enough, why names the missing source or the slot the window does not cover, and prints the timeline. "
            "why does not call a model."
        ),
    )
    why_parser.add_argument(
        "--case",
        help="Case id printed by ingest. Created when files are passed and the case does not exist.",
    )
    why_parser.add_argument("--since", help="Include events at or after this UTC time")
    why_parser.add_argument("--until", help="Include events at or before this UTC time")
    _add_case_inputs(why_parser)
    why_parser.set_defaults(func=_cmd_why)

    patterns_parser = commands.add_parser(
        "patterns",
        help="Has this happened before?",
        description=(
            "Compare a local case with earlier cases on this machine. "
            "A match shares a class, the client, the version, and a similar order of classes. "
            "The command prints how many earlier cases matched and their ids. "
            "A case with no earlier case on this machine says this is the first time. "
            "--export writes a signature of class ids, client, version, and class order. "
            "The signature has no log lines and no host names. "
            "--signature reports match or no match against local cases."
        ),
    )
    patterns_parser.add_argument("--case", help="Case id printed by ingest")
    patterns_parser.add_argument("--export", metavar="FILE", help="Write a redacted signature for another host")
    patterns_parser.add_argument("--signature", metavar="FILE", help="Match a signature against local cases")
    patterns_parser.set_defaults(func=_cmd_patterns)
    return parser


def _add_case_inputs(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(inputs=[])
    _add_source(parser, "--execution", "execution", "Execution-client log")
    parser.add_argument(
        "--execution-client",
        action=_ClientInput,
        const="execution",
        metavar="CLIENT",
        help="Name the preceding execution log: geth, nethermind, erigon, besu, or reth",
    )
    _add_source(parser, "--consensus", "consensus", "Consensus-client log")
    parser.add_argument(
        "--consensus-client",
        action=_ClientInput,
        const="consensus",
        metavar="CLIENT",
        help="Name the preceding consensus log: lighthouse, prysm, teku, nimbus, or lodestar",
    )
    _add_source(parser, "--validator", "validator", "Validator or signer log")
    parser.add_argument(
        "--validator-client",
        action=_ClientInput,
        const="validator",
        metavar="CLIENT",
        help="Name the preceding validator log: lighthouse, prysm, teku, nimbus, lodestar, or web3signer",
    )
    _add_source(parser, "--builder", "builder", "Builder log")
    parser.add_argument(
        "--builder-client",
        action=_ClientInput,
        const="builder",
        metavar="CLIENT",
        help="Name the preceding builder log: mev-boost",
    )
    _add_source(parser, "--kube", "kubernetes", "Kubernetes events file or container log")
    parser.add_argument(
        "--namespace",
        action=_KubeValue,
        metavar="NAME",
        help="Namespace of the preceding container log",
    )
    parser.add_argument(
        "--pod",
        action=_KubeValue,
        metavar="NAME",
        help="Pod name of the preceding container log",
    )
    parser.add_argument(
        "--container",
        action=_KubeValue,
        metavar="NAME",
        help="Container name of the preceding container log",
    )
    parser.add_argument(
        "--restart",
        action=_KubeValue,
        type=int,
        metavar="N",
        help="Restart count of the preceding container log",
    )
    parser.add_argument(
        "--previous",
        action=_KubePrevious,
        nargs=0,
        help="The preceding container log is the previous instance",
    )


def _add_source(parser: argparse.ArgumentParser, flag: str, source: str, help_text: str) -> None:
    parser.add_argument(flag, action=_FileInput, const=source, metavar="FILE", help=help_text)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (None, 0):
            return 0
        return 3
    if not args.command:
        parser.print_help()
        return 0
    return args.func(args)


def _cmd_ingest(args: argparse.Namespace) -> int:
    inputs = _inputs(args)
    if not inputs:
        print("ingest needs an input file", file=sys.stderr)
        return 3
    announced = False

    def on_ready(case_id: str) -> None:
        nonlocal announced
        if args.follow:
            print(f"case {case_id}", flush=True)
            announced = True

    def on_events(events: list[Event]) -> None:
        for event in events:
            excerpt = paint_redacted(event.excerpt) if sys.stdout.isatty() else event.excerpt
            print(excerpt, flush=True)

    try:
        case_id = ingest(
            inputs,
            case_id=args.case,
            follow=args.follow,
            on_ready=on_ready,
            on_events=on_events if args.follow else None,
        )
    except (InputError, CaseError) as exc:
        print(exc, file=sys.stderr)
        return 3
    if not announced:
        print(f"case {case_id}")
    return 0


def _cmd_classify(args: argparse.Namespace) -> int:
    try:
        color, _width = _terminal_view()
        text = classify_case(
            args.case,
            catalog=None if args.catalog is None else args.catalog,
            color=color,
        )
    except CaseNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 3
    except (CaseError, CatalogError) as exc:
        print(exc, file=sys.stderr)
        return 3
    print(text)
    return 0


def _cmd_timeline(args: argparse.Namespace) -> int:
    try:
        case = load_case(args.case)
        color, width = _terminal_view()
        text = render_timeline(case, since=args.since, until=args.until, color=color, width=width)
    except CaseNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 3
    except (CaseError, TimelineError) as exc:
        print(exc, file=sys.stderr)
        return 3
    print(text)
    return 0


def _terminal_view() -> tuple[bool, int | None]:
    if not sys.stdout.isatty():
        return False, None
    width = shutil.get_terminal_size().columns
    if width < 1:
        return True, None
    return True, width


def _inputs(args: argparse.Namespace) -> list[tuple[str, ...]]:
    return [
        (item[0], item[1], item[2], item[3] if len(item) > 3 else None)
        for item in args.inputs
        if item[0] in SOURCES
    ]


def _cmd_why(args: argparse.Namespace) -> int:
    inputs = _inputs(args)
    if args.case is None and not inputs:
        print("why needs a case id or an input file", file=sys.stderr)
        return 3
    try:
        case_id = ingest(inputs, case_id=args.case) if inputs else args.case
        color, width = _terminal_view()
        classify_case(case_id)
        text, cited = explain(
            load_case(case_id),
            color=color,
            width=width,
            since=getattr(args, "since", None),
            until=getattr(args, "until", None),
        )
    except CaseNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 3
    except (CaseError, InputError, CatalogError, TimelineError) as exc:
        print(exc, file=sys.stderr)
        return 3
    print(text)
    return 0 if cited else 1


def _cmd_patterns(args: argparse.Namespace) -> int:
    if args.signature and (args.case or args.export):
        print("patterns takes a signature or a case, not both", file=sys.stderr)
        return 3
    if args.export and not args.case:
        print("patterns --export needs a case id", file=sys.stderr)
        return 3
    if not args.case and not args.signature:
        print("patterns needs a case id or a signature file", file=sys.stderr)
        return 3
    try:
        if args.signature:
            text = match_signature(Path(args.signature))
        elif args.export:
            text = write_signature(args.case, Path(args.export))
        else:
            text = find_patterns(args.case)
    except CaseNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 3
    except (CaseError, CatalogError, SignatureError) as exc:
        print(exc, file=sys.stderr)
        return 3
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
