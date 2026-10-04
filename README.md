<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/banner-dark.svg">
  <img src="assets/banner-light.svg" alt="A wide beam passing through four apertures labelled model tiering, output filtering, policy blocks and session hygiene. The beam narrows at each one while a bright core runs through all of them unchanged.">
</picture>

# claude-code-token-optimization

A configuration that reduces token consumption in Claude Code without moving reasoning to weaker models, plus the scripts to check whether it worked on your own machine.

*Above: the idea, not a result. Each layer narrows what passes through it. The core running down the middle is the reasoning, and no layer touches it.*

Four layers, installed independently. Each one acts on a different part of the bill:

| Layer | Acts on | Status |
|---|---|---|
| Model tiering | which model does which work | measured, adopted, cost per turn down |
| Output filtering | how much tool output enters context | self-reported by the tool, not independently measured |
| Policy blocks | when to delegate, when to write code, how much to report | measured indirectly through adoption and rework proxies |
| Session hygiene | how long a session grows before it is reset | plausible, not demonstrated by this data |

What was measured, for the one operator this data comes from: layer 1 was adopted in practice and displaced the built-in search agent, taking 70% of subagent dispatches where the prior state was 2%. Cost per turn fell 22.6%, and 23.5% after adjusting for how deep in a session those turns sat, with every depth band falling between 11% and 37%. Measured rework improved rather than degraded, which is the part that matters, since a cost reduction bought with more mistakes is not a saving.

The second finding is about how the first one is measured, and it is the more transferable of the two. Normalized by output tokens instead of by turns, the same data reports cost going **up** 4.1%. Both numbers are correct. Output per turn fell 26%, because this setup reduces output tokens on purpose: a policy block instructs shorter write-ups, and work moved from generating text to running tools. Dividing by a denominator the change is designed to shrink reports a loss where there was a gain. If you measure your own setup, this is the trap to avoid.

