#!/usr/bin/env python3
"""Watch the regional Microsoft Foundry catalog using filters saved in .env."""

import argparse
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import dotenv_values


SCRIPT_DIR = Path(__file__).resolve().parent
ARM_RESOURCE = "https://management.azure.com"
GRAPH_RESOURCE = "https://graph.microsoft.com"
API_VERSION = "2025-06-01"


def csv_list(value):
    """Trim comma-separated values and remove duplicates, ignoring case."""
    result = {}
    for item in value.split(","):
        item = item.strip()
        if item:
            result.setdefault(item.casefold(), item)
    return list(result.values())


def parse_bool(value):
    if value.strip().casefold() in {"1", "true", "yes", "on"}:
        return True
    if value.strip().casefold() in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError("QUIET must be true or false")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Settings file (default: .env beside this script)")
    parser.add_argument("--regions", help="Comma-separated Azure regions")
    parser.add_argument("--providers", help="Comma-separated providers; an empty string watches all")
    parser.add_argument("--deployment-types", help="Comma-separated SKU names; an empty string watches all")
    parser.add_argument("--state-path", help="Baseline JSON path")
    parser.add_argument("--webhook-url", help="URL to POST alerts to")
    parser.add_argument("--email-to", help="Comma-separated Microsoft Graph email recipients")
    parser.add_argument("--quiet", action=argparse.BooleanOptionalAction, default=None,
                        help="Suppress progress and the results table")
    parser.add_argument("--json", action="store_true", help="Print new models as JSON, with progress on stderr")
    args = parser.parse_args(argv)
    env_path = args.env_file if args.env_file is not None else SCRIPT_DIR / ".env"
    if args.env_file is not None and not env_path.is_file():
        parser.error(f"Environment file does not exist: {env_path}")
    # Process environment overrides saved settings, so CI can inject secrets.
    saved = dotenv_values(env_path, encoding="utf-8-sig", interpolate=False) if env_path.is_file() else {}
    settings = {**saved, **os.environ}

    def setting(name, default=""):
        cli_value = getattr(args, name)
        return cli_value if cli_value is not None else (settings.get(name.upper()) or default)

    args.regions = csv_list(setting("regions", "eastus2"))
    args.providers = csv_list(setting("providers"))
    args.deployment_types = csv_list(setting("deployment_types"))
    args.email_to = csv_list(setting("email_to"))
    args.webhook_url = setting("webhook_url")
    args.state_path = Path(setting("state_path", "model-watch-state.json")).expanduser()
    if not args.state_path.is_absolute():
        args.state_path = SCRIPT_DIR / args.state_path
    try:
        args.quiet = parse_bool(setting("quiet", "false")) if args.quiet is None else args.quiet
    except ValueError as exc:
        parser.error(str(exc))
    if not args.regions:
        parser.error("At least one region is required")
    return args


def az_output(*args):
    executable = shutil.which("az")
    if executable is None:
        raise RuntimeError("Azure CLI is required. Install it and run 'az login' first.")
    try:
        result = subprocess.run([executable, *args, "-o", "tsv"], capture_output=True,
                                text=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Azure CLI failed. Check 'az login', the selected subscription, and permissions.") from exc
    if not result.stdout.strip():
        raise RuntimeError("Azure CLI returned no account or token. Run 'az login' first.")
    return result.stdout.strip()


def access_token(resource):
    return az_output("account", "get-access-token", "--resource", resource, "--query", "accessToken")


def request_json(url, *, token=None, payload=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers=headers)
    try:
        with urlopen(request, timeout=60) as response:
            # Webhooks can reply with plain text, and Graph sendMail returns no body.
            if payload is not None:
                return None
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"HTTP request failed with status {exc.code}; check Azure permissions or the alert endpoint.") from exc
    except URLError as exc:
        raise RuntimeError(f"HTTP request failed: {exc.reason}") from exc


