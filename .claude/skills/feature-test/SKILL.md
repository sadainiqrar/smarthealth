---
name: feature-test
description: "The carrot for the Stop hook: infer a 'Behaviour under test' sentence from the working-set diff, author a validated tests/cases/<id>.yaml for the current feature (which alone clears the Stop block), and optionally run just that case and report the result."
---

# feature-test — author (and optionally run) a case for the current feature

Usually invoked right after the **`Stop` hook** blocked "done" because feature code changed under a core path with no validated case. This skill makes complying easier than fighting the block.

**This skill orchestrates; it does not reimplement.** It delegates to:
- **smarthealth-testcase** — natural language to a validated `tests/cases/<id>.yaml`
- **test-stack** — boots infrastructure, if the case's tier needs it
- **ai-judge** — semantic verdict, for AI cases only

```
/feature-test [--run] [--tier <tier>]
```

## 1. Infer the "Behaviour under test" sentence

```bash
git diff
```

From that diff, propose exactly ONE line:

> **Behaviour under test:** &lt;what now holds / what a user can now do&gt;

Show it, then accept-on-empty:

> Press Enter to accept, or type a replacement sentence:

Empty input accepts as-is; any typed text replaces it verbatim. Do not proceed until it is confirmed — this sentence is the seed for step 2.

## 2. Author the case — delegate to smarthealth-testcase

Invoke **smarthealth-testcase** with the confirmed sentence. It writes the file, self-validates via the routing dry-run, and hands back.

**State this clearly to the developer:**

> A validated `tests/cases/<id>.yaml` is what the `Stop` hook enforces. This step alone clears the block. Running it is optional — you may stop here.

Confirm before claiming the block is cleared: `python -m tests.runner.route_check` must exit 0 and list the new id in a runnable bucket.

## 3. Optionally run it — opt-in only

Enter this step only if `--run` was passed or the developer says yes to:

> Run this case now? [y/N]

Default is No. On yes:

1. If the case's tier is `integration` or `journey`, delegate to **test-stack** to boot the infrastructure.
2. Run **only this case**, never the whole suite:
   `python -m pytest tests/tiers/test_catalog.py -k <id> -v`
   (or, for an `impl`-backed case, the path in its `impl:` field)
3. For an AI case, delegate to **ai-judge** for the semantic verdict.
4. Report every signal: the structural result, and — for AI cases — the judge verdict alongside it. When a judge verdict disagrees with a soft structural failure, present the judge as the more trustworthy signal. This run is a proof, not a gate; nothing here changes what cleared the Stop hook in step 2.

## Escape hatch (mention it, do not run it for them)

```bash
E2E_WAIVE="<reason>"    # skips the Stop block; the reason is logged to tests/waivers.log
```

Every waiver leaves a tracked record — an audited escape, not a silent one. Point developers to it only when authoring genuinely does not fit.
