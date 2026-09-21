<#
.SYNOPSIS
    Detects newly added models in the Microsoft Foundry catalog and alerts on them.

.DESCRIPTION
    Polls the Azure Cognitive Services model catalog for one or more regions, filters to
    the providers you care about, and compares the result against a stored baseline.
    Anything not in the baseline is reported as NEW.

    Providers are matched on the model's `format` field, which is the publisher:
    OpenAI, Anthropic, Microsoft, DeepSeek, Mistral AI, Cohere, Meta, xAI,
    Black Forest Labs, MoonshotAI, OpenAI-OSS, Alibaba.

    First run writes the baseline and reports nothing. Subsequent runs report deltas.

.PARAMETER Regions
    Azure regions to poll. The catalog differs per region, so poll every region you deploy into.

.PARAMETER Providers
    Providers to watch. Omit to watch all.

.PARAMETER StatePath
    Where the baseline JSON is kept. Defaults to .\model-watch-state.json beside the script.

.PARAMETER EmailTo
    If supplied, sends an alert email via Microsoft Graph when new models are found.
    Requires an authenticated context with Mail.Send.

.PARAMETER WebhookUrl
    If supplied, POSTs a JSON payload when new models are found. Use for Teams, Slack,
    ServiceNow, or any internal alerting endpoint.

.EXAMPLE
    .\Watch-FoundryModels.ps1 -Regions eastus2,swedencentral -Providers OpenAI,Anthropic

.EXAMPLE
    .\Watch-FoundryModels.ps1 -Regions eastus2 -WebhookUrl https://internal/alerts -Quiet

.NOTES
    Auth: uses the current `az login` context. For unattended runs use a service principal
    or managed identity with Reader on the subscription.
#>

[CmdletBinding()]
param(
    [string[]] $Regions   = @('eastus2'),
    [string[]] $Providers = @(),
    [string]   $StatePath,
    [string[]] $EmailTo   = @(),
    [string]   $WebhookUrl,
    [switch]   $Quiet
)

$ErrorActionPreference = 'Stop'

if (-not $StatePath) {
    $StatePath = Join-Path $PSScriptRoot 'model-watch-state.json'
}

function Write-Info { param($m) if (-not $Quiet) { Write-Host $m } }

# --- Acquire an ARM token from the current az context -----------------------
try {
    $subId = az account show --query id -o tsv 2>$null
    $token = az account get-access-token --resource https://management.azure.com --query accessToken -o tsv 2>$null
} catch {
    throw "Could not get an Azure token. Run 'az login' first."
}
if (-not $subId -or -not $token) { throw "Not signed in to Azure. Run 'az login' first." }

# --- Pull the catalog for each region ---------------------------------------
$apiVersion = '2025-06-01'
$current = [System.Collections.Generic.List[object]]::new()

foreach ($region in $Regions) {
    Write-Info "Polling $region ..."
    $uri = "https://management.azure.com/subscriptions/$subId/providers/Microsoft.CognitiveServices/locations/$region/models?api-version=$apiVersion"

    $page = $null
    do {
        $target = if ($page) { $page } else { $uri }
        $resp = Invoke-RestMethod -Uri $target -Headers @{ Authorization = "Bearer $token" } -Method Get -TimeoutSec 60
        foreach ($entry in $resp.value) {
            $mdl = $entry.model
            if (-not $mdl) { continue }
            if ($Providers.Count -gt 0 -and $mdl.format -notin $Providers) { continue }

            $current.Add([pscustomobject]@{
                Key       = "$region|$($mdl.format)|$($mdl.name)|$($mdl.version)"
                Region    = $region
                Provider  = $mdl.format
                Name      = $mdl.name
                Version   = $mdl.version
                Lifecycle = $mdl.lifecycleStatus
                CreatedAt = $mdl.systemData.createdAt
                Retires   = $mdl.deprecation.inference
                Skus      = ($mdl.skus.name | Sort-Object -Unique) -join ','
                AssetId   = $mdl.modelCatalogAssetId
            })
        }
        $page = $resp.nextLink
    } while ($page)
}

