# Pennsieve Workflow Scripts

Trigger Pennsieve workflow runs in bulk for a list of package IDs.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials and workflow config
```

## Usage

```bash
# From a file (one package ID per line)
python trigger_workflows.py package_ids.txt

# From CLI args
python trigger_workflows.py --package-id PKG1 --package-id PKG2

# Override config at runtime
python trigger_workflows.py package_ids.txt --workflow-id UUID --dataset-id UUID

# Preview without triggering
python trigger_workflows.py package_ids.txt --dry-run
```

All config can be set via `.env`, environment variables, or CLI flags. CLI flags take precedence.
