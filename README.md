# Pennsieve Workflow Scripts

Trigger Pennsieve workflow runs in bulk for a list of package IDs. Workflow types are defined in `workflows.yaml` — add new types there without changing any code.

## Common Runs

```bash
# Run a workflow on a list of package IDs from a file
python trigger_workflows.py batches/nifti_2026-09-01.txt -w nifti

# Discover packages from a dataset and save to batches/ for later
python trigger_workflows.py --list-packages -w nifti

# Discover and immediately run all workflows on a dataset
python trigger_workflows.py --dataset

# Discover and run only NIfTI packages
python trigger_workflows.py --dataset -w nifti

# Dry run — see what would be triggered without doing it
python trigger_workflows.py --dataset -w nifti --dry-run

# Check statuses from a previous run
python trigger_workflows.py --status logs/runs_2026-09-01_143022.log

# Check which packages are missing viewer assets
python trigger_workflows.py --asset-check -w nifti
```

## Built-in Workflows

| Name | Label | Extensions |
|------|-------|------------|
| `nifti` | NIfTI | `.nii.gz`, `.nii` |
| `tiff` | OME-TIFF | `.ome.tiff`, `.ome.tif` |
| `plain_tiff` | Plain TIFF | `.tiff`, `.tif` |
| `timeseries` | Timeseries | `.edf`, `.mef`, `.eeg` |

## Setup

```bash
pip install -r requirements.txt
cp examples/.env.example .env
# Edit .env with your credentials and workflow config
```

See `examples/.env.example` for all available env vars.

## Usage

Use `-w`/`--workflow` to select which workflow(s) to run. In `--dataset` mode, all workflows run by default if no `-w` is given.

### Run from a batch file

The most basic run — point a workflow at a text file of package IDs:

```bash
python trigger_workflows.py batches/nifti_2026-09-01.txt -w nifti
```

Each package gets triggered with a 30s delay between runs (configurable via `DELAY_BETWEEN_RUNS` in `.env`).

### Discover and save package lists

Use `--list-packages` to discover packages from a dataset and save their IDs to `batches/` without triggering anything:

```bash
# Save all workflow types
python trigger_workflows.py --list-packages

# Save only NIfTI package IDs
python trigger_workflows.py --list-packages -w nifti
```

Output files are saved as `batches/<workflow>_<timestamp>.txt`. You can then review the list and run it later:

```bash
python trigger_workflows.py batches/nifti_2026-09-01_143022.txt -w nifti
```

### Auto-discover and run immediately

Use `--dataset` to discover packages and trigger workflows in one step:

```bash
# All workflows
python trigger_workflows.py --dataset

# Specific workflows
python trigger_workflows.py --dataset -w nifti -w tiff
```

### Check run statuses

```bash
python trigger_workflows.py --status logs/runs_2026-09-01_143022.log
```

### Check which packages are missing assets

```bash
# All workflow types
python trigger_workflows.py --asset-check

# Specific workflow
python trigger_workflows.py --asset-check -w nifti
```

### Other options

```bash
# Dry run — preview without triggering
python trigger_workflows.py --dataset -w nifti --dry-run

# Stop on auth errors and print remaining packages
python trigger_workflows.py batches/ids.txt -w nifti --stop-on-auth-error

# Custom config file
python trigger_workflows.py --dataset --config path/to/workflows.yaml
```

## Adding a New Workflow Type

1. Add a block to `workflows.yaml`:
   ```yaml
   microct:
     label: "MicroCT"
     extensions: [".dcm", ".dicom"]
     package_types: ["MRI"]
     env_vars:
       workflow_id: "MICROCT_WORKFLOW_ID"
       compute_node_id: "MICROCT_COMPUTE_NODE_ID"
       source_node_id: "MICROCT_SOURCE_NODE_ID"
       target_zarr_node_id: "MICROCT_TARGET_ZARR_NODE_ID"
       target_thumb_node_id: "MICROCT_TARGET_THUMB_NODE_ID"
       converter_processor_node_id: "MICROCT_PROCESSOR_NODE_ID"
       thumb_processor_node_id: "MICROCT_THUMB_PROCESSOR_NODE_ID"
       converter_processor_version: "MICROCT_PROCESSOR_VERSION"
       thumb_processor_version: "MICROCT_THUMB_PROCESSOR_VERSION"
     defaults:
       converter_processor_version: "v1.0.0"
       thumb_processor_version: "v1.3.1"
   ```
2. Add the corresponding env vars to `.env`
3. Run: `python trigger_workflows.py --dataset -w microct --dry-run`

Use `alias_of` to share another workflow's config (e.g. `plain_tiff` reuses `tiff`'s converter).

## Logging

Non-dry-run executions automatically log to `logs/runs_<timestamp>.log`. Use `--status <logfile>` to check run results afterward.
