#!/usr/bin/env python3
"""Trigger Pennsieve workflow runs for a list of package IDs.

Workflow types are defined in workflows.yaml — add new types there without
changing this script.

Usage:
    # Dataset mode: auto-discover packages, run all workflows
    python trigger_workflows.py --dataset

    # Dataset mode: specific workflows
    python trigger_workflows.py --dataset -w nifti -w tiff

    # File mode: run package IDs through a specific workflow
    python trigger_workflows.py package_ids.txt -w nifti

    # Check run statuses from a log file
    python trigger_workflows.py --status logs/runs_2026-08-26_143022.log

    # Check which packages are missing ome-zarr/thumb assets
    python trigger_workflows.py --asset-check -w nifti
    python trigger_workflows.py --asset-check --dataset-id N:dataset:xxx -w nifti

    # Legacy flags still work
    python trigger_workflows.py --dataset --nifti --tiff --plain-tiff
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

SCRIPT_DIR = Path(__file__).parent
LOGS_DIR = SCRIPT_DIR / "logs"
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "workflows.yaml"


# ---------------------------------------------------------------------------
# YAML config loading
# ---------------------------------------------------------------------------

def load_workflow_definitions(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Load workflow definitions from YAML and resolve aliases.

    Returns an OrderedDict-like dict (Python 3.7+ dicts preserve insertion order)
    mapping workflow name -> definition dict.
    """
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    workflows = raw.get("workflows", {})

    # Resolve aliases: inherit env_vars, defaults, cpu, memory from target
    for name, wf in workflows.items():
        alias = wf.get("alias_of")
        if alias:
            if alias not in workflows:
                raise ValueError(f"Workflow '{name}' aliases '{alias}' which is not defined")
            target = workflows[alias]
            for key in ("env_vars", "defaults", "cpu", "memory", "expected_assets"):
                if key not in wf and key in target:
                    wf[key] = target[key]

    return workflows


def classify_package(name: str, workflow_defs: dict) -> str | None:
    """Classify a package name by checking extensions in YAML definition order.

    Within each workflow, extensions are checked longest-first so that
    e.g. '.ome.tiff' matches before '.tiff'.
    """
    lower = name.lower()
    for wf_name, wf in workflow_defs.items():
        # Sort extensions longest-first for correct matching
        extensions = sorted(wf.get("extensions", []), key=len, reverse=True)
        for ext in extensions:
            if lower.endswith(ext):
                return wf_name
    return None


def build_workflow_config(workflow_def: dict, dataset_id: str) -> dict:
    """Build a workflow config dict by resolving env var names from the YAML definition.

    For each key in env_vars, looks up the env var value, falling back to defaults.
    """
    env_vars = workflow_def.get("env_vars", {})
    defaults = workflow_def.get("defaults", {})

    config = {"dataset_id": dataset_id}
    for key, env_name in env_vars.items():
        value = os.environ.get(env_name) or defaults.get(key)
        config[key] = value

    # CPU and memory with defaults
    config["cpu"] = workflow_def.get("cpu", "8192")
    config["memory"] = workflow_def.get("memory", "61440")

    return config


def get_all_package_types(workflow_defs: dict, selected: list[str]) -> set[str]:
    """Get the union of package_types from selected workflows."""
    types = set()
    for name in selected:
        wf = workflow_defs[name]
        types.update(wf.get("package_types", []))
    return types


# ---------------------------------------------------------------------------
# API helpers (unchanged)
# ---------------------------------------------------------------------------