Full method, reference results, and the limitations that qualify them are in [claude-code-measure-efficiency](https://github.com/makinggainz/claude-code-measure-efficiency). The measured figures, and why two of them disagree, are in [What the numbers showed](#what-the-numbers-showed) at the end.

## Install

Everything is optional and independent. Nothing here is a package, and there is no state beyond the files you copy.

```bash
git clone https://github.com/makinggainz/claude-code-token-optimization
cd claude-code-token-optimization

cp agents/*.md ~/.claude/agents/          # layer 1
cat CLAUDE.md.example >> ~/.claude/CLAUDE.md   # layer 3
```

For layer 2, install [rtk](https://github.com/rtk-ai/rtk) and let it wire its own hook:

```bash
rtk init --hook-only
```

Then merge the relevant keys from [`settings.json.example`](settings.json.example) into `~/.claude/settings.json`.

**Measure before you install.** The point of this repository is that the claim is checkable. Record a baseline first:

```bash
python3 verify/usage_report.py --days 30
```

## Layer 1: model tiering

Seven subagent definitions in [`agents/`](agents/), each pinned to a model tier.

The principle is that work whose failure mode is cheap and immediately visible runs on inexpensive models, and work whose failure mode is expensive stays on capable ones. Reasoning is never moved down a tier to save money. A wrong architectural decision costs far more than the tokens saved by making it on a cheap model.

| Agent | Model | Purpose |
|---|---|---|
| `deep-analyst` | top tier | Escalation only. Problems that already resisted a serious attempt. |
| `architect` | high tier | Design decisions and difficult debugging. Read-only, returns a plan. On a main session already at this tier it adds a separate context, not a stronger model. |
| `implementer` | mid tier | Executes an already-decided specification. |
| `test-runner` | mid tier | Runs verbose commands, reports only failures. Never fixes. |
| `scout` | low tier | Read-only code and file discovery. |
| `web-researcher` | low tier | Documentation and error lookup, returns a distilled answer. |
| `second-opinion` | low tier | Relays a verdict from a non-Claude model via a local CLI. |

Two design notes the adoption data supports.

`scout` exists because the built-in Explore agent inherits the main conversation's model, so unguided searching runs at the main model's rate. A cheap-tier alternative with an explicit description displaced it: Explore dispatches fell from 43 to 8 after installation.

The set is deliberately small. Large agent libraries create overlapping descriptions, and the routing model selects by matching a task against those descriptions. These seven were selected in 70% of dispatches where the prior state was 2%. Curation appears to matter more than coverage.

There is a second effect that is easy to miss. A subagent runs in its own context, so its tokens never enter the main conversation and never become part of the transcript that is re-read on every subsequent turn. Delegating a verbose search is therefore worth more than the tier difference alone suggests.

## Layer 2: output filtering at the tool boundary

Raw command output is one of the largest uncontrolled inputs to a session. A directory listing, a process table, a linter run, or a fetched HTML page can each be tens of thousands of tokens, most of it padding, repetition, and formatting.

[rtk](https://github.com/rtk-ai/rtk) is a CLI proxy that filters and compacts that output before it reaches the model. A `PreToolUse` hook rewrites `Bash` commands to their proxied equivalent transparently, so no change in habit is required. It is a separate Apache-2.0 project, not vendored here; install it from upstream.

Reported reduction on proxied output for this operator, across roughly 20,000 commands, was 64.8%. Where that saving comes from is more useful than the headline:

| Command | Average reduction | Share of total saving |
|---|---|---|
| `grep` | 32.8% | 44% |
| `read` | 20.8% | 11% |
| `ls` | 60.6% | 10% |
| `ps aux` | 99.0% | 10% |

The pathological cases compress the most, and contribute the least. Almost half of the total saving came from `grep`, which has the *lowest* per-call reduction in the table and simply runs constantly. Frequency dominates compressibility. If you are choosing where to filter, filter what you run most, not what looks worst.

Two honest caveats. These percentages are the tool's own accounting, comparing raw output to filtered output; they are not measured through Claude Code transcripts, and no independent verification is offered here. And filtering is lossy by design, so a filtered output can omit the one line that mattered; treat `rtk proxy <cmd>` as the escape hatch when a result looks wrong.

This layer compounds with the mechanism behind layer 4. Output that never enters the transcript is not merely cheap once; it is absent from every future cache read in that session.

## Layer 3: policy blocks

[`CLAUDE.md.example`](CLAUDE.md.example) contains three blocks, loaded once per session at a combined cost of roughly 200 tokens.

**Delegation policy.** Small work is done inline, because a delegation costs a fresh context, a re-exploration, and a report that must then be read. That overhead exceeds the saving on short tasks. Decisions are never delegated, and difficulty escalates upward rather than settling for a cheaper tier's answer.

**Reuse ladder.** Checks whether code needs to exist, whether the codebase already provides it, and whether a dependency covers it, before new code is written. Correctness, boundary validation, security, and accessibility are explicitly excluded from reduction.

**Report calibration.** Matches write-up length to task significance. Failures, caveats, and the line between verified and assumed are excluded from trimming.

The reason these are worth their tokens is that they act on behavior every turn, for a one-time cost.

## Layer 4: session hygiene

This layer rests on a mechanism rather than on a measured result, and the distinction matters.

The mechanism is real and is not in dispute. Every turn re-reads the transcript accumulated so far, so the cost of a session grows with roughly the square of its length: more turns, each re-reading more history. Cache reads were about 60% of cost-equivalent in both periods measured, which is what makes the mechanism worth caring about at all.

What this data does **not** show is that resetting sessions more often would have helped the operator it came from. An earlier version of this repository claimed session length grew 60% over the measurement period and that this drove the cost increase. Correct bucketing showed session length fell about 7%, and the claim was withdrawn. Cache read per turn still rose 14%, and the cause is unidentified: fixed per-turn overhead was flat to within 1%, so it is not instruction or tool-definition bloat either.

Session length remains an operator behavior rather than a property of any configuration. Nothing in layers 1 through 3 lengthens or shortens a session. The advice below is sound because the mechanism is sound, and it is free to follow, but treat it as reasoning rather than as a demonstrated saving:

- **Start a new session when the task changes.** A finished task's transcript is pure cost on every subsequent turn. `/clear` is free and instant.
- **Do not switch model or effort mid-session.** Either invalidates the prompt cache, forcing a full re-read at the uncached rate. Choose at session start. A subagent's model never touches the parent's cache, which is another reason to delegate rather than switch.
- **Watch where the cost actually concentrates.** In one 40-day sample, the top 10% of sessions by cost held 47% of total cost-equivalent, and sessions above the 90th percentile in length held 50% of all cache read cost. A small number of long sessions carry a large share of the bill, which is a fact about distribution rather than evidence that shortening them would have saved money.

`verify/session_growth.py` reports exactly this, per session, so runaway sessions are visible rather than inferred.

No hook is shipped for this. A threshold warning is straightforward to build and has not been validated, so it is not offered as advice.

## Verifying it on your own machine

The scripts in [`verify/`](verify/) are the reason to take any of the above seriously, or to reject it. They read the transcripts Claude Code already writes to `~/.claude/projects/`, transmit nothing, and print aggregate counts only. No conversation content is written to output. Python 3.8 or later, no dependencies.

```bash
python3 verify/usage_report.py --days 30           # baseline, before installing
python3 verify/usage_report.py --since YYYY-MM-DD  # compare two periods
python3 verify/decompose.py YYYY-MM-DD             # cost per unit of work, four components
python3 verify/friction.py YYYY-MM-DD              # rework proxies
python3 verify/session_growth.py                   # per-session context growth
python3 verify/per_turn.py YYYY-MM-DD              # cost per turn, adjusted for session depth
```

Pass the date the configuration change took effect.

Three values indicate whether the configuration is being used at all: the share of dispatches going to these agents, the low-tier share of tokens, and the Explore dispatch count. If the first two do not rise, the configuration is installed but inactive, and the correct response is to remove it rather than to assume it is helping.

Read `decompose.py` output before concluding anything. A total that moved the wrong way while its components moved the right way is the normal case, not an anomaly, and the components are where the explanation is.

These scripts are copies of the tooling in [claude-code-measure-efficiency](https://github.com/makinggainz/claude-code-measure-efficiency), included here so this repository is self-sufficient. That repository is canonical: it carries the method, the full reference results, and the limitations that qualify them. If the two ever disagree, it is correct.

## Cost-equivalent is not a bill

The scripts apply published API list rates to observed token counts. On a subscription the amount paid is fixed regardless. Cost-equivalent is a unit for comparing one period against another, and it is meaningless as an absolute. Rates are constants at the top of each script and need updating when list prices change. Each model has its own input, output and cache read rate, and cache writes are priced by TTL (1.25x input for 5 minutes, 2x for 1 hour). The figures under [What the numbers showed](#what-the-numbers-showed) came from an earlier version with flat multipliers and have not been recomputed.

## Evaluated and not adopted

Recorded because a list of what was rejected is more informative than a list of what was kept.

- **Open verdict: knowledge-graph indexing of a repository.** One successful test, on a small codebase, where the extracted call chain verified correct against source with no fabricated edges. One test is not a verdict. The document-extraction layer produced some dangling edges while the syntax-tree layer produced none, and an unfiltered scan will attempt a vision call per image unless images are excluded first. Not recommended either way yet.
- **Rejected: proxy-level prompt compression.** Compressing requests in transit trades output quality for tokens, and overlaps with layer 2 while being far harder to reason about when something goes wrong.
- **Rejected: terse-speak output compression.** Instructing the model to drop articles and function words does reduce output tokens. Output tokens were the smallest component in the decomposition, and readability is the product.

## What the numbers showed

Two figures from the same dataset point in opposite directions, and the disagreement is the most useful thing here.

| Measure | Baseline | After | Change |
|---|---|---|---|
| Cost per turn, adjusted for session depth | 0.3073 | 0.2352 | **−23.5%** |
| Cost per million output tokens | 220.6 | 229.7 | **+4.1%** |

Cost per turn fell in every depth band measured, from −11% to −37%, so the reduction is not an artifact of which kinds of turns happened to occur. Output tokens per turn fell 26% over the same period.

That last number is the explanation. Output tokens were the original denominator, and this setup reduces them by design: layer 3 instructs shorter write-ups, and layers 1 and 2 move work from generating text toward running tools and reading filtered output. A denominator that responds to the treatment stops being a measure of the treatment. The first published version of this repository used it and concluded no saving had been demonstrated. That conclusion is withdrawn.

Neither denominator is neutral. A turn is not a fixed quantity of work either, and if turns became denser then the per-turn figure understates the gain. Tool calls per turn rose 7%, so on this dataset the per-turn measure is the conservative one.

**What is still open.** Context per turn at matched depth is about 8% higher than baseline. Sessions did not get longer, fixed startup context did not grow, images went down 14%, and every visible category of content entering context per turn fell. About 180 tokens per turn of the increase is context that does not appear in the transcript at all, which is consistent with tool schemas loaded on demand mid-session or with injected reminders. Neither is recorded, so this data cannot settle it. It is logged as unexplained rather than assigned to the nearest plausible cause.

Full method and the correction log are in [claude-code-measure-efficiency](https://github.com/makinggainz/claude-code-measure-efficiency#correction).

## Limitations

1. One operator, one machine, one workload. The reference results indicate direction, not magnitude, and are not generalizable without replication.
2. The periods compared were not a controlled experiment. Workload composition differed between them, and part of the measured change is attributable to that rather than to the configuration.
3. Layer 2's reduction figures are the tool's own self-report, on a different basis from every other number here.
4. The verification scripts parse an undocumented internal transcript format. Format changes will break them.
5. Output tokens are used as the proxy for work produced. A refactor and a long explanation are not equivalent work at equal token counts.
6. Friction metrics measure friction, not correctness. Nothing here measures whether the output was right.
7. The reference figures were corrected on 2026-08-14, twice: two period-assignment defects, and a denominator that the configuration itself reduces. Numbers published before that date, including a claim that session length grew 60% and a conclusion that no saving was demonstrated, should not be relied on.
8. Layer 4 is reasoning from a mechanism, not a measured result. It is free to follow and the mechanism is sound, but this data does not demonstrate that it would have saved this operator money.

## License

MIT. See [LICENSE](LICENSE). `rtk` is a separate project under its own license and is linked, not included.
