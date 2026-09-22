# Foundry Model Catalog Watcher

Get alerted when a new model from a provider you care about becomes deployable in your Azure region.

A Python script that polls the Microsoft Foundry model catalog, diffs it against a stored
baseline, and reports anything new — filtered to the providers, regions, and deployment types
saved in your `.env` file.

```bash
python watch_foundry_models.py
```

## Why this exists

A common ask: *"tell us automatically when a new Anthropic or OpenAI model shows up in Foundry."*

There is no first-party push feed that does this. I went looking, and every obvious candidate
turns out to answer a different question:

| Candidate | Why it doesn't work |
|---|---|
| Foundry Notification Center | In-app only. Carries security alerts, policy/compliance events, and run completion — not catalog additions. |
| Azure Service Health alerts | Outages and planned maintenance for resources you already have. A model you haven't deployed isn't a resource. |
| Azure OpenAI webhooks | Inference and job events (`response.completed`, fine-tuning, batch). Not catalog events. |
| Azure Updates RSS | Platform and feature announcements, not catalog additions. Its `<category>` values are lifecycle stages ("In preview"), not provider names — so you can't even filter by provider. |

On that last one I tested rather than assumed: I pulled a 200-item window of the Azure Updates RSS
feed spanning roughly three and a half months, then checked it against models that demonstrably
landed in the catalog during that window. **None of them appeared in the feed.** It's a useful
secondary signal for feature news. It is not a model-catalog feed.

What does work is unglamorous: poll the catalog API on a schedule and diff it. That's all this is.

## What it does

