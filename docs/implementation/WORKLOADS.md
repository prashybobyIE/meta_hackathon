# Workload manifests

Status: first version, built November 1. Runs on the fixture provider and the loopback simulator. Inventory reorder has one completed live run on the Meta API, which is not a reliability measure. It is a template for small bounded-module applications, not a general application generator: arbitrary code execution stays out of scope (C03, C11).

## Run

```sh
python -m agent_marketplace.demo                                  # expense report, the default
python -m agent_marketplace.demo --workload agent_marketplace/workloads/inventory_reorder/manifest.json
python -m agent_marketplace.demo --provider simulated-meta --workload agent_marketplace/workloads/inventory_reorder/manifest.json
python -m agent_marketplace.workload PATH --public-out public.json  # validate, export worker contracts
```

`--protected-url` is refused with `--workload`. The protected step categorises private expense rows, so it belongs to the expense report only and is separate from the manifest.

## Manifest format

`schema` is `agent-marketplace-workload-v1`. Every object has an exact field list, and an unknown or missing field is an error naming its location. Duplicate JSON keys and NaN are refused.

| Field | Meaning |
| --- | --- |
| `id`, `title` | Workload identity |
| `sensitivity` | Must be `public`. Anything else is refused |
| `planning_brief` | System text for the hosted planner |
| `budget_credits` | Job reward limit. At least the highest price times the task count |
| `tasks.<kind>` | One bounded module; see below |
| `integration` | `input`, `input_filename` (written to the workspace), `result` expression, `expect`. `result` must call every task |
| `report` | `renderer` (`expense-summary` or `json-table`) and `title` |

Task fields: `contract_id`, `filename`, `function`, `description` (dispatched and sent to the planner), `spec` (the module prompt), `depends_on`, optional `consumes`, `complexity` (`low` 12, `medium` 16, `high` 20 credits maximum), `output` (exact-type result schema), `public_cases`, `hidden_cases`, `fixture`, optional `simulated_first_attempt`.

A case is `{"input": X, "expect": Y}` or `{"input": X, "raises": true}`. Public cases may be quoted in repair feedback. Hidden cases never are.

Inputs may contain two forms. `{"$call": "task", "arg": X}` runs that task's accepted artifact on `X`; `{"$input": true}` is the integration input and is available only in the integration result. Other keys beginning with `$` are refused.

## Dependencies

`depends_on` orders the work: a task is advertised only after its dependencies are accepted. `consumes`, a subset of `depends_on`, delivers the artifact as well.

For a consuming task:

1. The requester puts the accepted source, digest and identity in the dispatch's `dependencies`. `dispatch` returns 409 unless the dependency is accepted, so it holds even if an assignment somehow came first.
2. The worker checks that the delivery is exactly what its own contract declares, and checks the digest, before generating. The first prompt includes the source so the model can see the output format.
3. At acceptance and integration, the requester runs the accepted artifact in the restricted interpreter and passes its output as data in the module's one argument.

Modules stay one-argument functions, and `restricted.py` is unchanged. No builtin was added.

A `$call` in an acceptance case must use an input that the dependency returned on in its own acceptance cases, and a public case may use only the dependency's public inputs. So the dependency's output on that input was already checked, a failure lands on the module under test, and quoting a resolved public input cannot reveal a hidden one. Consequently a `$call` argument must be literal, so a consumed task cannot itself take a consumed input in those cases. Integration has no such limit and should use new input.

## What stops hidden cases reaching a prompt

The main control is structural. `Workload.public_contracts()` is an allowlist of `filename`, `function`, `spec`, `fixture_source` and `consumes`, and workers receive only that. A worker builds prompts from its own registered contracts, so a requester cannot push spec text. A worker adds contracts through `--contracts-file`, which refuses a full manifest because of its unknown fields. The planner receives `planning_brief`, `function` and `description`.

Validation adds checks on top:

- A hidden case's input strings, input JSON and expected JSON may not appear in any public field, with whitespace changes tolerated. Strings under 8 characters are skipped because they match by chance.
- A hidden case may not repeat a public input of the same task.
- Credential-shaped text, and fields named like `password` or `api_key`, are refused in public data.

These checks catch literal copies. They do not prove a spec reveals nothing about a hidden case, and they cannot recognise private data in general.

"Hidden" means withheld from workers and prompts. A person with the repository can read the manifests, as they could read `reference.py` before. A public repository publishes them, so a real deployment would keep the manifest out of what it distributes.

## Trust limits

- Accepted dependency source is text from another worker's model, and it enters the next worker's prompt. The prompt marks it as data, but that does not prevent prompt injection. The effect is bounded because the result still has to pass independent acceptance, and the budget guard caps spending.
- A requester can place up to 32 KiB of arbitrary text in that section for a contract that declares `consumes`, because the worker can verify only the digest. A contract with no `consumes` receives none.
- Repair prompts omit dependency source, keeping to the literal wording of C23. The spec therefore has to describe the dependency's output on its own.
- The fixture provider validates a delivered artifact but ignores its content, so a fixture run proves delivery and data flow and says nothing about model behaviour.
- Everything here is public work on ordinary workers. Nothing in a manifest grants host confidentiality.

## Adding a workload

Create a directory with `manifest.json` and `fixtures/`, validate it, and run it through `--workload`. Check first that a module fits the interpreter: `str.splitlines`, `set` mutation, `sorted(key=...)`, helper functions and module constants are not available. Keep the spec self-sufficient, since hidden cases should test behaviour it already states.
