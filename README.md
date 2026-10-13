# chainlog-ai

chainlog-ai answers one question: **why did my infrastructure fail?**

It reads the logs an operator already has — execution clients, consensus clients, validators, and Kubernetes — and returns a cause that cites those lines. The case stays on the machine. A model is optional, and it only sees redacted excerpts.

chainlog-ai does not restart a client, edit a manifest, scrape a cluster, or sign with a validator key.

Work is tracked in [milestones](https://github.com/ehsanhajian/chainlog-ai/milestones). `ingest` writes a case on this machine. `classify --case` labels events from the rule catalog and prints the counts. A line with no matching rule stays in the case, unclassified. Every rule cites a source. Classify does not call a model. `why --case` accepts the id that `ingest` prints. Stating a cause comes later.

## Command

| Question | Command |
| --- | --- |
| Read logs into a local case | `ingest` |
| Which failure classes are present? | `classify` |
| What happened, in order? | `timeline` |
| Why did it fail? | `why` |
| Has this happened before? | `patterns` |
| What should I check next? | `ask` |
| Write the report | `report` |

`why` can take a case id, or the same files as `ingest`. It classifies, builds the timeline, and states a cause.

`examples/` is a short incident you can ingest without a node. The files include fake secrets. Ingest prints `case <id>`. The excerpts are in `~/.chainlog-ai/cases/<id>/case.json` (or under `CHAINLOG_AI_HOME`). A key, mnemonic, JWT, password, or API token is marked `[redacted]`. The pubkey, peer id, and block hash stay. The same flags take the operator's own files.

`examples/geth.log` starts with a Geth banner, so the execution events are recorded as `geth` with the time each line prints. `examples/nethermind.log`, `examples/erigon.log`, `examples/besu.log`, and `examples/reth.log` are the other execution clients. `--execution-client` names the client when that banner is not in the file. Those logs include database, disk, engine, peer, sync, and JWT failures. `classify` names them from the execution rules, and each rule cites a public source.

`examples/beacon.log` is Lighthouse. `examples/prysm.log`, `examples/teku.log`, `examples/nimbus.log`, and `examples/lodestar.log` are the other consensus clients. A line that prints a slot, epoch, or block keeps those fields. `--consensus-client` names the client when the banner is absent. Those logs include missed duties, checkpoint sync, weak subjectivity, fork choice, reorgs, and a clock that is not in sync.

`examples/validator.log` is the Lighthouse validator client. `examples/prysm-validator.log`, `examples/teku-validator.log`, `examples/nimbus-validator.log`, and `examples/lodestar-validator.log` are the other validator clients. `examples/web3signer.log` stays on the validator source, including a signing failure, a signer timeout, a signer refusal, and doppelganger detection. `examples/mev-boost.log` stays on the builder source, including a relay timeout and a builder timeout. `--validator-client` and `--builder-client` name the client when the banner is absent. `classify` names these from the consensus and validator rules, and each rule cites a public source.

`examples/events.json` is a Kubernetes events document. Each event keeps its pod, container, namespace, and reason, including OOMKilled, Killing, BackOff, Unhealthy, Evicted, FailedMount, node memory and disk pressure, node not ready, and FailedScheduling for insufficient CPU. `classify` names those causes. Killing means the process was stopped from outside the client. `examples/pods/ethereum_geth-0_poduid/geth/0.log` is the previous container and `1.log` is the current one. The restart count comes from the file name. The previous container is stored as its own source. A Geth line inside the pod is recorded as `geth`, and the pod name stays with it. `--namespace`, `--pod`, `--container`, `--restart`, and `--previous` name the container when the path is not a kubelet log path.

`examples/follow.log` is a short local log. `--follow` prints the case id, then each new line, and keeps reading until the command is stopped. A later ingest of the same case continues from the stored offset for each file. Replacing the file, so it has a new inode, starts again, and the case records the rotation.

```bash
chainlog-ai ingest \
  --execution examples/geth.log \
  --consensus examples/beacon.log \
  --validator examples/validator.log \
  --builder examples/mev-boost.log \
  --kube examples/events.json

chainlog-ai ingest --execution examples/reth.log
chainlog-ai ingest --execution examples/geth.log --execution-client geth
chainlog-ai ingest --consensus examples/prysm.log
chainlog-ai ingest --consensus examples/beacon.log --consensus-client lighthouse
chainlog-ai ingest --validator examples/validator.log
chainlog-ai ingest --validator examples/web3signer.log
chainlog-ai ingest --builder examples/mev-boost.log
chainlog-ai ingest --kube examples/events.json
chainlog-ai ingest \
  --kube examples/pods/ethereum_geth-0_poduid/geth/0.log \
  --kube examples/pods/ethereum_geth-0_poduid/geth/1.log
cp examples/follow.log /tmp/follow.log
chainlog-ai ingest --follow --case follow --execution /tmp/follow.log
chainlog-ai ingest --case incident \
  --execution examples/geth.log \
  --consensus examples/beacon.log \
  --validator examples/validator.log \
  --builder examples/mev-boost.log \
  --kube examples/events.json
chainlog-ai classify --case incident
chainlog-ai ingest --case execution-rules --execution examples/nethermind.log
chainlog-ai classify --case execution-rules
chainlog-ai ingest --case consensus-rules \
  --consensus examples/beacon.log \
  --validator examples/validator.log \
  --builder examples/mev-boost.log
chainlog-ai classify --case consensus-rules
chainlog-ai ingest --case kube-rules --kube examples/events.json
chainlog-ai classify --case kube-rules
chainlog-ai ingest --case 20261008T061200Z --execution examples/geth.log
chainlog-ai why --case 20261008T061200Z
chainlog-ai ask --case 20261008T061200Z "what happened in the slot before the miss?"
chainlog-ai report --case 20261008T061200Z
```

Exit 0 means a cause is stated and every claim cites a timeline row. Exit 1 means the evidence is not enough. Exit 3 is a usage error or an unreadable input.

## What a cause is allowed to say

A cause cites timeline rows: time, source, and the redacted line. The report separates three things:

- **Trigger** — the earliest cited class that explains what followed.
- **Contributors** — cited conditions that made it worse, such as disk pressure before a database error.
- **Symptoms** — later misses, restarts, or probe failures that the trigger explains.

A symptom is never printed as the trigger. A class is applied only when a catalog rule matches, and every rule has a source. An unmatched line stays unclassified. When the lines do not support a cause, `why` says so and names what is missing.

A doppelganger detection or a slashable duty is printed first, even when an earlier warning exists in the window.

## Sources

| Source | Clients and objects | What it explains |
| --- | --- | --- |
| Execution | Geth, Nethermind, Erigon, Besu, Reth | Sync, database, engine API, peers, disk, JWT |
| Consensus | Lighthouse, Prysm, Teku, Nimbus, Lodestar | Slots, fork choice, checkpoints, engine calls |
| Validator | Validator clients, Web3Signer | Missed duties, doppelganger, signer timeouts |
| Builder | MEV-boost | Relay and builder timeouts on a proposal |
| Kubernetes | Container logs, cluster events | OOM, eviction, probes, volume mounts, node pressure |

Slot, epoch, and block are taken from the line that prints them. A slot filled in from a nearby consensus line is marked inferred. When two consensus lines disagree, the slot stays blank.

## Around the failure

An alert can open the case. A ValidatorPulse or Prometheus alert sets the window: one hour before it fired, and fifteen minutes after. A metric snapshot for that window can support a contributor: disk, memory, restarts, peer count, head lag, attestation effectiveness.

chainlog-ai reads the snapshot the operator exports. ValidatorPulse remains the monitor that raises the alert. ChainDiff remains the tool that decides whether a client upgrade is safe. chainlog-ai records the version change and stops there.

A pattern exported for another host contains class ids, client, version, and the order of classes. It contains no log lines and no host names.

## Where a case lives

Cases are written under `~/.chainlog-ai`, or `CHAINLOG_AI_HOME` when that is set. The case stores redacted excerpts and citations. The raw files stay at the paths the operator passed.

Redaction removes private keys, mnemonics, keystore passwords, JWT secrets, and API tokens before the case is written and before any model sees text. Validator pubkeys and peer ids stay, because the timeline joins on them. A removed span is marked `[redacted]`.

`ask` answers from the case. With no model configured, it answers from the cause and the sourced runbook. `--remote` is off unless that invocation sets it, and the command prints that a remote call is about to happen. If redaction did not run, the remote call is refused.

## Report

`report` writes markdown and JSON: cause, trigger, contributors, symptoms, the last healthy slot, the first bad slot, citations, and the pattern note. `--no-paths` drops local file paths. `--bundle` writes the redacted citations, the signature, and a hash. `--verify` checks that hash later.

## Install

```bash
pip install chainlog-ai
```

The package is published from a GitHub release. Python 3.11 or newer. The command is `chainlog-ai`.

```bash
chainlog-ai --version
```

## Development

Python 3.11 or newer. From a checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

That install includes pytest and puts `chainlog-ai` on the path from this tree.

```bash
pytest
chainlog-ai --version
```

## Limits

chainlog-ai explains a failure from evidence the operator provides. It does not fetch logs from a cluster API, apply a fix, or decide that a release is safe to run.
