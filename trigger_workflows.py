#!/usr/bin/env python3
"""Trigger Pennsieve workflow runs for a list of package IDs.

Supports three workflows: NIfTI-to-Zarr, OME-TIFF-to-Zarr, and plain TIFF-to-Zarr.

Usage:
    # Dataset mode: auto-discover packages, run all workflows
    python trigger_workflows.py --dataset --nifti --tiff --plain-tiff

    # Dataset mode: only nifti
    python trigger_workflows.py --dataset --nifti

    # Dataset mode: only plain tiffs
    python trigger_workflows.py --dataset --plain-tiff

    # File mode: run package IDs through nifti workflow
    python trigger_workflows.py package_ids.txt --nifti

    # File mode: run package IDs through tiff workflow
    python trigger_workflows.py package_ids.txt --tiff

    # File mode: run package IDs through plain tiff workflow
    python trigger_workflows.py package_ids.txt --plain-tiff

    # Check run statuses from a log file
    python trigger_workflows.py --status logs/runs_2026-08-26_143022.log

    # Check which packages are missing ome-zarr/thumb assets
    python trigger_workflows.py --asset-check --nifti
    python trigger_workflows.py --asset-check --dataset-id N:dataset:xxx --nifti
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
from dotenv import load_dotenv

load_dotenv()

SCRIPT_DIR = Path(__file__).parent
LOGS_DIR = SCRIPT_DIR / "logs"

NIFTI_EXTENSIONS = (".nii.gz", ".nii")
TIFF_EXTENSIONS = (".ome.tiff", ".ome.tif")
PLAIN_TIFF_EXTENSIONS = (".tiff", ".tif")


def classify_package(name: str) -> str | None:
    """Classify a package name as 'nifti', 'tiff', 'plain_tiff', or None based on file extension."""
    lower = name.lower()
    # Check OME-TIFF before plain TIFF since .ome.tiff ends with .tiff
    for ext in TIFF_EXTENSIONS:
        if lower.endswith(ext):
            return "tiff"
    for ext in PLAIN_TIFF_EXTENSIONS:
        if lower.endswith(ext):
            return "plain_tiff"
    for ext in NIFTI_EXTENSIONS:
        if lower.endswith(ext):
            return "nifti"
    return None


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
                 "version": config["thumb_processor_version"], "cpu": "8192", "memory": "61440"},
                {"nodeId": config["converter_processor_node_id"], "executionTarget": "standard",
                 "version": config["converter_processor_version"], "cpu": "8192", "memory": "61440"},
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


PACKAGE_TYPES = ("MRI", "Slide")


def fetch_dataset_packages(api_host: str, token: str, refresh_token: str, dataset_id: str) -> list[dict]:
    """Fetch all non-deleted packages from a dataset via the Pennsieve API.

    Queries for each type in PACKAGE_TYPES separately.
    Returns list of dicts with 'node_id' and 'name' keys.
    """
    # The packages endpoint is on the v1 API (api.pennsieve.io), not api2
    packages_host = api_host.replace("api2.", "api.")
    headers = {"Authorization": f"Bearer {token}"}
    if refresh_token:
        headers["x-refresh-token"] = refresh_token
    packages = []
    seen = set()

    for pkg_type in PACKAGE_TYPES:
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
    # Parse lines like: [1/200] Triggering for package: N:package:xxx... OK (runId: abc-123)
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

    # Print summary
    print(f"\nSummary: {len(results.get('SUCCEEDED', []))} succeeded, "
          f"{len(results.get('FAILED', []))} failed, "
          f"{len(runs) - len(results.get('SUCCEEDED', [])) - len(results.get('FAILED', []))} other")

    # Print non-succeeded runs
    for status in ("FAILED", "STARTED", "FINALIZING"):
        if results.get(status):
            print(f"\n{status}:")
            for run in results[status]:
                print(f"  {run['package_id']} (runId: {run['run_id']})")

    if errors:
        print(f"\nERRORS ({len(errors)}):")
        for run in errors:
            print(f"  {run['package_id']}: {run['status']}")

    # Print failed package IDs for easy copy-paste into a batch file
    failed = results.get("FAILED", [])
    if failed:
        print(f"\nFailed package IDs ({len(failed)}):")
        for run in failed:
            print(run["package_id"])


def get_package_assets(api_host: str, token: str, dataset_id: str, package_id: str) -> list[dict]:
    """Get viewer assets for a package. Tries discover endpoint first, falls back to non-published."""
    headers = {"Authorization": f"Bearer {token}"}

    # Try published/discover endpoint first
    resp = requests.get(
        f"{api_host}/packages/discover/assets",
        headers=headers,
        params={"package_id": package_id},
    )
    if resp.status_code == 200:
        assets = resp.json().get("assets", [])
        if assets:
            return assets

    # Fall back to non-published endpoint
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
                         workflow_types: list[str]):
    """Check which packages in a dataset are missing ome-zarr and/or thumb assets."""
    packages = fetch_dataset_packages(api_host, token, refresh_token, dataset_id)

    # Filter to requested types
    filtered = []
    for pkg in packages:
        file_type = classify_package(pkg["name"])
        if file_type in workflow_types:
            pkg["file_type"] = file_type
            filtered.append(pkg)

    if not filtered:
        print("No matching packages found.")
        return

    print(f"\nChecking {len(filtered)} packages for viewer assets...\n")

    complete = []
    missing_zarr = []
    missing_thumb = []
    missing_both = []

    for i, pkg in enumerate(filtered, 1):
        try:
            assets = get_package_assets(api_host, token, dataset_id, pkg["node_id"])
            asset_types = {a["asset_type"] for a in assets}

            has_zarr = "ome-zarr" in asset_types
            has_thumb = "thumb" in asset_types

            if has_zarr and has_thumb:
                complete.append(pkg)
            elif not has_zarr and not has_thumb:
                missing_both.append(pkg)
            elif not has_zarr:
                missing_zarr.append(pkg)
            else:
                missing_thumb.append(pkg)

        except requests.HTTPError as e:
            if e.response.status_code in (401, 403):
                print(f"\nAuth error at package {i}/{len(filtered)}. Token may have expired.")
                print(f"Checked {i - 1} packages before error.")
                break
            missing_both.append(pkg)
        except Exception as e:
            print(f"  Error checking {pkg['node_id']}: {e}")
            missing_both.append(pkg)

        if i % 25 == 0:
            print(f"  Checked {i}/{len(filtered)}...")

    # Summary
    total_checked = len(complete) + len(missing_zarr) + len(missing_thumb) + len(missing_both)
    total_incomplete = len(missing_zarr) + len(missing_thumb) + len(missing_both)
    print(f"\nResults: {len(complete)}/{total_checked} packages have both assets")
    if missing_both:
        print(f"  Missing both:     {len(missing_both)}")
    if missing_zarr:
        print(f"  Missing ome-zarr: {len(missing_zarr)}")
    if missing_thumb:
        print(f"  Missing thumb:    {len(missing_thumb)}")

    # List incomplete package IDs for re-processing
    if total_incomplete > 0:
        all_incomplete = missing_both + missing_zarr + missing_thumb
        print(f"\nIncomplete package IDs ({total_incomplete}):")
        for pkg in all_incomplete:
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


def build_nifti_config(args) -> dict:
    """Build workflow config dict for NIfTI workflow."""
    return {
        "workflow_id": args.workflow_id,
        "dataset_id": args.dataset_id,
        "compute_node_id": args.compute_node_id,
        "source_node_id": args.source_node_id,
        "target_zarr_node_id": args.target_zarr_node_id,
        "target_thumb_node_id": args.target_thumb_node_id,
        "converter_processor_node_id": args.nifti_processor_node_id,
        "thumb_processor_node_id": args.thumb_processor_node_id,
        "converter_processor_version": args.nifti_processor_version,
        "thumb_processor_version": args.thumb_processor_version,
    }


def build_tiff_config(args) -> dict:
    """Build workflow config dict for OME-TIFF workflow."""
    return {
        "workflow_id": args.tiff_workflow_id,
        "dataset_id": args.dataset_id,
        "compute_node_id": args.tiff_compute_node_id,
        "source_node_id": args.tiff_source_node_id,
        "target_zarr_node_id": args.tiff_target_zarr_node_id,
        "target_thumb_node_id": args.tiff_target_thumb_node_id,
        "converter_processor_node_id": args.tiff_processor_node_id,
        "thumb_processor_node_id": args.tiff_thumb_processor_node_id,
        "converter_processor_version": args.tiff_processor_version,
        "thumb_processor_version": args.tiff_thumb_processor_version,
    }


def validate_config(args) -> list[str]:
    """Validate required config. Returns list of missing items."""
    missing = []
    if not args.session_token:
        missing.append("PENNSIEVE_SESSION_TOKEN (--session-token)")
    if not args.dataset_id:
        missing.append("DATASET_ID (--dataset-id)")

    if args.nifti:
        if not args.workflow_id:
            missing.append("WORKFLOW_ID (--workflow-id)")
        if not args.compute_node_id:
            missing.append("COMPUTE_NODE_ID (--compute-node-id)")

    if args.tiff or args.plain_tiff:
        if not args.tiff_workflow_id:
            missing.append("TIFF_WORKFLOW_ID (--tiff-workflow-id)")
        if not args.tiff_compute_node_id:
            missing.append("TIFF_COMPUTE_NODE_ID (--tiff-compute-node-id)")

    return missing


def main():
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

    # Workflow type selection
    parser.add_argument("--nifti", action="store_true",
                        help="Run the NIfTI-to-Zarr workflow")
    parser.add_argument("--tiff", action="store_true",
                        help="Run the OME-TIFF-to-Zarr workflow")
    parser.add_argument("--plain-tiff", action="store_true",
                        help="Run the plain TIFF-to-Zarr workflow (uses same converter as OME-TIFF)")

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

    # NIfTI workflow config
    parser.add_argument("--workflow-id", default=os.environ.get("WORKFLOW_ID"))
    parser.add_argument("--compute-node-id", default=os.environ.get("COMPUTE_NODE_ID"))
    parser.add_argument("--source-node-id", default=os.environ.get("SOURCE_NODE_ID"))
    parser.add_argument("--target-zarr-node-id", default=os.environ.get("TARGET_ZARR_NODE_ID"))
    parser.add_argument("--target-thumb-node-id", default=os.environ.get("TARGET_THUMB_NODE_ID"))
    parser.add_argument("--nifti-processor-node-id", default=os.environ.get("NIFTI_PROCESSOR_NODE_ID"))
    parser.add_argument("--thumb-processor-node-id", default=os.environ.get("THUMB_PROCESSOR_NODE_ID"))
    parser.add_argument("--nifti-processor-version", default=os.environ.get("NIFTI_PROCESSOR_VERSION", "v1.1.1"))
    parser.add_argument("--thumb-processor-version", default=os.environ.get("THUMB_PROCESSOR_VERSION", "v1.3.1"))

    # TIFF workflow config
    parser.add_argument("--tiff-workflow-id", default=os.environ.get("TIFF_WORKFLOW_ID"))
    parser.add_argument("--tiff-compute-node-id", default=os.environ.get("TIFF_COMPUTE_NODE_ID"))
    parser.add_argument("--tiff-source-node-id", default=os.environ.get("TIFF_SOURCE_NODE_ID"))
    parser.add_argument("--tiff-target-zarr-node-id", default=os.environ.get("TIFF_TARGET_ZARR_NODE_ID"))
    parser.add_argument("--tiff-target-thumb-node-id", default=os.environ.get("TIFF_TARGET_THUMB_NODE_ID"))
    parser.add_argument("--tiff-processor-node-id", default=os.environ.get("TIFF_PROCESSOR_NODE_ID"))
    parser.add_argument("--tiff-thumb-processor-node-id", default=os.environ.get("TIFF_THUMB_PROCESSOR_NODE_ID"))
    parser.add_argument("--tiff-processor-version", default=os.environ.get("TIFF_PROCESSOR_VERSION", "v1.2.4"))
    parser.add_argument("--tiff-thumb-processor-version", default=os.environ.get("TIFF_THUMB_PROCESSOR_VERSION", "v1.3.1"))

    args = parser.parse_args()

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
        # Determine which types to check
        types = []
        if args.nifti:
            types.append("nifti")
        if args.tiff:
            types.append("tiff")
        if args.plain_tiff:
            types.append("plain_tiff")
        if not types:
            types = ["nifti", "tiff", "plain_tiff"]
        check_package_assets(args.api_host, args.session_token, args.refresh_token,
                             args.dataset_id, types)
        return

    # Validate workflow type selection
    # In dataset mode, default to all workflows when no type flag is given
    if args.dataset and not args.nifti and not args.tiff:
        args.nifti = True
        args.tiff = True

    if not args.nifti and not args.tiff and not args.plain_tiff:
        parser.error("At least one workflow type must be specified: --nifti, --tiff, and/or --plain-tiff")

    selected_types = sum([args.nifti, args.tiff, args.plain_tiff])
    if not args.dataset and selected_types > 1:
        parser.error("Cannot use multiple workflow types without --dataset. "
                      "In file/CLI mode, specify exactly one: --nifti, --tiff, or --plain-tiff.")

    # Validate required config
    missing = validate_config(args)
    if missing:
        print(f"Error: Missing required config:\n  " + "\n  ".join(missing), file=sys.stderr)
        print("\nSet these in .env or pass as CLI args.", file=sys.stderr)
        sys.exit(1)

    # Collect package IDs
    nifti_package_ids = []
    tiff_package_ids = []
    plain_tiff_package_ids = []

    if args.dataset:
        print(f"Discovering packages from dataset {args.dataset_id}...")
        packages = fetch_dataset_packages(args.api_host, args.session_token, args.refresh_token, args.dataset_id)

        skipped = []
        for pkg in packages:
            file_type = classify_package(pkg["name"])
            if file_type == "nifti" and args.nifti:
                nifti_package_ids.append(pkg["node_id"])
            elif file_type == "tiff" and args.tiff:
                tiff_package_ids.append(pkg["node_id"])
            elif file_type == "plain_tiff" and args.plain_tiff:
                plain_tiff_package_ids.append(pkg["node_id"])
            else:
                skipped.append(pkg)

        print(f"\nFound {len(packages)} packages total:")
        if args.nifti:
            print(f"  NIfTI:      {len(nifti_package_ids)}")
        if args.tiff:
            print(f"  OME-TIFF:   {len(tiff_package_ids)}")
        if args.plain_tiff:
            print(f"  Plain TIFF: {len(plain_tiff_package_ids)}")
        if skipped:
            print(f"  Skipped:    {len(skipped)}")
        print()

        if not nifti_package_ids and not tiff_package_ids and not plain_tiff_package_ids:
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

        if args.nifti:
            nifti_package_ids = package_ids
        elif args.tiff:
            tiff_package_ids = package_ids
        else:
            plain_tiff_package_ids = package_ids

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
        if nifti_package_ids:
            nifti_config = build_nifti_config(args)
            s, f = run_workflow(args, nifti_config, nifti_package_ids, "NIfTI", log_file)
            total_succeeded += s
            total_failed += f

        if tiff_package_ids:
            tiff_config = build_tiff_config(args)
            s, f = run_workflow(args, tiff_config, tiff_package_ids, "OME-TIFF", log_file)
            total_succeeded += s
            total_failed += f

        if plain_tiff_package_ids:
            plain_tiff_config = build_tiff_config(args)
            s, f = run_workflow(args, plain_tiff_config, plain_tiff_package_ids, "Plain TIFF", log_file)
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