- Polls the Azure Cognitive Services model catalog for one or more regions
- Loads saved provider and deployment-type filters from `.env`
- Deduplicates entries that appear more than once (see [Caveats](#caveats))
- Compares against a local baseline and reports only what's new
- Optionally POSTs to a webhook and/or sends an email
- Can emit new models as JSON for other tools

First run writes the baseline without sending alerts. Every run after that reports deltas.

## Requirements

- Python 3.10+ and the packages in `requirements.txt`
- Azure CLI, signed in (`az login`)
- **`Reader` on the subscription** — that's the whole permission surface. The catalog is a
  read-only ARM endpoint, so this never needs write access to anything.
- For the email option: a signed-in user context with delegated `Mail.Send` permission.
  The `/me/sendMail` endpoint does not support service-principal or managed-identity contexts;
  use the webhook option for those unattended runs.

## Quick start

```bash
python -m pip install -r requirements.txt
cp .env.example .env
az login
```

In PowerShell, use `Copy-Item .env.example .env` for the copy step. On systems where Python is
named `python3`, use that in place of `python`.

Edit `.env` to save the filters you want:

```dotenv
REGIONS=eastus2,swedencentral
PROVIDERS=OpenAI,Anthropic
DEPLOYMENT_TYPES=GlobalStandard,DataZoneStandard
```

Then run the same command each time:

```bash
python watch_foundry_models.py
```

The first run creates `model-watch-state.json`; later runs report newly matching models.
The default `.env` is loaded beside the script even when you run it from another directory.
For a different saved configuration:

```bash
python watch_foundry_models.py --env-file /path/to/production.env
```

Typical output:

```
Polling eastus2 ...
Found 107 distinct models across: eastus2

2 NEW model(s):

Provider   Name           Version  Lifecycle           Region   Skus
---------  -------------  -------  ------------------  -------  --------------
Anthropic  example-model  1        GenerallyAvailable  eastus2  GlobalStandard
OpenAI     another-model  1        Preview             eastus2  GlobalStandard
```

## Saved settings and command-line overrides

| `.env` setting | CLI override | Description |
|---|---|---|
| `REGIONS` | `--regions` | Comma-separated Azure regions. Defaults to `eastus2`. |
| `PROVIDERS` | `--providers` | Comma-separated providers. Blank means all providers. |
| `DEPLOYMENT_TYPES` | `--deployment-types` | Comma-separated deployment SKU names. Blank means all deployment types. |
| `STATE_PATH` | `--state-path` | Defaults to `model-watch-state.json`. Relative paths resolve beside the script. |
| `WEBHOOK_URL` | `--webhook-url` | POST a JSON alert to this URL. Blank disables webhooks. |
| `EMAIL_TO` | `--email-to` | Comma-separated Graph email recipients. Blank disables email. |
| `QUIET` | `--quiet` / `--no-quiet` | Suppress progress and the results table. Defaults to `false`. |

Command-line options override process environment variables, which override `.env`, which overrides
defaults. `.env` is optional; an explicitly supplied `--env-file` must exist. `.env` files are
ignored by Git because they can contain alert secrets; `.env.example` is the tracked template.
Values can be quoted and are read literally, without `${...}` expansion.

Lists are comma-separated, with surrounding spaces ignored. Provider and deployment-type matches
are case-insensitive. A model must match a selected provider **and** at least one selected deployment
type. For example, the settings above select either OpenAI or Anthropic models that offer either
`GlobalStandard` or `DataZoneStandard`. Models with no SKU information only pass when the deployment
filter is blank. Reported `Skus` includes all available types for the matching model.

Providers are matched on the model's `format` field, which is the publisher. Values observed in the
public catalog include: `OpenAI`, `Anthropic`, `Microsoft`, `DeepSeek`, `Mistral AI`, `Cohere`,
`Meta`, `xAI`, `Black Forest Labs`, `MoonshotAI`, `OpenAI-OSS`, `Alibaba`.

Deployment types match `model.skus[].name`, for example `Standard`, `GlobalStandard`,
`DataZoneStandard`, or `GlobalBatch`. Use the API's SKU names, rather than portal display labels
such as "Global Standard". To inspect providers and SKUs in your own region:

```bash
az cognitiveservices model list -l eastus2 --query "[].model.{Provider:format,Name:name,Skus:skus[].name}" -o json
```

One-off overrides leave the saved file unchanged:

```bash
python watch_foundry_models.py --providers "OpenAI,Mistral AI" --deployment-types GlobalStandard
```

## Alerting

Save optional alert destinations in `.env`:

```dotenv
WEBHOOK_URL=https://example.internal/alerts
EMAIL_TO=platform-team@example.com
QUIET=true
```

Leave either destination blank to disable it. The webhook uses the payload below; destinations
such as Teams or Slack may need a workflow or adapter that accepts this shape.

For machine-readable output, `--json` emits only the new-model array on stdout and sends progress
to stderr. It emits `[]` on the first run or when nothing changed. `--quiet` also suppresses progress:

```bash
python watch_foundry_models.py --json --quiet > new-models.json
```

Webhook payload shape:

```json
{
  "source": "foundry-model-watch",
  "detected": "2026-09-21T14:02:11.4821930-04:00",
  "newModels": [
    {
      "Provider": "Anthropic",
      "Name": "claude-fable-5-1",
      "Version": "1",
      "Lifecycle": "GenerallyAvailable",
      "Region": "eastus2",
      "CreatedAt": "2026-09-19T00:00:00Z",
      "Skus": "GlobalStandard,DataZoneStandard"
    }
  ]
}
```

## Running it unattended

Daily is the right cadence. Models don't land hourly, and the API is cheap but not free of
rate limits.

Any of these work:

- **Azure Automation runbook** on a schedule, with a managed identity holding `Reader`. Keep the
  state file in blob storage.
- **Timer-triggered Azure Function**, same idea.
- **GitHub Actions or Azure DevOps scheduled pipeline** with OIDC federated credentials. Commit the
  state file back to the repo and you get a free audit trail of exactly when each model appeared —
  which is genuinely useful evidence in a regulated environment.

A starter GitHub Actions workflow is in [`.github/workflows/watch.yml`](.github/workflows/watch.yml).
It installs Python dependencies and reads `.env.example`, with repository variables `REGIONS`,
`PROVIDERS`, and `DEPLOYMENT_TYPES` overriding the saved defaults. Its default regions are
`eastus2,swedencentral`; blank provider and deployment-type variables watch all. Set
`ALERT_WEBHOOK_URL` as a repository secret for webhook alerts. The workflow explicitly adds the
otherwise ignored baseline to Git so it persists between scheduled runs.

## How it works

One ARM endpoint does the work:

```
GET https://management.azure.com/subscriptions/{subscriptionId}
    /providers/Microsoft.CognitiveServices/locations/{region}/models
    ?api-version=2025-06-01
```

CLI equivalent, if you want to explore the shape by hand:

```bash
az cognitiveservices model list -l eastus2
```

The fields that matter:

| Field | What it gives you |
|---|---|
| `model.format` | The provider matched by `PROVIDERS`. |
| `model.name` / `model.version` | Identity. |
| `model.lifecycleStatus` | `Preview`, `GenerallyAvailable`, `Deprecating`, etc. |
| `model.systemData.createdAt` | When the entry appeared. A reliable recency signal. |
| `model.deprecation.inference` | **Retirement date.** See [Worth extending](#worth-extending). |
| `model.skus[].name` | Deployment types matched by `DEPLOYMENT_TYPES`. |

The script builds a key of `region|provider|name|version` for each entry, stores the set, and
compares on the next run. Existing PowerShell baselines, including UTF-8 files with a BOM, work
without conversion. State also records the selected deployment types.

The baseline is replaced atomically after polling and alerts succeed. Failed polling or alert
delivery returns a nonzero exit code and keeps the old baseline so the next run can retry.
If one alert destination succeeds and another fails, retrying can repeat the successful alert.

## Caveats

Two things will bite you if you write your own version:

1. **The catalog is per-region.** A model available in one region may not exist in another. Poll
   every region you deploy into, or you will get a confident answer about the wrong place.

2. **Entries are duplicated per account kind.** The same model is returned once under `OpenAI` and
   again under `AIServices`. Without deduplication your "new models" list roughly doubles and every
   entry looks like it arrived twice. The script dedupes on the composite key above and combines
   SKUs across duplicates before applying deployment-type filters.

The baseline contains the models matching the current filters. Expanding filters may report
already-existing models as new to your selection. Use a separate `STATE_PATH` for each independent
configuration. Delete a baseline to start fresh without alerts on that first run.

Deployment types control which models enter the baseline. Adding a SKU to a model already matching
the filter does not generate a separate alert; a previously excluded model that gains a matching
SKU does.

## Worth extending

Two ideas that cost almost nothing on top of what's here:

**Watch retirements, not just arrivals.** `model.deprecation.inference` is right there in the same
response. Retirement dates are more operationally urgent than new arrivals — a new model is an
opportunity, a retirement is a deadline — and they're much more commonly missed. Same poll, same
diff, higher value.

**Reframe the alert for regulated environments.** "A new model was added" is a newsletter. *"A model
we have not assessed is now deployable in our region"* is a control. Feed the output into a review
queue rather than a notification channel, and pair it with Azure Policy restricting which models can
actually be deployed — so a new catalog entry can't quietly become shadow usage before anyone has
looked at it.

## Validation

Run the offline tests with:

```bash
python -m unittest discover -s tests -v
```

They cover saved settings and overrides, provider and deployment-type filtering, pagination,
deduplication, multiple regions, first-run and delta detection, PowerShell baseline compatibility,
failure recovery, and alert payloads. Azure and alert endpoints are mocked; these tests do not
require an Azure login or send notifications.

## License

MIT. See [LICENSE](LICENSE).

## Disclaimer

This is a personal project, provided as-is. It is not an official Microsoft product, is not
supported by Microsoft, and carries no warranty. The Azure API surface it depends on may change.
Verify behaviour in your own environment before relying on it.
