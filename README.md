# Foundry Model Catalog Watcher

Get alerted when a new model from a provider you care about becomes deployable in your Azure region.

A single PowerShell script that polls the Microsoft Foundry model catalog, diffs it against a stored
baseline, and reports anything new — filtered to the providers and regions you actually use.

```powershell
.\Watch-FoundryModels.ps1 -Regions eastus2,swedencentral -Providers OpenAI,Anthropic
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
- Filters to the providers you specify
- Deduplicates entries that appear more than once (see [Caveats](#caveats))
- Compares against a local baseline and reports only what's new
- Optionally POSTs to a webhook and/or sends an email
- Emits new models to the pipeline so you can compose it into something else

First run writes the baseline and stays silent. Every run after that reports deltas.

## Requirements

- PowerShell 5.1 or PowerShell 7+
- Azure CLI, signed in (`az login`)
- **`Reader` on the subscription** — that's the whole permission surface. The catalog is a
  read-only ARM endpoint, so this never needs write access to anything.
- For the email option: a token context with `Mail.Send`

## Quick start

```powershell
# First run — writes a baseline, reports nothing
.\Watch-FoundryModels.ps1 -Regions eastus2 -Providers OpenAI,Anthropic

# Later runs — reports only what's new since the baseline
.\Watch-FoundryModels.ps1 -Regions eastus2 -Providers OpenAI,Anthropic
```

Typical output:

```
Polling eastus2 ...
Found 107 distinct models across: eastus2

2 NEW model(s):

Provider  Name                  Version  Lifecycle        Region   Added
--------  ----                  -------  ---------        ------   -----
Anthropic claude-fable-5-1      1        GenerallyAvailable eastus2 2026-09-19
OpenAI    gpt-6-astra           2026-09-15 Preview        eastus2  2026-09-18
```

## Parameters

| Parameter | Description |
|---|---|
| `-Regions` | Regions to poll. Defaults to `eastus2`. The catalog is **per-region** — poll every region you deploy into. |
| `-Providers` | Providers to watch. Omit to watch all. |
| `-StatePath` | Where the baseline lives. Defaults to `model-watch-state.json` beside the script. |
| `-WebhookUrl` | POST a JSON payload when new models are found. Works with Teams, Slack, ServiceNow, or any internal endpoint. |
| `-EmailTo` | Send an HTML summary email via Microsoft Graph. |
| `-Quiet` | Suppress console output. Use for scheduled runs. |

Providers are matched on the model's `format` field, which is the publisher. Values observed in the
public catalog include: `OpenAI`, `Anthropic`, `Microsoft`, `DeepSeek`, `Mistral AI`, `Cohere`,
`Meta`, `xAI`, `Black Forest Labs`, `MoonshotAI`, `OpenAI-OSS`, `Alibaba`. Run once without
`-Providers` to see the current list in your own region.

## Alerting

```powershell
# Webhook
.\Watch-FoundryModels.ps1 -Regions eastus2 -WebhookUrl https://example.internal/alerts -Quiet

# Email
.\Watch-FoundryModels.ps1 -Regions eastus2 -EmailTo platform-team@example.com -Quiet

# Or just consume the objects
$new = .\Watch-FoundryModels.ps1 -Regions eastus2 -Quiet
$new | Where-Object Provider -eq 'Anthropic' | Export-Csv .\new-models.csv
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

A starter GitHub Actions workflow is in [`.github\workflows\watch.yml`](.github/workflows/watch.yml).

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
| `model.format` | The provider. This is your filter. |
| `model.name` / `model.version` | Identity. |
| `model.lifecycleStatus` | `Preview`, `GenerallyAvailable`, `Deprecating`, etc. |
| `model.systemData.createdAt` | When the entry appeared. A reliable recency signal. |
| `model.deprecation.inference` | **Retirement date.** See [Worth extending](#worth-extending). |
| `model.skus` | Deployment types available (`GlobalStandard`, `DataZoneStandard`, ...). |

The script builds a key of `region|provider|name|version` for each entry, stores the set, and
compares on the next run.

## Caveats

Two things will bite you if you write your own version:

1. **The catalog is per-region.** A model available in one region may not exist in another. Poll
   every region you deploy into, or you will get a confident answer about the wrong place.

2. **Entries are duplicated per account kind.** The same model is returned once under `OpenAI` and
   again under `AIServices`. Without deduplication your "new models" list roughly doubles and every
   entry looks like it arrived twice. The script dedupes on the composite key above — in one test
   that collapsed 320 raw entries to 107 distinct models.

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

## Verified behaviour

| Scenario | Result |
|---|---|
| First run, 3 providers, one region | 320 raw entries → 107 distinct, baseline written, no alerts |
| Re-run with no catalog change | "No new models." |
| Simulated arrival of 3 models | All three detected with provider, version, status, and date |
| Multi-region, all providers | 328 distinct across two regions |

## License

MIT. See [LICENSE](LICENSE).

## Disclaimer

This is a personal project, provided as-is. It is not an official Microsoft product, is not
supported by Microsoft, and carries no warranty. The Azure API surface it depends on may change.
Verify behaviour in your own environment before relying on it.