# The catalog returns one entry per account-kind, so the same model appears more than once.
$current = $current | Sort-Object Key -Unique
Write-Info "Found $($current.Count) distinct models across: $($Regions -join ', ')"

# --- Compare against the baseline -------------------------------------------
$firstRun = -not (Test-Path $StatePath)
$known = @{}
if (-not $firstRun) {
    $prior = Get-Content $StatePath -Raw | ConvertFrom-Json
    foreach ($k in $prior.keys) { $known[$k] = $true }
}

$new = @($current | Where-Object { -not $known.ContainsKey($_.Key) })

# --- Persist the new baseline ------------------------------------------------
[pscustomobject]@{
    lastRun   = (Get-Date).ToString('o')
    regions   = $Regions
    providers = $Providers
    keys      = @($current.Key)
} | ConvertTo-Json -Depth 4 | Set-Content $StatePath -Encoding UTF8

if ($firstRun) {
    Write-Info "Baseline written to $StatePath with $($current.Count) models. No alerts on first run."
    return
}

if ($new.Count -eq 0) {
    Write-Info "No new models."
    return
}

# --- Report -------------------------------------------------------------------
Write-Info "`n$($new.Count) NEW model(s):`n"
$table = $new | Sort-Object Provider, Name |
    Select-Object Provider, Name, Version, Lifecycle, Region,
                  @{n='Added';e={ if ($_.CreatedAt) { ([datetime]$_.CreatedAt).ToString('yyyy-MM-dd') } }}
if (-not $Quiet) { $table | Format-Table -AutoSize | Out-Host }

# Emit to the pipeline so it can be consumed by a caller
$new

# --- Optional: webhook --------------------------------------------------------
if ($WebhookUrl) {
    $payload = @{
        source    = 'foundry-model-watch'
        detected  = (Get-Date).ToString('o')
        newModels = @($new | Select-Object Provider, Name, Version, Lifecycle, Region, CreatedAt, Skus)
    } | ConvertTo-Json -Depth 5
    Invoke-RestMethod -Uri $WebhookUrl -Method Post -Body $payload -ContentType 'application/json' | Out-Null
    Write-Info "Posted to webhook."
}

# --- Optional: email via Microsoft Graph ---------------------------------------
if ($EmailTo.Count -gt 0) {
    $rows = ($new | Sort-Object Provider, Name | ForEach-Object {
        "<tr><td>$($_.Provider)</td><td>$($_.Name)</td><td>$($_.Version)</td><td>$($_.Lifecycle)</td><td>$($_.Region)</td></tr>"
    }) -join ''
    $html = @"
<p>$($new.Count) new model(s) detected in the Microsoft Foundry catalog.</p>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:Segoe UI,sans-serif;font-size:10pt">
<tr><th>Provider</th><th>Model</th><th>Version</th><th>Status</th><th>Region</th></tr>
$rows
</table>
<p style="font-size:9pt;color:#666">Regions polled: $($Regions -join ', ')</p>
"@
    $graphToken = az account get-access-token --resource https://graph.microsoft.com --query accessToken -o tsv 2>$null
    $body = @{
        message = @{
            subject = "Foundry catalog: $($new.Count) new model(s)"
            body    = @{ contentType = 'HTML'; content = $html }
            toRecipients = @($EmailTo | ForEach-Object { @{ emailAddress = @{ address = $_ } } })
        }
        saveToSentItems = $true
    } | ConvertTo-Json -Depth 8
    Invoke-RestMethod -Uri 'https://graph.microsoft.com/v1.0/me/sendMail' -Method Post `
        -Headers @{ Authorization = "Bearer $graphToken" } -Body $body -ContentType 'application/json' | Out-Null
    Write-Info "Alert emailed to: $($EmailTo -join ', ')"
}