def fetch_models(subscription_id, token, regions, providers, deployment_types, info):
    wanted_providers = {value.casefold() for value in providers}
    wanted_types = {value.casefold() for value in deployment_types}
    models = {}
    sku_names = {}
    for region in regions:
        info(f"Polling {region} ...")
        url = (f"{ARM_RESOURCE}/subscriptions/{quote(subscription_id, safe='')}"
               f"/providers/Microsoft.CognitiveServices/locations/{quote(region, safe='')}"
               f"/models?api-version={API_VERSION}")
        while url:
            page = request_json(url, token=token)
            if not isinstance(page, dict) or not isinstance(page.get("value"), list):
                raise ValueError("Unexpected catalog response: expected a 'value' array")
            for entry in page["value"]:
                model = entry.get("model")
                if not model:
                    continue
                provider = model.get("format") or ""
                if wanted_providers and provider.casefold() not in wanted_providers:
                    continue
                key = f"{region}|{provider}|{model.get('name') or ''}|{model.get('version') or ''}"
                models.setdefault(key, {
                    "Key": key,
                    "Region": region,
                    "Provider": provider,
                    "Name": model.get("name"),
                    "Version": model.get("version"),
                    "Lifecycle": model.get("lifecycleStatus"),
                    "CreatedAt": (model.get("systemData") or {}).get("createdAt"),
                    "Retires": (model.get("deprecation") or {}).get("inference"),
                    "AssetId": model.get("modelCatalogAssetId"),
                })
                # Account-kind duplicates can advertise different deployment types.
                names = sku_names.setdefault(key, {})
                for sku in model.get("skus") or []:
                    if sku.get("name"):
                        names.setdefault(sku["name"].casefold(), sku["name"])
            url = page.get("nextLink")

    current = []
    for key in sorted(models):
        names = sku_names[key]
        if wanted_types and not wanted_types.intersection(names):
            continue
        models[key]["Skus"] = ",".join(sorted(names.values(), key=str.casefold))
        current.append(models[key])
    return current


def load_baseline(path):
    if not path.exists():
        return None
    # Windows PowerShell's UTF-8 state files can contain a BOM.
    prior = json.loads(path.read_text(encoding="utf-8-sig"))
    keys = prior.get("keys") if isinstance(prior, dict) else None
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ValueError(f"Invalid baseline in {path}: expected a 'keys' array of strings")
    return set(keys)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def save_baseline(path, args, current):
    state = {"lastRun": timestamp(), "regions": args.regions, "providers": args.providers,
             "deploymentTypes": args.deployment_types, "keys": [model["Key"] for model in current]}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle, indent=2)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def send_alerts(new_models, args, info):
    if args.webhook_url:
        fields = ("Provider", "Name", "Version", "Lifecycle", "Region", "CreatedAt", "Skus")
        request_json(args.webhook_url, payload={
            "source": "foundry-model-watch", "detected": timestamp(),
            "newModels": [{field: model[field] for field in fields} for model in new_models],
        })
        info("Posted to webhook.")
    if args.email_to:
        fields = ("Provider", "Name", "Version", "Lifecycle", "Region", "Skus")
        rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(model[field] or ''))}</td>"
                                       for field in fields) + "</tr>" for model in new_models)
        content = (f"<p>{len(new_models)} new model(s) detected in the Microsoft Foundry catalog.</p>"
                   '<table border="1" cellpadding="6" cellspacing="0"><tr>'
                   + "".join(f"<th>{field}</th>" for field in fields) + "</tr>" + rows + "</table>"
                   + f"<p>Regions polled: {html.escape(', '.join(args.regions))}</p>")
        request_json(f"{GRAPH_RESOURCE}/v1.0/me/sendMail", token=access_token(GRAPH_RESOURCE), payload={
            "message": {
                "subject": f"Foundry catalog: {len(new_models)} new model(s)",
                "body": {"contentType": "HTML", "content": content},
                "toRecipients": [{"emailAddress": {"address": address}} for address in args.email_to],
            },
            "saveToSentItems": True,
        })
        info(f"Alert emailed to: {', '.join(args.email_to)}")


def print_table(models):
    fields = ("Provider", "Name", "Version", "Lifecycle", "Region", "Skus")
    rows = [list(fields)] + [[str(model[field] or "") for field in fields] for model in models]
    widths = [max(len(row[index]) for row in rows) for index in range(len(fields))]
    for row in [rows[0], ["-" * width for width in widths], *rows[1:]]:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)))


def run(args):
    def info(message):
        if not args.quiet:
            print(message, file=sys.stderr if args.json else sys.stdout)

    known = load_baseline(args.state_path)
    subscription_id = az_output("account", "show", "--query", "id")
    token = access_token(ARM_RESOURCE)
    current = fetch_models(subscription_id, token, args.regions, args.providers, args.deployment_types, info)
    info(f"Found {len(current)} distinct models across: {', '.join(args.regions)}")
    new_models = [] if known is None else [model for model in current if model["Key"] not in known]
    new_models.sort(key=lambda model: (model["Provider"], model["Name"] or "", model["Region"]))
    if new_models:
        info(f"\n{len(new_models)} NEW model(s):\n")
        if not args.quiet and not args.json:
            print_table(new_models)
        send_alerts(new_models, args, info)
    # Keep the previous baseline if polling or alert delivery fails, allowing a retry.
    save_baseline(args.state_path, args, current)
    if known is None:
        info(f"Baseline written to {args.state_path} with {len(current)} models. No alerts on first run.")
    elif not new_models:
        info("No new models.")
    if args.json:
        print(json.dumps(new_models, indent=2))
    return new_models


def main(argv=None):
    try:
        run(parse_args(argv))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