def trigger_workflow_run(api_host: str, token: str, refresh_token: str, config: dict, package_id: str) -> dict:
    """Trigger a single workflow run for one package."""
    payload = {
        "workflowInstanceConfiguration": {
            "workflowId": config["workflow_id"],
            "computeNodeId": config["compute_node_id"],
            "processorConfigs": [
                {"nodeId": config["target_zarr_node_id"], "executionTarget": "standard"},
                {"nodeId": config["target_thumb_node_id"], "executionTarget": "standard"},
                {"nodeId": config["thumb_processor_node_id"], "executionTarget": "standard",
                 "version": config["thumb_processor_version"],
                 "cpu": config.get("cpu", "8192"), "memory": config.get("memory", "61440")},
                {"nodeId": config["converter_processor_node_id"], "executionTarget": "standard",
                 "version": config["converter_processor_version"],
                 "cpu": config.get("cpu", "8192"), "memory": config.get("memory", "61440")},
            ],
        },
        "datasetId": config["dataset_id"],
        "dataSources": {
            config["source_node_id"]: {
                "packageIds": [package_id],
            },
        },
        "dataTargets": {
            config["target_zarr_node_id"]: {
                "params": {
                    "ASSET_PROPERTIES_FILE": "asset-properties.json",
                    "ASSET_NAME": "preview",
                    "ASSET_TYPE": "ome-zarr",
                },
            },
            config["target_thumb_node_id"]: {
                "params": {
                    "ASSET_PROPERTIES_FILE": "asset-properties.json",
                    "ASSET_NAME": "thumbnail",
                    "ASSET_TYPE": "thumb",
                },
            },
        },
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if refresh_token:
        headers["x-refresh-token"] = refresh_token
    resp = requests.post(f"{api_host}/compute/workflows/runs", headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()


def fetch_dataset_packages(api_host: str, token: str, refresh_token: str,
                           dataset_id: str, package_types: set[str]) -> list[dict]:
    """Fetch all non-deleted packages from a dataset via the Pennsieve API.

    Queries for each type in package_types separately.
    Returns list of dicts with 'node_id' and 'name' keys.
    """
    packages_host = api_host.replace("api2.", "api.")
    headers = {"Authorization": f"Bearer {token}"}
    if refresh_token:
        headers["x-refresh-token"] = refresh_token
    packages = []
    seen = set()

    for pkg_type in sorted(package_types):
        cursor = None
        type_count = 0

        while True:
            params = {"types": pkg_type, "pageSize": 500}
            if cursor:
                params["cursor"] = cursor

            resp = requests.get(
                f"{packages_host}/datasets/{dataset_id}/packages",
                headers=headers,
                params=params,
            )
            if resp.status_code == 401:
                print("\nError: Unauthorized. Your session token may have expired.", file=sys.stderr)
                sys.exit(1)
            resp.raise_for_status()
            data = resp.json()

            for pkg in data.get("packages", []):
                content = pkg.get("content", {})
                node_id = content.get("nodeId")
                if content.get("state") == "DELETED":
                    continue
                if node_id in seen:
                    continue
                seen.add(node_id)
                packages.append({
                    "node_id": node_id,
                    "name": content.get("name", ""),
                })
                type_count += 1

            cursor = data.get("cursor")
            if not cursor:
                break

        print(f"  Fetched {type_count} {pkg_type} packages")

    print(f"  Total: {len(packages)} packages")
    return packages


def check_run_statuses(api_host: str, token: str, log_file: str):
    """Parse a log file for runIds and check their workflow run status."""
    pattern = re.compile(r"package:\s*(N:package:[0-9a-f-]+).*\(runId:\s*([0-9a-f-]+)\)")

    runs = []
    with open(log_file) as f:
        for line in f:
            m = pattern.search(line)
            if m:
                runs.append({"package_id": m.group(1), "run_id": m.group(2)})

    if not runs:
        print(f"No runIds found in {log_file}")
        return

    print(f"Found {len(runs)} runs in {log_file}\n")

    headers = {"Authorization": f"Bearer {token}"}

    results = {"SUCCEEDED": [], "FAILED": [], "STARTED": [], "FINALIZING": []}
    errors = []

    for i, run in enumerate(runs, 1):
        try:
            resp = requests.get(f"{api_host}/compute/workflows/runs/{run['run_id']}", headers=headers)
            if resp.status_code in (401, 403):
                print(f"\nAuth error ({resp.status_code}) at run {i}/{len(runs)}. Token may have expired.")
                break
            resp.raise_for_status()
            status = resp.json().get("status", "UNKNOWN")
            run["status"] = status
            if status in results:
                results[status].append(run)
            else:
                results.setdefault(status, []).append(run)
        except Exception as e:
            run["status"] = f"ERROR: {e}"
            errors.append(run)

        if i % 50 == 0:
            print(f"  Checked {i}/{len(runs)}...")

    print(f"\nSummary: {len(results.get('SUCCEEDED', []))} succeeded, "
          f"{len(results.get('FAILED', []))} failed, "
          f"{len(runs) - len(results.get('SUCCEEDED', [])) - len(results.get('FAILED', []))} other")

    for status in ("FAILED", "STARTED", "FINALIZING"):
        if results.get(status):
            print(f"\n{status}:")
            for run in results[status]:
                print(f"  {run['package_id']} (runId: {run['run_id']})")

    if errors:
        print(f"\nERRORS ({len(errors)}):")
        for run in errors:
            print(f"  {run['package_id']}: {run['status']}")

    failed = results.get("FAILED", [])
    if failed:
        print(f"\nFailed package IDs ({len(failed)}):")
        for run in failed:
            print(run["package_id"])


def get_package_assets(api_host: str, token: str, dataset_id: str, package_id: str) -> list[dict]:
    """Get viewer assets for a package. Tries discover endpoint first, falls back to non-published."""
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(
        f"{api_host}/packages/discover/assets",
        headers=headers,
        params={"package_id": package_id},
    )
    if resp.status_code == 200:
        assets = resp.json().get("assets", [])
        if assets:
            return assets

    resp = requests.get(
        f"{api_host}/packages/assets",
        headers=headers,
        params={"dataset_id": dataset_id, "package_id": package_id},
    )
    if resp.status_code in (401, 403):
        raise requests.HTTPError(response=resp)
    if resp.status_code == 200:
        return resp.json().get("assets", [])

    return []


def check_package_assets(api_host: str, token: str, refresh_token: str, dataset_id: str,
                         workflow_types: list[str], workflow_defs: dict):
    """Check which packages in a dataset are missing expected assets.

    Expected asset types are read from each workflow's 'expected_assets' list
    in workflows.yaml (defaults to ["ome-zarr", "thumb"]).
    """
    pkg_types = get_all_package_types(workflow_defs, workflow_types)
    packages = fetch_dataset_packages(api_host, token, refresh_token, dataset_id, pkg_types)

    filtered = []
    for pkg in packages:
        file_type = classify_package(pkg["name"], workflow_defs)
        if file_type in workflow_types:
            pkg["file_type"] = file_type
            filtered.append(pkg)

    if not filtered:
        print("No matching packages found.")
        return

    # Collect the union of expected asset types across selected workflows
    expected_by_workflow = {}
    for wf_name in workflow_types:
        expected_by_workflow[wf_name] = workflow_defs[wf_name].get(
            "expected_assets", ["ome-zarr", "thumb"])

    print(f"\nChecking {len(filtered)} packages for viewer assets...\n")

    complete = []
    incomplete = []  # list of (pkg, missing_assets)

    for i, pkg in enumerate(filtered, 1):
        try:
            assets = get_package_assets(api_host, token, dataset_id, pkg["node_id"])
            asset_types = {a["asset_type"] for a in assets}

            expected = expected_by_workflow.get(pkg["file_type"], ["ome-zarr", "thumb"])
            missing = [a for a in expected if a not in asset_types]

            if not missing:
                complete.append(pkg)
            else:
                incomplete.append((pkg, missing))

        except requests.HTTPError as e:
            if e.response.status_code in (401, 403):
                print(f"\nAuth error at package {i}/{len(filtered)}. Token may have expired.")
                print(f"Checked {i - 1} packages before error.")
                break
            expected = expected_by_workflow.get(pkg.get("file_type"), ["ome-zarr", "thumb"])
            incomplete.append((pkg, expected))
        except Exception as e:
            print(f"  Error checking {pkg['node_id']}: {e}")
            expected = expected_by_workflow.get(pkg.get("file_type"), ["ome-zarr", "thumb"])
            incomplete.append((pkg, expected))

        if i % 25 == 0:
            print(f"  Checked {i}/{len(filtered)}...")

    total_checked = len(complete) + len(incomplete)
    print(f"\nResults: {len(complete)}/{total_checked} packages have all expected assets")

    if incomplete:
        # Group by missing asset combination for a cleaner summary
        from collections import Counter
        missing_counts = Counter(tuple(sorted(m)) for _, m in incomplete)
        for missing_combo, count in missing_counts.most_common():
            print(f"  Missing {', '.join(missing_combo)}: {count}")

        print(f"\nIncomplete package IDs ({len(incomplete)}):")
        for pkg, missing in incomplete:
            print(pkg["node_id"])


def load_package_ids(filepath: str) -> list[str]:
    """Load package IDs from a text file (one per line)."""
    with open(filepath) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def _log(msg: str, log_file=None, end="\n"):
    """Print to stdout and optionally write to a log file."""
    print(msg, end=end)
    if log_file:
        log_file.write(msg + end)
        log_file.flush()


def run_workflow(args, workflow_config: dict, package_ids: list[str], label: str, log_file=None):
    """Run a workflow for a list of package IDs. Returns (succeeded, failed) counts."""
    _log(f"\n--- {label} workflow ---", log_file)
    _log(f"Workflow ID:       {workflow_config['workflow_id']}", log_file)
    _log(f"Packages:          {len(package_ids)}", log_file)
    _log("", log_file)

    if args.dry_run:
        for i, pkg_id in enumerate(package_ids, 1):
            _log(f"[DRY RUN] {i}/{len(package_ids)} Would trigger for package: {pkg_id}", log_file)
        _log(f"\nDry run complete. {len(package_ids)} runs would be triggered.", log_file)
        return len(package_ids), 0

    token = args.session_token
    succeeded = 0
    failed = 0

    for i, pkg_id in enumerate(package_ids, 1):
        try:
            _log(f"[{i}/{len(package_ids)}] Triggering for package: {pkg_id}...", log_file, end=" ")
            result = trigger_workflow_run(args.api_host, token, args.refresh_token, workflow_config, pkg_id)
            run_id = result.get("uuid", result.get("executionRunId", ""))
            _log(f"OK (runId: {run_id})", log_file)
            succeeded += 1
        except requests.HTTPError as e:
            _log(f"FAILED ({e.response.status_code}: {e.response.text})", log_file)
            failed += 1
            if args.stop_on_auth_error and e.response.status_code in (401, 403):
                _log(f"\n>>> Auth error hit. Stopping.", log_file)
                _log(f">>> Last successful package: [{succeeded}/{len(package_ids)}]", log_file)
                _log(f">>> Resume from package: {pkg_id}", log_file)
                remaining = package_ids[i:]
                _log(f">>> Remaining packages ({len(remaining)}):", log_file)
                for r in remaining:
                    _log(f"    {r}", log_file)
                return succeeded, failed
        except Exception as e:
            _log(f"FAILED ({e})", log_file)
            failed += 1

        if i < len(package_ids) and args.delay > 0:
            _log(f"  Waiting {int(args.delay)}s before next run...", log_file)
            time.sleep(args.delay)

    return succeeded, failed


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_config(args, workflow_defs: dict) -> list[str]:
    """Validate required config for selected workflows. Returns list of missing items."""
    missing = []
    if not args.session_token:
        missing.append("PENNSIEVE_SESSION_TOKEN (--session-token)")
    if not args.dataset_id:
        missing.append("DATASET_ID (--dataset-id)")

    # Check critical env vars for each selected workflow
    critical_keys = ("workflow_id", "compute_node_id")
    for wf_name in args.workflows:
        wf = workflow_defs[wf_name]
        env_vars = wf.get("env_vars", {})
        for key in critical_keys:
            env_name = env_vars.get(key)
            if env_name and not os.environ.get(env_name):
                label = wf.get("label", wf_name)
                missing.append(f"{env_name} ({label} workflow)")

    return missing


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    # Load workflow definitions first so we can use them in argparse
    try:
        config_path = DEFAULT_CONFIG_PATH
        # Quick pre-parse for --config flag
        for i, arg in enumerate(sys.argv[1:], 1):
            if arg == "--config" and i < len(sys.argv) - 1:
                config_path = Path(sys.argv[i + 1])
                break
            if arg.startswith("--config="):
                config_path = Path(arg.split("=", 1)[1])
                break

        workflow_defs = load_workflow_definitions(config_path)
    except FileNotFoundError:
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        print("Create workflows.yaml or specify --config <path>.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading workflow config: {e}", file=sys.stderr)
        sys.exit(1)

    available_names = list(workflow_defs.keys())

    parser = argparse.ArgumentParser(
        description="Trigger Pennsieve workflow runs for multiple packages"
    )
    parser.add_argument(
        "package_file",
        nargs="?",
        help="Text file with one package ID per line",
    )
    parser.add_argument(
        "--package-id",
        action="append",
        dest="package_ids",
        help="Package ID (can be specified multiple times)",
    )

    # Workflow type selection — new style
    parser.add_argument(
        "-w", "--workflow",
        action="append",
        dest="workflows",
        metavar="NAME",
        help=f"Workflow type to run (repeatable). Available: {', '.join(available_names)}",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to workflows.yaml config file",
    )

    # Legacy flags (hidden, for backwards compat)
    parser.add_argument("--nifti", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--tiff", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--plain-tiff", action="store_true", help=argparse.SUPPRESS)

    # General config
    parser.add_argument("--api-host", default=os.environ.get("PENNSIEVE_API_HOST", "https://api2.pennsieve.io"))
    parser.add_argument("--session-token", default=os.environ.get("PENNSIEVE_SESSION_TOKEN"),
                        help="Session token from the browser")
    parser.add_argument("--refresh-token", default=os.environ.get("PENNSIEVE_REFRESH_TOKEN"),
                        help="Refresh token from the browser (sent as x-refresh-token header)")
    parser.add_argument("--dataset-id", default=os.environ.get("DATASET_ID"))
    parser.add_argument("--delay", type=float, default=float(os.environ.get("DELAY_BETWEEN_RUNS", "30")),
                        help="Seconds to wait between triggering runs (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be triggered without actually doing it")
    parser.add_argument("--stop-on-auth-error", action="store_true",
                        help="Stop immediately on 401/403 and print remaining packages")
    parser.add_argument("--dataset", action="store_true",
                        help="Auto-discover packages from the dataset and classify by file type")
    parser.add_argument("--status", metavar="LOGFILE",
                        help="Check workflow run statuses from a previous run's log file")
    parser.add_argument("--asset-check", action="store_true",
                        help="Check which packages are missing ome-zarr/thumb assets")
    parser.add_argument("--list-packages", action="store_true",
                        help="Discover packages from --dataset and save IDs to batches/ (no workflows triggered)")

    args = parser.parse_args()

    # Resolve legacy flags into args.workflows
    legacy_map = {"nifti": args.nifti, "tiff": args.tiff, "plain_tiff": args.plain_tiff}
    legacy_used = [name for name, flag in legacy_map.items() if flag]
    if legacy_used:
        if args.workflows:
            parser.error("Cannot mix legacy flags (--nifti, --tiff, --plain-tiff) with -w/--workflow.")
        args.workflows = legacy_used

    # Validate workflow names
    if args.workflows:
        for name in args.workflows:
            if name not in workflow_defs:
                parser.error(f"Unknown workflow '{name}'. Available: {', '.join(available_names)}")

    # Status check mode
    if args.status:
        if not args.session_token:
            print("Error: PENNSIEVE_SESSION_TOKEN required for status checks.", file=sys.stderr)
            sys.exit(1)
        check_run_statuses(args.api_host, args.session_token, args.status)
        return

    # Asset check mode
    if args.asset_check:
        if not args.session_token:
            print("Error: PENNSIEVE_SESSION_TOKEN required.", file=sys.stderr)
            sys.exit(1)
        if not args.dataset_id:
            print("Error: DATASET_ID required.", file=sys.stderr)
            sys.exit(1)
        types = args.workflows if args.workflows else available_names
        check_package_assets(args.api_host, args.session_token, args.refresh_token,
                             args.dataset_id, types, workflow_defs)
        return

    # List packages mode
    if args.list_packages:
        if not args.session_token:
            print("Error: PENNSIEVE_SESSION_TOKEN required.", file=sys.stderr)
            sys.exit(1)
        if not args.dataset_id:
            print("Error: DATASET_ID required.", file=sys.stderr)
            sys.exit(1)
        types = args.workflows if args.workflows else available_names
        pkg_types = get_all_package_types(workflow_defs, types)
        print(f"Discovering packages from dataset {args.dataset_id}...")
        packages = fetch_dataset_packages(args.api_host, args.session_token, args.refresh_token,
                                          args.dataset_id, pkg_types)

        batches_dir = SCRIPT_DIR / "batches"
        batches_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

        for wf_name in types:
            label = workflow_defs[wf_name].get("label", wf_name)
            ids = [pkg["node_id"] for pkg in packages
                   if classify_package(pkg["name"], workflow_defs) == wf_name]
            if not ids:
                print(f"  {label}: 0 packages (skipped)")
                continue
            out_path = batches_dir / f"{wf_name}_{timestamp}.txt"
            with open(out_path, "w") as f:
                f.write(f"# {label} packages from {args.dataset_id}\n")
                f.write(f"# Generated: {datetime.now().isoformat()}\n")
                for pkg_id in ids:
                    f.write(pkg_id + "\n")
            print(f"  {label}: {len(ids)} packages -> {out_path}")
        return

    # In dataset mode, default to all workflows when none specified
    if args.dataset and not args.workflows:
        args.workflows = available_names

    if not args.workflows:
        parser.error(f"At least one workflow must be specified with -w/--workflow. "
                     f"Available: {', '.join(available_names)}")

    if not args.dataset and len(args.workflows) > 1:
        parser.error("Cannot use multiple workflows without --dataset. "
                      "In file/CLI mode, specify exactly one with -w.")

    # Validate required config
    missing = validate_config(args, workflow_defs)
    if missing:
        print(f"Error: Missing required config:\n  " + "\n  ".join(missing), file=sys.stderr)
        print("\nSet these in .env or pass as CLI args.", file=sys.stderr)
        sys.exit(1)

    # Build configs for selected workflows
    workflow_configs = {}
    for wf_name in args.workflows:
        workflow_configs[wf_name] = build_workflow_config(workflow_defs[wf_name], args.dataset_id)

    # Collect package IDs per workflow
    workflow_packages = {wf_name: [] for wf_name in args.workflows}

    if args.dataset:
        pkg_types = get_all_package_types(workflow_defs, args.workflows)
        print(f"Discovering packages from dataset {args.dataset_id}...")
        packages = fetch_dataset_packages(args.api_host, args.session_token, args.refresh_token,
                                          args.dataset_id, pkg_types)

        skipped = []
        for pkg in packages:
            file_type = classify_package(pkg["name"], workflow_defs)
            if file_type in workflow_packages:
                workflow_packages[file_type].append(pkg["node_id"])
            else:
                skipped.append(pkg)

        print(f"\nFound {len(packages)} packages total:")
        for wf_name in args.workflows:
            label = workflow_defs[wf_name].get("label", wf_name)
            print(f"  {label}: {len(workflow_packages[wf_name])}")
        if skipped:
            print(f"  Skipped:    {len(skipped)}")
        print()

        if not any(workflow_packages.values()):
            print("No matching packages found for the selected workflow type(s).")
            return
    else:
        # File/CLI mode — all IDs go to the single selected workflow
        package_ids = []
        if args.package_file:
            package_ids.extend(load_package_ids(args.package_file))
        if args.package_ids:
            package_ids.extend(args.package_ids)

        if not package_ids:
            parser.error("No package IDs provided. Use a file, --package-id flags, or --dataset.")

        wf_name = args.workflows[0]
        workflow_packages[wf_name] = package_ids

    print(f"Dataset ID:        {args.dataset_id}")
    print(f"API host:          {args.api_host}")

    if not args.dry_run:
        print("\nUsing provided session token.")

    # Auto-logging setup
    log_file = None
    log_path = None
    if not args.dry_run:
        LOGS_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        log_path = LOGS_DIR / f"runs_{timestamp}.log"
        log_file = open(log_path, "w")
        log_file.write(f"# Dataset: {args.dataset_id}\n")
        log_file.write(f"# Started: {datetime.now().isoformat()}\n")
        log_file.flush()
        print(f"Logging to: {log_path}")

    total_succeeded = 0
    total_failed = 0

    try:
        for wf_name in args.workflows:
            pkg_ids = workflow_packages[wf_name]
            if not pkg_ids:
                continue
            label = workflow_defs[wf_name].get("label", wf_name)
            config = workflow_configs[wf_name]
            s, f = run_workflow(args, config, pkg_ids, label, log_file)
            total_succeeded += s
            total_failed += f

        if not args.dry_run:
            msg = f"\nDone. Succeeded: {total_succeeded}, Failed: {total_failed}"
            _log(msg, log_file)
    finally:
        if log_file:
            log_file.close()
            print(f"Log saved: {log_path}")


if __name__ == "__main__":
    main()
