"""Command line for chainlog-ai."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from chainlog_ai import __version__
from chainlog_ai.ask import REMOTE_NOTICE, RemoteError, answer, case_is_redacted, send_remote
from chainlog_ai.case import CaseError, CaseNotFoundError, Event, case_window, load_case
from chainlog_ai.classify import CatalogError, classify_case
from chainlog_ai.ingest import SOURCES, InputError, ingest
from chainlog_ai.patterns import SignatureError, find_patterns, match_signature, write_signature
from chainlog_ai.recognize import RecognizeError, Recognized, recognize
from chainlog_ai.report import ReportError, verify_bundle, write_report
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


_COMMANDS = {"ingest", "classify", "timeline", "why", "patterns", "ask", "report"}
_BARE_EXAMPLE = (
    "chainlog-ai examples/geth.log examples/beacon.log "
    "examples/validator.log examples/mev-boost.log examples/events.json"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chainlog-ai",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Pass the log files and read the cause.\n"
            "\n"
            f"    {_BARE_EXAMPLE}\n"
            "\n"
            "A directory is read the same way. A skipped file in a directory is named. "
            "A file that cannot be recognized stops the command. "
            "The commands below inspect a case that is already on this machine."
        ),
    )
    parser.add_argument("--version", action="version", version=f"chainlog-ai {__version__}")
    commands = parser.add_subparsers(dest="command")

    ingest_parser = commands.add_parser(
        "ingest",
        help="Read logs into a local case",
        description=(
            "Read plain text, JSON lines, a container log, a Kubernetes events file, "
            "or a ValidatorPulse or Prometheus alert "
            "into a case under ~/.chainlog-ai, or CHAINLOG_AI_HOME when that is set. "
            "--alert puts the alert name and firing time on the timeline. "
            "The window defaults to one hour before the alert and fifteen minutes after. "
            "The alert is not the root cause. "
            "--metrics reads a Prometheus snapshot or range file. "
            "A metric that crosses a threshold in that window is a contributor, not the root cause."
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
    ingest_parser.add_argument(
        "--alert",
        metavar="FILE",
        help="ValidatorPulse or Prometheus alert JSON. Alertmanager webhook or one saved alert.",
    )
    ingest_parser.add_argument(
        "--metrics",
        metavar="FILE",
        help="Prometheus snapshot or range file. A series that crosses a threshold in the window is a contributor.",
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
            "cluster, execution, consensus, validator, builder, alert, metric. "
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
            "A relay or builder timeout that precedes a missed proposal is the trigger, and the miss stays a symptom. "
            "A doppelganger or a slashable duty is named critical before the trigger. "
            "Pass a case id, or the same files as ingest. "
            "--metrics reads a Prometheus snapshot or range file and cites a metric that crosses a threshold in the same window as the trigger. "
            "When the evidence is not enough, why names the missing source or the slot the window does not cover, and prints the timeline. "
            "why does not call a model and does not scrape a cluster."
        ),
    )
    why_parser.add_argument(
        "--case",
        help="Case id printed by ingest. Created when files are passed and the case does not exist.",
    )
    why_parser.add_argument("--since", help="Include events at or after this UTC time")
    why_parser.add_argument("--until", help="Include events at or before this UTC time")
    why_parser.add_argument(
        "--metrics",
        metavar="FILE",
        help="Prometheus snapshot or range file. A series that crosses a threshold in the window is a contributor.",
    )
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
            "The pattern names the client version from a banner or startup line, or unknown when the logs did not print one. "
            "A pattern that begins after a version change names the previous version and the new version. "
            "--export writes a signature of class ids, client, version, and class order. "
            "The signature has no log lines and no host names. "
            "--signature reports match or no match against local cases."
        ),
    )
    patterns_parser.add_argument("--case", help="Case id printed by ingest")
    patterns_parser.add_argument("--export", metavar="FILE", help="Write a redacted signature for another host")
    patterns_parser.add_argument("--signature", metavar="FILE", help="Match a signature against local cases")
    patterns_parser.set_defaults(func=_cmd_patterns)

    ask_parser = commands.add_parser(
        "ask",
        help="What should I check next?",
        description=(
            "Answer a question from a local case. "
            "The question is an argument and a case id is required. "
            "With no model configured, the answer comes from the cause and the runbook, and it cites the timeline rows it used. "
            "For each cited class it prints the next read-only checks and the source of those checks. "
            "An answer that cannot cite a row says the case does not contain that. "
            "--remote is off unless this invocation sets it. "
            "It prints that a remote call is about to happen, then sends the question, the cited rows, and the runbook text. "
            "The call is refused when redaction did not run."
        ),
    )
    ask_parser.add_argument("--case", required=True, help="Case id printed by ingest")
    ask_parser.add_argument(
        "--remote",
        action="store_true",
        help="Send the question, cited rows, and runbook text to CHAINLOG_AI_REMOTE_URL. Off unless this invocation sets it.",
    )
    ask_parser.add_argument("question", help="Question to answer from the case")
    ask_parser.set_defaults(func=_cmd_ask)

    report_parser = commands.add_parser(
        "report",
        help="Write the incident report",
        description=(
            "Write markdown and JSON for a local case, next to the case. "
            "The report includes the cause, the timeline boundary, the citations, and the pattern note. "
            "A doppelganger or a slashable duty is the first section when the cause includes one. "
            "--no-paths drops local file paths so the report can leave the machine. "
            "--bundle writes the redacted citations, the signature, and a hash of that content. "
            "--verify checks that hash."
        ),
    )
    report_parser.add_argument("--case", help="Case id printed by ingest")
    report_parser.add_argument(
        "--no-paths",
        action="store_true",
        help="Drop local file paths from the markdown and JSON.",
    )
    report_parser.add_argument(
        "--bundle",
        action="store_true",
        help="Write bundle/ with the redacted citations, the signature, and a hash.",
    )
    report_parser.add_argument(
        "--verify",
        metavar="DIR",
        help="Check the hash in a bundle directory. A mismatch is reported.",
    )
    report_parser.set_defaults(func=_cmd_report)
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
    args = list(sys.argv[1:] if argv is None else argv)
    if args and not args[0].startswith("-") and args[0] not in _COMMANDS:
        return _cmd_bare(args)
    parser = build_parser()
    try:
        parsed = parser.parse_args(args)
    except SystemExit as exc:
        if exc.code in (None, 0):
            return 0
        return 3
    if not parsed.command:
        parser.print_help()
        return 0
    return parsed.func(parsed)


def _cmd_bare(argv: list[str]) -> int:
    if any(arg.startswith("-") for arg in argv):
        print("pass log files or a directory", file=sys.stderr)
        return 3
    try:
        recognized, skipped = _collect(argv)
    except RecognizeError as exc:
        print(exc, file=sys.stderr)
        return 3
    for name in skipped:
        print(f"skipped {name}", file=sys.stderr)
    if not recognized:
        where = argv[0] if len(argv) == 1 else "these paths"
        print(f"no recognized file in {where}", file=sys.stderr)
        return 3
    try:
        case_id = _ingest_recognized(recognized)
        classify_case(case_id)
        case = load_case(case_id)
        since, until = case_window(case, None, None)
        color, width = _terminal_view()
        text, _cited = explain(case, color=color, width=width, since=since, until=until)
        _markdown_path, _json_path, stated = write_report(case_id)
    except (InputError, CaseError, CatalogError, TimelineError, SignatureError, ReportError) as exc:
        print(exc, file=sys.stderr)
        return 3
    print(_cause_text(text, case_id), end="")
    return 0 if stated else 1


def _collect(argv: list[str]) -> tuple[list[Recognized], list[str]]:
    recognized: list[Recognized] = []
    skipped: list[str] = []
    for raw in argv:
        path = Path(raw).expanduser()
        if path.is_dir():
            files = sorted(child for child in path.iterdir() if child.is_file())
            if not files:
                raise RecognizeError(f"directory is empty: {raw}", kind="read")
            for child in files:
                try:
                    recognized.append(recognize(child))
                except RecognizeError as exc:
                    if exc.kind != "unknown":
                        raise
                    skipped.append(child.name)
            continue
        recognized.append(recognize(Path(raw)))
    return recognized, skipped


def _ingest_recognized(items: list[Recognized]) -> str:
    logs: list[tuple[str, ...]] = []
    alerts: list[str] = []
    metrics: list[str] = []
    for item in items:
        file_path = str(item.path)
        if item.source == "alert":
            alerts.append(file_path)
        elif item.source == "metrics":
            metrics.append(file_path)
        elif item.client is None:
            logs.append((item.source, file_path))
        else:
            logs.append((item.source, file_path, item.client))
    case_id = ingest(
        logs,
        alert=alerts[0] if alerts else None,
        metrics=metrics[0] if metrics else None,
    )
    for file_path in alerts[1:]:
        case_id = ingest([], case_id=case_id, alert=file_path)
    for file_path in metrics[1:]:
        case_id = ingest([], case_id=case_id, metrics=file_path)
    return case_id


def _cause_text(text: str, case_id: str) -> str:
    lines = text.splitlines()
    if lines and lines[0] == f"case {case_id}":
        lines = lines[1:]
    body = "\n".join(lines).strip("\n")
    tail = f"case {case_id}\nreport.md\n"
    if not body:
        return tail
    return f"{body}\n{tail}"


def _cmd_ingest(args: argparse.Namespace) -> int:
    inputs = _inputs(args)
    if not inputs and not args.alert and not args.metrics:
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
            alert=args.alert,
            metrics=args.metrics,
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
        since, until = case_window(case, args.since, args.until)
        text = render_timeline(case, since=since, until=until, color=color, width=width)
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
    metrics = getattr(args, "metrics", None)
    if args.case is None and not inputs and not metrics:
        print("why needs a case id or an input file", file=sys.stderr)
        return 3
    try:
        case_id = ingest(inputs, case_id=args.case, metrics=metrics) if inputs or metrics else args.case
        color, width = _terminal_view()
        classify_case(case_id)
        case = load_case(case_id)
        since, until = case_window(case, getattr(args, "since", None), getattr(args, "until", None))
        text, cited = explain(
            case,
            color=color,
            width=width,
            since=since,
            until=until,
        )
    except CaseNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 3
    except (CaseError, InputError, CatalogError, TimelineError) as exc:
        print(exc, file=sys.stderr)
        return 3
    print(text)
    return 0 if cited else 1


def _cmd_ask(args: argparse.Namespace) -> int:
    question = args.question.strip()
    if not question:
        print("ask needs a question", file=sys.stderr)
        return 3
    try:
        classify_case(args.case)
        case = load_case(args.case)
        color, width = _terminal_view()
        text, cited, payload = answer(case, question, color=color, width=width)
    except CaseNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 3
    except (CaseError, CatalogError, TimelineError) as exc:
        print(exc, file=sys.stderr)
        return 3
    if not args.remote or payload is None:
        print(text)
        return 0 if cited else 1
    if not case_is_redacted(case):
        print("remote call refused: redaction did not run", file=sys.stderr)
        return 3
    url = os.environ.get("CHAINLOG_AI_REMOTE_URL", "").strip()
    if not url:
        print("remote call refused: no remote model configured", file=sys.stderr)
        print(text)
        return 3
    print(REMOTE_NOTICE, flush=True)
    try:
        send_remote(url, payload)
    except RemoteError as exc:
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


def _cmd_report(args: argparse.Namespace) -> int:
    if args.verify:
        if args.case or args.bundle or args.no_paths:
            print("report --verify checks a bundle", file=sys.stderr)
            return 3
        try:
            matched = verify_bundle(Path(args.verify))
        except ReportError as exc:
            print(exc, file=sys.stderr)
            return 3
        print("match" if matched else "mismatch")
        return 0 if matched else 1
    if not args.case:
        print("report needs a case id", file=sys.stderr)
        return 3
    try:
        _markdown_path, _json_path, stated = write_report(
            args.case,
            paths=not args.no_paths,
            bundle=args.bundle,
        )
    except CaseNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 3
    except (CaseError, CatalogError, TimelineError, SignatureError, ReportError) as exc:
        print(exc, file=sys.stderr)
        return 3
    print(f"case {args.case}")
    print("report.md")
    print("report.json")
    if args.bundle:
        print("bundle")
    return 0 if stated else 1
