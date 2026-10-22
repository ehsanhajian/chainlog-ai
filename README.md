# chainlog-ai

chainlog-ai answers one question: **why did my infrastructure fail?**

Pass the logs you already have. The cause is printed, and `report.md` is written next to the case.

```bash
chainlog-ai examples/geth.log examples/beacon.log examples/validator.log examples/mev-boost.log examples/events.json
```

A directory works the same way. A file in that directory that cannot be recognized is named and skipped. An empty directory fails.

It reads execution clients, consensus clients, validators, builders, and Kubernetes. The case stays on this machine. A model is optional, and it only sees redacted excerpts.

chainlog-ai does not restart a client, edit a manifest, scrape a cluster, or sign with a validator key.

## Install

```bash
pip install chainlog-ai
```

Python 3.11 or newer. The package is published from a GitHub release to [PyPI](https://pypi.org/project/chainlog-ai/).

```bash
chainlog-ai --version
```

## What it prints

Exit 0 means a cause was stated. Exit 1 means the evidence is not enough, and the report is still written. Exit 3 is a usage error or a file that cannot be read.

The cause has three parts:

- **Critical.** A doppelganger or a slashable duty, when the logs contain one. This block comes first.
- **Trigger.** The earliest class that explains what followed. A missed proposal stays a symptom. A relay or builder timeout on that slot, or the slot before, is the trigger.
- **Contributors and symptoms.** Conditions that made it worse, such as disk pressure, then the later misses, restarts, and probe failures.

Each line cites a timeline row. When the lines do not support a cause, the command says so and names the missing source or the slot the window does not cover. It does not guess.

## Files

| Source | How a file is recognized | What it explains |
| --- | --- | --- |
| Execution | A Geth, Nethermind, Erigon, Besu, or Reth banner | Database, disk, engine API, peers, sync, JWT |
| Consensus | A Lighthouse, Prysm, Teku, Nimbus, or Lodestar banner | Slots, fork choice, checkpoints, engine calls |
| Validator | One of those clients on a line that says validator, or Web3Signer | Missed duties, doppelganger, signer timeouts |
| Builder | An MEV-boost banner | Relay and builder timeouts |
| Kubernetes | An events document, or a kubelet log path | OOM, eviction, probes, mounts, node pressure |
| Alert | An Alertmanager webhook, or one saved alert | The alert name and when it fired. Not the cause |
| Metrics | A Prometheus snapshot or range file | A threshold crossed in the failure window. Not the cause |

A file that matches nothing, or more than one source, stops the command and names the file. `--execution-client`, `--consensus-client`, `--validator-client`, and `--builder-client` name the client when the banner is missing.

`examples/` is a short incident. The files contain fake secrets. In a real log, a key, mnemonic, JWT, password, or API token is stored as `[redacted]`. A pubkey, a peer id, and a block hash stay.

## Commands

The command at the top is enough. These inspect a case that is already on the machine.

| Question | Command |
| --- | --- |
| Read logs into a local case | `ingest` |
| Which failure classes are present? | `classify` |
| What happened, in order? | `timeline` |
| Why did it fail? | `why` |
| Has this happened before? | `patterns` |
| What should I check next? | `ask` |
| Write the report | `report` |

`ingest` prints `case <id>`. The case is `~/.chainlog-ai/cases/<id>/case.json`, or under `CHAINLOG_AI_HOME` when that is set. The raw files stay at the paths you passed.

```bash
chainlog-ai ingest \
  --execution examples/geth.log \
  --consensus examples/beacon.log \
  --validator examples/validator.log \
  --builder examples/mev-boost.log \
  --kube examples/events.json

chainlog-ai why --case <id>
chainlog-ai timeline --case <id> --since 2026-10-08T06:13:40Z --until 2026-10-08T06:14:12Z
```

`classify` labels events from the rule catalog and prints the counts. A line with no matching rule stays unclassified. Every rule cites a source. Classify does not call a model. Each label has a severity (`info`, `warning`, or `critical`) and whether the client was still serving.

`timeline` prints the case in time order. Rows that share a timestamp stay in source order: cluster, execution, consensus, validator, builder, alert, metric. A row shows the slot, epoch, and block printed on the line. A missing slot is taken from a consensus line in the same few seconds and marked inferred. When those lines name different slots, the row leaves the slot off. The timeline names the last healthy slot and the first bad slot. A gap of more than five seconds between a client clock and a cluster event is called out. An execution engine error is paired with the consensus line that requested the payload. In a terminal, severity, source, a clock gap, the slot boundary, and `[redacted]` are colored. Piped output stays plain.

`why` takes a case id, or the same files as `ingest`. It states one trigger. When the evidence is not enough, it prints the timeline it has and does not state a cause.

`patterns --case <id>` lists earlier cases on this machine with the same class, client, version, and a similar order of classes. No earlier case means this is the first time. The version comes from a banner or a startup line, or `unknown` when the logs did not print one. A failure that begins after a version change names the previous version and the new one. ChainDiff decides whether that upgrade is safe.

```bash
chainlog-ai patterns --case <id> --export /tmp/case.signature.json
chainlog-ai patterns --signature /tmp/case.signature.json
```

The signature is class ids, client, version, and class order. It has no log lines and no host names.

`ask --case <id> "why did it fail"` answers from the cause and the runbook, and cites the rows it used. For each cited class it prints the next read-only check and the source of that check. A check without a source is left out. A question that cannot cite a row says the case does not contain that. `--remote` is off unless that invocation sets it. The command says a remote call is about to happen, then sends the question, the cited rows, and the runbook text to `CHAINLOG_AI_REMOTE_URL`. The call is refused when that variable is unset, or when redaction did not run.

`report --case <id>` writes `report.md` and `report.json` next to the case. Both include the cause, the last healthy slot, the first bad slot, the citations, and the pattern note. The markdown is short enough to paste into a chat. A doppelganger or a slashable duty is the first section when the cause includes one. `--no-paths` drops local file paths. `--bundle` writes `bundle/` with the redacted citations, the signature, and a hash of that content. `report --verify <bundle>` checks the hash and exits 1 on a mismatch.

## Alerts and metrics

```bash
chainlog-ai ingest --alert examples/alert.json --execution examples/geth.log
chainlog-ai why --execution examples/geth.log --consensus examples/beacon.log --metrics examples/metrics.json
```

`--alert` reads an Alertmanager webhook or one saved ValidatorPulse or Prometheus alert. The window defaults to one hour before the alert and fifteen minutes after. `--metrics` reads a Prometheus file you already exported. Disk usage, disk latency, memory, restart count, peer count, execution head lag, consensus head lag, and attestation effectiveness become contributors when they cross a threshold in that window. chainlog-ai does not scrape a cluster.

ValidatorPulse raises the alert. ChainDiff decides whether an upgrade is safe. chainlog-ai explains the failure.

## Logs in `examples/`

- Execution: `geth.log`, `nethermind.log`, `erigon.log`, `besu.log`, `reth.log`
- Consensus: `beacon.log` (Lighthouse), `prysm.log`, `teku.log`, `nimbus.log`, `lodestar.log`
- Validator: `validator.log`, `prysm-validator.log`, `teku-validator.log`, `nimbus-validator.log`, `lodestar-validator.log`, `web3signer.log`
- Builder: `mev-boost.log`
- Kubernetes: `events.json`, and `pods/ethereum_geth-0_poduid/geth/0.log` (the previous container) with `1.log` (the current one). The restart count is the file name.
- Alert: `alert.json`. Metrics: `metrics.json`

`--follow` prints the case id, then each new line, until the command is stopped. A later ingest of the same case continues from the stored offset. Replacing the file, so it has a new inode, starts again.

```bash
cp examples/follow.log /tmp/follow.log
chainlog-ai ingest --follow --case follow --execution /tmp/follow.log
```

## Development

Python 3.11 or newer. From a checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Work is tracked in the [milestones](https://github.com/ehsanhajian/chainlog-ai/milestones).

## Limits

chainlog-ai explains a failure from the evidence you provide. It does not fetch logs from a cluster API, apply a fix, or decide that a release is safe to run.
