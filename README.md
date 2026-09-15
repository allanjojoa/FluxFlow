# FluxFlow

**Modular release management for Talend, Snowflake, and Tableau.**

FluxFlow provides a config-driven, two-stage (build → deploy) workflow to manage task releases across cloud data platforms. Each platform is supported via independent connectors that share a common interface.

## Features

- **Config-driven** — Define tasks, artifacts, versions, parameters, and connection settings in YAML
- **Environment-aware** — Tokens and workspace IDs scoped per environment (dev / qa / prod)
- **Two-stage workflow** — `build` detects changes, `deploy` applies them
- **Artifact promotion** — Automatically promotes artifacts across environments (dev → qa → prod)
- **Dry-run mode** — Preview deployments without making API calls
- **Rich CLI output** — Colored tables, progress indicators, and clear diff summaries

## Quick Start

```bash
# Install
pip install -r requirements.txt
pip install -e .

# Set your Talend API tokens
export TALEND_TOKEN_DEV="your-dev-token"
export TALEND_TOKEN_QA="your-qa-token"
export TALEND_TOKEN_PROD="your-prod-token"

# Build: detect changes against remote state
fluxflow build --env dev

# Deploy: preview what would change (dry-run)
fluxflow deploy --env dev --latest --dry-run

# Deploy: apply changes for real
fluxflow deploy --env dev --latest
```

## Configuration

### Environment Config (`configs/env_config.yaml`)

```yaml
promotion_chain: [dev, qa, prod]

environments:
  dev:
    region: "us"                        # base_url is auto-derived from region
    token: "${TALEND_TOKEN_DEV}"        # Resolved from env vars
    workspace_id: "ws-dev-xxxx"
  qa:
    region: "us"
    token: "${TALEND_TOKEN_QA}"
    workspace_id: "ws-qa-xxxx"
  prod:
    region: "eu"
    token: "${TALEND_TOKEN_PROD}"
    workspace_id: "ws-prod-xxxx"
```

### Release Config (`configs/release_config.yaml`)

```yaml
connector: talend

tasks:
  - name: "ETL_Customer_Load"
    artifact:
      name: "customer-pipeline"
      version: "2.3.1"
    parameters:
      source_database: "raw_db"
      batch_size: "5000"
    studio_connection:
      type: "CLOUD"
    processing:
      runtime: "Standard"
      log_level: "INFO"
      timeout_minutes: 60
```

## Architecture

```
FluxFlow/
├── fluxflow/
│   ├── cli.py                    # CLI entry point
│   ├── core/
│   │   ├── config.py             # Config loader & validator
│   │   ├── builder.py            # Build stage orchestrator
│   │   ├── deployer.py           # Deploy stage orchestrator
│   │   └── models.py             # Shared data models
│   └── connectors/
│       ├── base.py               # Abstract connector interface
│       └── talend/               # Talend Cloud connector
│           ├── client.py         # REST API client
│           ├── builder.py        # Talend connector (diff + deploy)
│           └── deployer.py       # Re-export module
├── configs/                      # YAML configuration files
├── builds/                       # Generated build manifests (gitignored)
└── tests/                        # Test suite
```

## How It Works

### Build Stage
1. Reads the release config and fetches all tasks from the remote Talend workspace
2. Matches each config task by **name** to decide create vs update
3. Diffs artifact versions, parameters, studio_connection, and processing config
4. Checks artifact availability — if not found, identifies the source environment in the promotion chain
5. Writes a build manifest JSON to `builds/`

### Deploy Stage
1. Loads the build manifest
2. Promotes any required artifacts from the source environment
3. Creates new tasks or updates existing ones via the Talend API
4. Prints a deploy report with per-task status

## Running Tests

```bash
python -m pytest tests/ -v
```

## License

MIT
