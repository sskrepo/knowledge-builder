---
title: OCI Deployment Runbook — Knowledge Builder Framework
created: 2026-05-10
owner: architect
tags: [engineering, runbook, oci, deployment, production]
status: current
---

# OCI Deployment Runbook — Knowledge Builder Framework

This is the step-by-step guide for deploying the framework on an OCI Compute VM.
Follow it from start to finish to go from an empty OCI tenancy to a live framework
accepting MCP connections.

**Target state:**
```
OCI Compute VM (VM.Standard.E4.Flex, 4 OCPU, 32 GB RAM)
├── Nginx (TLS termination, port 443 → 8080)
├── framework-api.service (FastAPI + 4 Uvicorn workers, port 8080)
├── framework-ingestion.service (webhook receiver + ingestion worker)
└── framework-scheduler.service (cron dispatcher for ON_SCHEDULE skills)

Network connections out:
├── Oracle 23ai ADB (wallet-based TLS)
├── OCI GenAI Inference (instance principal auth)
├── Confluence / Jira (HTTPS + bearer token from Vault)
├── Git repo (SSH)
└── UDAP / Sentinel (internal network, read-through)
```

**What will NOT work without this runbook:** All `STORAGE_BACKEND=oracle_adb` paths,
real vector search, real LLM synthesis, Confluence/Jira ingestion, and SSE transport
for remote MCP clients. Local laptop mode (filestore + stub LLM) continues to work
without any of this; see `docs/wiki/engineering/laptop-quickstart.md`.

---

## Sections

1. [Prerequisites](#1-prerequisites)
2. [OCI Infrastructure Setup](#2-oci-infrastructure-setup)
3. [VM Setup](#3-vm-setup)
4. [Configuration](#4-configuration)
5. [Database Schema Setup](#5-database-schema-setup)
6. [First Deployment](#6-first-deployment)
7. [MCP Client Configuration](#7-mcp-client-configuration)
8. [Ongoing Operations](#8-ongoing-operations)
9. [Troubleshooting](#9-troubleshooting)
10. [Configuration Reference](#10-configuration-reference)

---

## 1. Prerequisites

### 1.1 OCI tenancy requirements

You need:
- An OCI tenancy with an IAM administrator or equivalent role
- A compartment created for this project (call it `kb-framework` throughout this guide)
  — never deploy into the root compartment
- OCI CLI configured on your local machine (`oci setup config` if not already done)
- Your tenancy OCID, compartment OCID, and region noted for later steps

To get your compartment OCID after creating it:
```
OCI Console → Identity & Security → Compartments → [your compartment] → OCID → Copy
```

### 1.2 OCI services provisioned in this guide

| Service | Purpose | Cost note |
|---------|---------|-----------|
| Compute (VM.Standard.E4.Flex) | Runs the framework (API, ingestion, scheduler) | ~$0.025/OCPU-hour |
| Oracle 23ai ADB | Vector store, wiki metadata, graph, shim tables, session state | Always Free 20 GB for dev; paid for prod |
| OCI Vault | API keys, bearer tokens, DB passwords | ~$0.0017/secret/month |
| OCI Generative AI Inference | LLM + embeddings | Per-token pricing; see OCI pricing page |
| OCI Object Storage | Workflow skill output artifacts, eval baselines | ~$0.0255/GB/month |
| OCI Email Delivery | Skill output email deliveries | Per-message pricing |

### 1.3 Tools needed on your local machine

```bash
# OCI CLI
brew install oci    # macOS
# or: https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm

oci setup config    # if not already done — needs tenancy OCID, region, API signing key

# Verify OCI auth works
oci iam compartment list --all --query 'data[0].id' --raw-output

# SSH client (built into macOS/Linux)
ssh -V

# yq (for bootstrap-vault.sh)
brew install yq     # macOS
# or: https://github.com/mikefarah/yq#install
```

### 1.4 Network requirements

The OCI VCN you create must have:
- One public subnet (for the Compute VM NIC)
- A security list with these rules (set up in section 2.2):

Ingress:
| Port | Protocol | Source | Purpose |
|------|----------|--------|---------|
| 22 | TCP | your-office-CIDR | SSH admin access |
| 443 | TCP | 0.0.0.0/0 | HTTPS (Nginx TLS, MCP SSE) |

Egress:
| Port | Destination | Purpose |
|------|-------------|---------|
| 443 | 0.0.0.0/0 | ADB, OCI GenAI, Confluence, Jira, GitHub |
| 1522 | ADB FQDN (see §2.3) | Oracle ADB wallet connection |

Port 8080 does NOT need to be in the security list — Nginx handles TLS termination
and proxies to localhost:8080 internally.

---

## 2. OCI Infrastructure Setup

Work through these subsections in order. Each depends on resources created in the previous.

### 2.1 Compute VM provisioning

Go to: `OCI Console → Compute → Instances → Create Instance`

Settings:
- Name: `kb-framework-prod`
- Compartment: `kb-framework`
- Image: Oracle Linux 8 (preferred) or Ubuntu 22.04
- Shape: `VM.Standard.E4.Flex` — set OCPU count to 4, RAM to 32 GB
- Networking: see 2.2 first, then select the VCN/subnet you create there
- Boot volume: 100 GB (minimum — ingestion cache and logs grow)
- SSH keys: upload your public key (`~/.ssh/id_rsa.pub` or generate a new pair)

After creation, note the **public IP address** of the VM. Referred to as `<VM_PUBLIC_IP>` below.

Verify SSH access before proceeding:
```bash
ssh opc@<VM_PUBLIC_IP>    # Oracle Linux; use ubuntu@ for Ubuntu
```

### 2.2 VCN + subnet + security list

If you don't already have a VCN:
```
OCI Console → Networking → Virtual Cloud Networks → Create VCN
  Name: kb-framework-vcn
  IPv4 CIDR block: 10.0.0.0/16
  Enable DNS resolution: yes
```

Create a public subnet:
```
VCN → Subnets → Create Subnet
  Name: kb-framework-public
  Type: Regional
  IPv4 CIDR: 10.0.0.0/24
  Route table: attach the default route table (0.0.0.0/0 → Internet Gateway)
  Security list: create new (see below)
```

Create the security list:
```
Networking → Security Lists → Create Security List
  Name: kb-framework-sl

  Ingress rules:
    Source: <your-office-CIDR>   Protocol: TCP   Port: 22   Description: SSH
    Source: 0.0.0.0/0            Protocol: TCP   Port: 443  Description: HTTPS MCP

  Egress rules:
    Destination: 0.0.0.0/0   Protocol: TCP   Port Range: 443   Description: Outbound HTTPS
    Destination: 0.0.0.0/0   Protocol: TCP   Port: 1522        Description: Oracle ADB
```

Assign the security list to the subnet after creating both.

### 2.3 Oracle 23ai Autonomous Database provisioning

Go to: `OCI Console → Oracle Database → Autonomous Database → Create Autonomous Database`

Settings:
- Display name: `kbf-prod`
- Database name: `KBFPROD`
- Workload type: Transaction Processing
- Deployment type: Shared Infrastructure
- Always Free: NO for production (Always Free lacks vector index support at scale)
- OCPU count: 2 (scale up later if needed)
- Storage: 1 TB
- Auto scaling: enabled
- Database version: 23ai (select if available; otherwise 21c with JSON Relational Duality)
- Password: generate a strong admin password — save it as `adb-admin-prod` in Vault later
- Network access: Secure access from everywhere (the VM uses wallet-based TLS — no IP allowlisting needed at the ADB level)
- License: License Included

After provisioning (takes 3-5 minutes):

**Download the wallet:**
```
ADB Console → [your DB] → Database Connection → Download Wallet
  Password: choose a wallet password (separate from admin password)
  Download ZIP
```

Save the wallet ZIP — you upload it to the VM in section 3.4.

**Note the connection string:**
```
ADB Console → Database Connection → Connection Strings
  TLS Authentication: mTLS (Mutual TLS)
  Service: kbfprod_high (or kbf_prod_high depending on naming)
```

The service name appears in `tnsnames.ora` inside the wallet ZIP. Use the `_high` service for
the MCP server (OLTP workload) and `_low` for ingestion batch jobs.

### 2.4 OCI Vault setup

Go to: `OCI Console → Identity & Security → Vault → Create Vault`

Settings:
- Name: `kb-framework-vault`
- Compartment: `kb-framework`
- Vault type: Default (Virtual Private Vault for high security, but Default is fine for most teams)

After creating the vault:

Create a master encryption key:
```
Vault → [your vault] → Master Encryption Keys → Create Key
  Name: kb-framework-key
  Key shape: AES, 256 bits
  Protection mode: HSM
```

Note both OCIDs:
```bash
# Vault OCID
oci kms vault list --compartment-id <compartment-ocid> \
  --query 'data[?contains("display-name",`kb-framework-vault`)].id | [0]' \
  --raw-output

# Key OCID (use Management Endpoint from vault detail page)
oci kms management key list \
  --compartment-id <compartment-ocid> \
  --endpoint <management-endpoint> \
  --query 'data[?contains("display-name",`kb-framework-key`)].id | [0]' \
  --raw-output
```

These OCIDs go into `framework/config/prod.yaml` under `vault.vault_ocid` and `vault.master_key_ocid`.

The actual secrets are created later in section 4.4 using `framework/scripts/bootstrap-vault.sh`.

### 2.5 OCI Generative AI Inference setup

Go to: `OCI Console → AI & Machine Learning → Generative AI`

Confirm the service is enabled in your region. As of 2026, it is available in:
`us-ashburn-1`, `us-chicago-1`, `eu-frankfurt-1`, `uk-london-1`, `ap-mumbai-1`, `ap-tokyo-1`, `ap-sydney-1`

If your region is not listed, use the closest available region for GenAI and keep your ADB
in your primary region. The framework supports a split configuration (see `framework/config/adapters/llm.yaml`).

The endpoint URL for your region follows the pattern:
```
https://inference.generativeai.<region>.oci.oraclecloud.com
```

For `us-ashburn-1`, this is:
```
https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com
```

**No explicit provisioning is required** — the VM uses Instance Principal auth (configured in section 3.6),
and the framework sends requests to this endpoint. You need only to ensure:
1. Your compartment policy allows Instance Principal to call OCI GenAI (see policy in section 2.6)
2. The endpoint URL is set correctly in `framework/config/adapters/llm.yaml` and `prod.yaml`

Model names the framework uses (from `framework/config/adapters/llm.yaml`):
```yaml
models:
  chat: openai.gpt-4o
  synthesis: openai.gpt-4o
  eval_judge: openai.gpt-4o
  embedding: openai.text-embedding-3-large
```

These are OCI GenAI model aliases for the OpenAI-compatible models available via
the OCI GenAI service. Verify availability in your region before proceeding.

### 2.6 IAM policies for Instance Principal

The VM uses Instance Principal auth to call OCI Vault, GenAI, and Object Storage without
managing credentials on the VM itself.

Create a Dynamic Group:
```
OCI Console → Identity & Security → Dynamic Groups → Create Dynamic Group
  Name: kb-framework-compute
  Description: KBF Compute VM instances
  Matching rules:
    Any {instance.compartment.id = '<compartment-ocid>'}
```

Create a Policy:
```
OCI Console → Identity & Security → Policies → Create Policy
  Name: kb-framework-vm-policy
  Compartment: kb-framework
  Statements (add each as a separate statement):

  Allow dynamic-group kb-framework-compute to read secret-bundles in compartment kb-framework
  Allow dynamic-group kb-framework-compute to use generative-ai-inference-family in compartment kb-framework
  Allow dynamic-group kb-framework-compute to manage objects in compartment kb-framework where target.bucket.name = 'kb-raw-prod'
  Allow dynamic-group kb-framework-compute to read autonomous-databases in compartment kb-framework
```

### 2.7 OCI Object Storage bucket

Go to: `OCI Console → Storage → Object Storage → Create Bucket`

Settings:
- Bucket name: `kb-raw-prod`
- Compartment: `kb-framework`
- Visibility: Private
- Storage tier: Standard
- Encryption: Oracle-managed

Note the namespace (shown on the bucket detail page under "Namespace"). This goes
into `framework/config/prod.yaml` under `object_storage.namespace`.

Set lifecycle policy to archive objects older than 365 days (optional but recommended):
```
Bucket → Lifecycle Policy Rules → Create Rule
  Name: archive-old-artifacts
  Action: Archive (not Delete — retain for audit)
  After: 365 days
  Object name prefix: eval/
```

---

## 3. VM Setup

All commands in this section run on the OCI VM via SSH unless noted otherwise.

### 3.1 OS packages

```bash
# On Oracle Linux 8:
sudo dnf update -y
sudo dnf install -y python3.11 python3.11-devel python3.11-pip \
  git nginx certbot certbot-nginx \
  poppler-utils \
  gcc gcc-c++ make

# Verify Python version
python3.11 --version   # must be 3.11.x or higher

# Set python3 to point to 3.11
sudo alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

# On Ubuntu 22.04 instead:
# sudo apt update && sudo apt install -y python3.11 python3.11-dev python3.11-pip \
#   git nginx certbot python3-certbot-nginx poppler-utils build-essential
```

### 3.2 Clone the repo

```bash
# Create the deployment directory
sudo mkdir -p /opt/kbf
sudo chown opc:opc /opt/kbf

# Clone (replace with your actual repo URL)
cd /opt/kbf
git clone git@github.com:<your-org>/Knowledgebase.git app
cd app

# Verify the framework directory exists
ls framework/   # should show: adapters/ cli/ config/ core/ deploy/ ...
```

If your repo is private, set up an SSH deploy key:
```bash
# On the VM, generate a key pair
ssh-keygen -t ed25519 -C "kb-framework-deploy" -f ~/.ssh/deploy_key -N ""

# Copy the public key to your GitHub/GitLab repo as a read-only deploy key
cat ~/.ssh/deploy_key.pub   # paste into repo Settings → Deploy Keys

# Tell SSH to use this key for your repo host
cat >> ~/.ssh/config << 'EOF'
Host github.com
  IdentityFile ~/.ssh/deploy_key
  StrictHostKeyChecking accept-new
EOF
```

### 3.3 Python venv + pip install

```bash
cd /opt/kbf/app

# Create venv using Python 3.11
python3.11 -m venv .venv
source .venv/bin/activate

# Install framework + dependencies
pip install --upgrade pip
pip install -e framework/
pip install -r framework/requirements.txt

# Install document rendering libs (for PPT/DOCX skill outputs)
pip install python-pptx python-docx

# Verify key packages installed
python -c "import fastapi, oracledb, oci; print('ok')"
```

Expected output: `ok`

### 3.4 Oracle Instant Client installation

The `oracledb` Python package (version >= 2.2, thick mode) requires Oracle Instant Client.

```bash
# On Oracle Linux 8 — install from Oracle's DNF repo
sudo dnf install -y oracle-instantclient-release-el8
sudo dnf install -y oracle-instantclient-basic oracle-instantclient-sqlplus

# Verify
sqlplus -V   # should print: SQL*Plus: Release 21.x.x.x ...

# Set library path permanently (add to /etc/profile.d/oracle.sh)
sudo bash -c 'cat > /etc/profile.d/oracle.sh << EOF
export LD_LIBRARY_PATH=/usr/lib/oracle/21/client64/lib:\$LD_LIBRARY_PATH
export PATH=/usr/lib/oracle/21/client64/bin:\$PATH
EOF'
source /etc/profile.d/oracle.sh
```

On Ubuntu, download directly from Oracle:
```bash
# https://www.oracle.com/database/technologies/instant-client/linux-x86-64-downloads.html
# Download basic + sqlplus packages and install via dpkg
sudo dpkg -i oracle-instantclient-basic_21.x_amd64.deb
sudo dpkg -i oracle-instantclient-sqlplus_21.x_amd64.deb
sudo sh -c 'echo /usr/lib/oracle/21/client64/lib > /etc/ld.so.conf.d/oracle.conf'
sudo ldconfig
```

### 3.5 Wallet configuration

Upload the ADB wallet ZIP you downloaded in section 2.3 to the VM:

```bash
# From your local machine
scp ~/Downloads/Wallet_KBFPROD.zip opc@<VM_PUBLIC_IP>:/tmp/

# On the VM
sudo mkdir -p /opt/kbf/wallet/prod
sudo unzip /tmp/Wallet_KBFPROD.zip -d /opt/kbf/wallet/prod/
sudo chmod 700 /opt/kbf/wallet/prod/
sudo chmod 600 /opt/kbf/wallet/prod/*
sudo chown -R opc:opc /opt/kbf/wallet/
rm /tmp/Wallet_KBFPROD.zip

# Verify the wallet contents
ls /opt/kbf/wallet/prod/
# Expected: cwallet.sso  ewallet.p12  ewallet.pem  keystore.jks  ojdbc.properties
#           sqlnet.ora  tnsnames.ora  truststore.jks
```

Edit `sqlnet.ora` to point to the correct wallet directory:
```bash
# The existing content will have a placeholder path — replace it
sudo sed -i "s|WALLET_LOCATION.*|WALLET_LOCATION = (SOURCE = (METHOD = FILE) (METHOD_DATA = (DIRECTORY = \"/opt/kbf/wallet/prod\")))|" \
  /opt/kbf/wallet/prod/sqlnet.ora
```

Set the environment variable:
```bash
echo 'export TNS_ADMIN=/opt/kbf/wallet/prod' | sudo tee -a /etc/profile.d/kbf.sh
source /etc/profile.d/kbf.sh
```

Test the connection (use the admin password from section 2.3):
```bash
sqlplus ADMIN/"<admin-password>"@kbfprod_high
# Expected: Connected to: Oracle Database 23ai ...
# Type: exit
```

### 3.6 Nginx reverse proxy config

```bash
# Create the site config
sudo bash -c 'cat > /etc/nginx/conf.d/kbf.conf << '"'"'EOF'"'"'
upstream kbf_api {
    server 127.0.0.1:8080;
    keepalive 32;
}

server {
    listen 80;
    server_name <your-hostname>;

    # Let certbot handle ACME challenges
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    # Redirect all other HTTP to HTTPS
    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name <your-hostname>;

    ssl_certificate     /etc/letsencrypt/live/<your-hostname>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<your-hostname>/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # Health check — no auth required
    location /healthz {
        proxy_pass         http://kbf_api/healthz;
        proxy_set_header   Host $host;
        proxy_read_timeout 10s;
    }

    # MCP SSE endpoint — long-lived connection
    location /mcp {
        proxy_pass             http://kbf_api/mcp;
        proxy_set_header       Host $host;
        proxy_set_header       X-Real-IP $remote_addr;
        proxy_set_header       Connection "";
        proxy_http_version     1.1;
        proxy_buffering        off;
        proxy_read_timeout     3600s;    # SSE connections stay open
        proxy_send_timeout     3600s;
        chunked_transfer_encoding on;
    }

    # REST API
    location /api/ {
        proxy_pass         http://kbf_api/api/;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }

    # Webhook receivers
    location /webhooks/ {
        proxy_pass         http://kbf_api/webhooks/;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 30s;
    }
}
EOF'

# Replace <your-hostname> with the actual hostname or public IP FQDN
# If using a static IP with no DNS, you can use a self-signed cert instead (see Troubleshooting §9.8)

# Obtain a TLS certificate (requires DNS pointing to the VM's public IP)
sudo certbot --nginx -d <your-hostname> --non-interactive --agree-tos -m <admin-email>

# Start and enable Nginx
sudo systemctl enable nginx
sudo systemctl start nginx
sudo systemctl status nginx   # must show active (running)
```

### 3.7 Systemd service files

Create three services: the API server, the ingestion worker, and the scheduler.

**framework-api.service:**
```bash
sudo bash -c 'cat > /etc/systemd/system/framework-api.service << '"'"'EOF'"'"'
[Unit]
Description=Knowledge Builder Framework — FastAPI MCP Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=opc
Group=opc
WorkingDirectory=/opt/kbf/app
EnvironmentFile=/opt/kbf/app/framework/.env
ExecStart=/opt/kbf/app/.venv/bin/uvicorn framework.deploy.mcp_server:app \
    --host 127.0.0.1 \
    --port 8080 \
    --workers 4 \
    --log-level info \
    --access-log
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kbf-api

[Install]
WantedBy=multi-user.target
EOF'
```

**framework-ingestion.service:**
```bash
sudo bash -c 'cat > /etc/systemd/system/framework-ingestion.service << '"'"'EOF'"'"'
[Unit]
Description=Knowledge Builder Framework — Ingestion Worker
After=network-online.target framework-api.service
Wants=network-online.target

[Service]
Type=simple
User=opc
Group=opc
WorkingDirectory=/opt/kbf/app
EnvironmentFile=/opt/kbf/app/framework/.env
ExecStart=/opt/kbf/app/.venv/bin/python -m framework.ingestion.worker
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kbf-ingestion

[Install]
WantedBy=multi-user.target
EOF'
```

**framework-scheduler.service:**
```bash
sudo bash -c 'cat > /etc/systemd/system/framework-scheduler.service << '"'"'EOF'"'"'
[Unit]
Description=Knowledge Builder Framework — Workflow Skill Scheduler
After=network-online.target framework-api.service
Wants=network-online.target

[Service]
Type=simple
User=opc
Group=opc
WorkingDirectory=/opt/kbf/app
EnvironmentFile=/opt/kbf/app/framework/.env
ExecStart=/opt/kbf/app/.venv/bin/python -m framework.workflow_runtime.trigger_dispatcher
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kbf-scheduler

[Install]
WantedBy=multi-user.target
EOF'
```

Reload systemd (do not start the services yet — do that in section 6):
```bash
sudo systemctl daemon-reload
```

---

## 4. Configuration

### 4.1 Fill in prod.yaml

Edit the production config. The file is at `framework/config/prod.yaml` in the repo checkout.
Replace every `YOUR_*` placeholder:

```bash
cd /opt/kbf/app
$EDITOR framework/config/prod.yaml
```

Changes to make (all other values are already correct defaults):

```yaml
# Line 4: wallet path on the VM
adb:
  service_name: kbfprod_high        # must match the service name in tnsnames.ora
  wallet_path: /opt/kbf/wallet/prod/

# Lines 15-17: fill in real OCIDs from your tenancy
vault:
  vault_ocid: ocid1.vault.oc1.iad.<real-ocid>
  master_key_ocid: ocid1.key.oc1.iad.<real-ocid>
  compartment_ocid: ocid1.compartment.oc1..<real-ocid>

# Lines 19-20: fill in your tenancy namespace and confirm bucket name
object_storage:
  namespace: <tenancy-namespace>     # shown on any bucket detail page in the console
  bucket: kb-raw-prod

# Line 24: OCI Streaming OCID (if using streaming for incremental ingest)
streaming:
  stream_ocid: ocid1.stream.oc1.iad.<real-ocid>

# Line 28: your VM's public hostname
compute:
  mcp_server_endpoint: https://<your-hostname>

# Line 33: confirm your OCI GenAI endpoint URL for your region
oci_genai:
  endpoint: https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com
  auth: instance_principal          # do NOT change — uses the Dynamic Group policy from §2.6
```

### 4.2 Fill in adapter configs

Edit Confluence adapter (`framework/config/adapters/confluence.yaml`):
```yaml
native:
  base_url: https://confluence.your-company.internal    # your Confluence URL
```

Edit Jira adapter (`framework/config/adapters/jira.yaml`):
```yaml
native:
  base_url: https://jira.your-company.internal          # your Jira URL
```

Edit Git adapter (`framework/config/adapters/git.yaml`):
```yaml
ssh_key_secret_default: vault://kb/git-readonly    # leave as-is; secret goes in Vault
clone_cache_path: /var/lib/kb/git-cache
```

Create the git cache directory:
```bash
sudo mkdir -p /var/lib/kb/git-cache
sudo chown opc:opc /var/lib/kb/git-cache
```

### 4.3 Create the .env file

The `.env` file is read by all three systemd services via `EnvironmentFile`. It contains
the runtime environment variables (NOT secrets — those stay in Vault).

```bash
cat > /opt/kbf/app/framework/.env << 'EOF'
# Framework runtime environment
KBF_ENV=prod
KBF_CONFIG=framework/config/prod.yaml
KBF_LLM_PROVIDER=oci_genai

# Storage backend — use ADB for prod (not filestore)
STORAGE_BACKEND=oracle_adb

# Oracle wallet
TNS_ADMIN=/opt/kbf/wallet/prod

# OCI GenAI settings (also in prod.yaml, but available here for process env)
OCI_GENAI_ENDPOINT=https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com
OCI_GENAI_COMPARTMENT_ID=ocid1.compartment.oc1..<your-compartment-ocid>

# Secrets backend — use OCI Vault in prod
KBF_SECRETS_BACKEND=vault

# Instance principal auth for OCI SDK (no creds file needed on the VM)
OCI_AUTH_METHOD=instance_principal

# Log level
KBF_LOG_LEVEL=INFO

# MCP transport mode for the server process
MCP_TRANSPORT=sse
MCP_PORT=8080

# Python path (needed since we run as a module)
PYTHONPATH=/opt/kbf/app
EOF

chmod 600 /opt/kbf/app/framework/.env
```

### 4.4 Provision secrets in Vault

Run the bootstrap script. It reads the `prod.yaml` config, discovers all required secret slugs
(by scanning `vault://kb/*` references), checks which already exist in Vault, and prompts for
values for any that are missing.

```bash
cd /opt/kbf/app
source .venv/bin/activate

# Ensure OCI CLI is configured and can reach Vault
oci vault secret list --compartment-id <compartment-ocid> --limit 1

# Run the bootstrap walker
./framework/scripts/bootstrap-vault.sh prod
```

The script will prompt you for each secret. Required secrets for production:

| Secret slug | What it is | Where to get it |
|-------------|-----------|----------------|
| `adb-admin-prod` | ADB ADMIN password | The password you set when provisioning ADB in §2.3 |
| `kb-incidents-rw-prod` | KB_INCIDENTS_RW schema password | You set this; must match what you use in schema setup §5 |
| `kb-wiki-rw-prod` | KB_WIKI_RW schema password | Same |
| `kb-code-rw-prod` | KB_CODE_RW schema password | Same |
| `kb-fa-rw-prod` | KB_FA_RW schema password | Same |
| `kb-shim-rw-prod` | KB_SHIM_RW schema password | Same |
| `confluence-readonly` | Confluence PAT | Confluence → Profile → Personal Access Tokens |
| `jira-readonly` | Jira PAT | Jira → Profile → Security → API Tokens |
| `confluence-webhook-secret` | HMAC secret for Confluence webhooks | Generate: `openssl rand -hex 32` |
| `jira-webhook-secret` | HMAC secret for Jira webhooks | Generate: `openssl rand -hex 32` |
| `git-readonly` | SSH private key for git repo access | Contents of `~/.ssh/deploy_key` (from §3.2) |
| `openai-api-key` | OpenAI API key (fallback only) | platform.openai.com → API keys |

The `openai-api-key` is optional if you rely exclusively on OCI GenAI. The framework
falls back to it when `KBF_LLM_PROVIDER=openai_direct` only.

After bootstrap completes, verify:
```bash
python3 framework/scripts/check-config.py --env prod
# Expected: "all green" or a list of any remaining issues
```

### 4.5 Create bearer tokens for MCP consumers

Bearer tokens are stored in Vault and loaded at server startup into a consumer manifest cache.
Create a token for each engineer or Claude Code instance that will connect to the framework.

```bash
# After the server is running (section 6), use kb-cli:
python -m framework.cli.kb_cli token create \
  --consumer "claude-code-sravan" \
  --scopes read,write \
  --persona-allowlist "tpm,ops_eng,pm,architect" \
  --rpm-cap 60

# The command prints the bearer token — save it securely.
# It is stored in Vault under vault://kb/bearer-<consumer-name>

# To revoke later:
python -m framework.cli.kb_cli token revoke --consumer "claude-code-sravan"
```

For read-only consumers (e.g., Aira):
```bash
python -m framework.cli.kb_cli token create \
  --consumer "aira-production" \
  --scopes read \
  --persona-allowlist "ops_eng,ops_mgr" \
  --rpm-cap 120
```

---

## 5. Database Schema Setup

Connect to ADB as ADMIN and run the DDL scripts. All scripts are idempotent
(the `kb-cli migrate` command handles "name already used" gracefully).

### 5.1 Create DB schemas (users)

Connect to ADB:
```bash
sqlplus ADMIN/"<admin-password>"@kbfprod_high
```

Run these CREATE USER statements. Use passwords you stored in Vault in §4.4:
```sql
-- Create schema owners (run as ADMIN)
CREATE USER KB_INCIDENTS_RW IDENTIFIED BY "<kb-incidents-rw-prod password>";
GRANT CONNECT, RESOURCE, UNLIMITED TABLESPACE TO KB_INCIDENTS_RW;

CREATE USER KB_WIKI_RW IDENTIFIED BY "<kb-wiki-rw-prod password>";
GRANT CONNECT, RESOURCE, UNLIMITED TABLESPACE TO KB_WIKI_RW;

CREATE USER KB_CODE_RW IDENTIFIED BY "<kb-code-rw-prod password>";
GRANT CONNECT, RESOURCE, UNLIMITED TABLESPACE TO KB_CODE_RW;

CREATE USER KB_FA_RW IDENTIFIED BY "<kb-fa-rw-prod password>";
GRANT CONNECT, RESOURCE, UNLIMITED TABLESPACE TO KB_FA_RW;

CREATE USER KB_SHIM_RW IDENTIFIED BY "<kb-shim-rw-prod password>";
GRANT CONNECT, RESOURCE, UNLIMITED TABLESPACE TO KB_SHIM_RW;

EXIT;
```

### 5.2 Run DDL scripts via kb-cli

```bash
cd /opt/kbf/app
source .venv/bin/activate

# Run migrations for each schema
python -m framework.cli.kb_cli migrate --schema kb_incidents --env prod
python -m framework.cli.kb_cli migrate --schema kb_wiki_meta --env prod
python -m framework.cli.kb_cli migrate --schema kb_code --env prod
python -m framework.cli.kb_cli migrate --schema kb_fa_semantic --env prod
python -m framework.cli.kb_cli migrate --schema kb_shim --env prod
```

Each command runs the corresponding DDL file from `framework/stores/sql/` and logs
"ORA-00955 name already used by an existing object" if already run — this is safe to ignore.

The `kb_incidents` migration (`framework/stores/sql/kb_incidents.sql`) creates:
- `content_items` table with full metadata columns and multi-valued JSON indexes
- `chunks` table with `VECTOR(3072, FLOAT32)` column
- HNSW vector index (`ix_chunks_embedding_hnsw`) with cosine distance, M=32, efConstruction=200
- Graph `edges` table
- `batch_insert_datasets_vectors_kbi` procedure for in-DB embedding (ADR-012)

The `kb_shim` migration creates:
- `cost_log` table (cost telemetry per LLM call)
- `author_skill_sessions` table (14-state machine persistence, 7-day TTL)
- `skill_candidates` table (Tier 4 no-answer logging)
- `consumer_tokens` table (bearer token → consumer manifest mapping)

### 5.3 Set up in-DB embedding credential (ADR-012)

The HNSW index is built by `DBMS_VECTOR.UTL_TO_EMBEDDING` which calls OCI GenAI
from inside the database. This requires a DB credential pointing to the GenAI endpoint.

```sql
-- Connect as ADMIN
sqlplus ADMIN/"<admin-password>"@kbfprod_high

-- Create the credential (use your tenancy's Resource Principal or a dedicated IAM key)
BEGIN
  DBMS_CLOUD.CREATE_CREDENTIAL(
    credential_name => 'OCI_VECTOR_CRED',
    user_ocid       => '<your-iam-user-ocid>',
    tenancy_ocid    => '<tenancy-ocid>',
    private_key     => '<RSA private key content — from OCI API key>',
    fingerprint     => '<API key fingerprint>'
  );
END;
/

-- Grant to KB_INCIDENTS_RW so the embedding procedure can run
GRANT EXECUTE ON DBMS_CLOUD TO KB_INCIDENTS_RW;
GRANT EXECUTE ON DBMS_VECTOR TO KB_INCIDENTS_RW;

EXIT;
```

For the IAM user/key to use here, you can create a dedicated API key in OCI Console:
`Identity → Users → [your user] → API Keys → Add API Key`

Download the private key and use its contents (with BEGIN/END RSA PRIVATE KEY markers removed,
on a single line) for the `private_key` parameter above.

### 5.4 Verify schema setup

```bash
python3 framework/scripts/check-config.py --env prod
# Expected: "all green"
```

Also verify the vector store is reachable from Python:
```bash
python3 -c "
import os
os.environ['TNS_ADMIN'] = '/opt/kbf/wallet/prod'
import oracledb
conn = oracledb.connect(
    user='KB_INCIDENTS_RW',
    password='<password>',
    dsn='kbfprod_high',
    config_dir='/opt/kbf/wallet/prod',
    wallet_location='/opt/kbf/wallet/prod',
    wallet_password='<wallet-password>'
)
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM content_items')
print('content_items row count:', cur.fetchone()[0])
conn.close()
print('ok')
"
```

Expected output:
```
content_items row count: 0
ok
```

---

## 6. First Deployment

### 6.1 Start services

```bash
sudo systemctl enable framework-api framework-ingestion framework-scheduler
sudo systemctl start framework-api
# Wait 5 seconds for API to initialize
sleep 5
sudo systemctl start framework-ingestion framework-scheduler
```

Check all three are running:
```bash
sudo systemctl status framework-api framework-ingestion framework-scheduler
```

All three should show `active (running)`. If any fail, check logs:
```bash
sudo journalctl -u framework-api -n 50 --no-pager
```

### 6.2 Verify health

From the VM itself (before DNS/TLS is confirmed working):
```bash
curl -s http://localhost:8080/healthz | python3 -m json.tool
```

Expected response:
```json
{
  "status": "ok",
  "checks": {
    "adb": "ok",
    "ociGenai": "ok",
    "vault": "ok",
    "git": "ok",
    "confluenceAdapter": "ok",
    "jiraAdapter": "ok"
  },
  "uptimeSeconds": 12,
  "version": "1.0.0"
}
```

If any check shows "error" instead of "ok", see Troubleshooting section 9.

From your local machine (via Nginx TLS):
```bash
curl -s https://<your-hostname>/healthz | python3 -m json.tool
```

Same response expected. If this fails but the localhost check worked, the issue is
Nginx TLS configuration — see §9.6.

### 6.3 Run smoke test

```bash
# Get a bearer token first (from §4.5)
TOKEN="<your-bearer-token>"

# Smoke test the ask endpoint
curl -s -X POST "https://<your-hostname>/api/v1/ask" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the Knowledge Builder Framework?", "persona": "ops_eng"}' \
  | python3 -m json.tool
```

Expected: a JSON response with `answer`, `citations`, `confidence`, and `tierUsed` fields.
The answer will be a Tier 4 "no answer" response initially (confidence < 0.40) since no
ingestion has run yet — that is correct behavior.

### 6.4 Test MCP connection from local machine

Configure your local Claude Code MCP settings (see §7 for full details) then verify
the tool list is reachable:

```bash
# From your local machine
curl -s "https://<your-hostname>/mcp/tools/list" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

Expected: a list containing `askKnowledgeBase` and `authorSkill` (plus internal tools
registered for the orchestrator's in-process use).

### 6.5 Run initial ingestion for one persona

Run the ops_eng persona first (it is the Phase 1 exit gate):

```bash
# On the VM
cd /opt/kbf/app
source .venv/bin/activate

# Validate the persona builder config
python -m framework.cli.kb_cli validate framework/persona_builders/ops-eng.yaml

# Dry-run on 5 issues to verify connectivity
python -m framework.cli.kb_cli ingest \
  --dry-run \
  --sample 5 \
  framework/persona_builders/ops-eng.yaml

# If dry-run succeeds, run full ingestion
python -m framework.cli.kb_cli ingest framework/persona_builders/ops-eng.yaml
```

Ingestion logs appear in the system journal:
```bash
sudo journalctl -u kbf-ingestion -f
```

### 6.6 Run eval to establish baseline

```bash
python -m framework.cli.kb_cli eval framework/persona_builders/ops-eng.yaml
```

Eval results are written to `eval/runs/ops-eng-<timestamp>.md` and stored in
OCI Object Storage under `oci://kb-raw-prod/eval/runs/`.

The Phase 1 exit gate is: recall@10 >= 0.80 on `eval/gold_sets/ops-eng.jsonl` (25 questions).

---

## 7. MCP Client Configuration

### 7.1 SSE transport (remote VM — recommended for team use)

This is the mode for engineers connecting from their laptops to the OCI VM via HTTPS.

Add to Claude Code's MCP configuration. On macOS, the file is typically at
`~/.claude/mcp.json` or the user-level Claude Code settings:

```json
{
  "mcpServers": {
    "knowledge-builder": {
      "transport": "sse",
      "url": "https://<your-hostname>/mcp",
      "headers": {
        "Authorization": "Bearer <your-bearer-token>"
      }
    }
  }
}
```

For Cursor or Codex, the same SSE URL and bearer token apply — consult the respective
client's MCP documentation for the config file location and format.

After saving the config, restart Claude Code. The `askKnowledgeBase` and `authorSkill`
tools should appear in the tool palette.

Test from Claude Code by asking:
```
Use the askKnowledgeBase tool to answer: what personas does the knowledge builder support?
```

### 7.2 stdio transport via SSH tunnel (alternative)

If SSE transport has firewall restrictions or you prefer a local connection:

```bash
# Set up SSH tunnel on your local machine (keep this terminal open)
ssh -N -L 8080:localhost:8080 opc@<VM_PUBLIC_IP>
```

Then configure Claude Code to use stdio mode pointing to the tunnel:
```json
{
  "mcpServers": {
    "knowledge-builder": {
      "transport": "sse",
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer <your-bearer-token>"
      }
    }
  }
}
```

Note: this still uses SSE transport, but over the SSH tunnel instead of direct HTTPS.
True stdio mode (`MCP_TRANSPORT=stdio`) is for the laptop quickstart only — the remote
server always runs SSE.

### 7.3 Testing the connection

Verify both MCP tools are visible:

In Claude Code, type:
```
List the available MCP tools.
```

Claude Code should report `askKnowledgeBase` and `authorSkill`.

Verify the consumption tool works:
```
Using askKnowledgeBase, ask: "What incidents touched auth-service in the last 30 days?"
```

Verify the knowledge builder tool works:
```
Using authorSkill with input "I want to create a skill that tracks weekly pod refresh status for ops engineering"
```

The server should respond with the IDENTIFY_PERSONA state prompt.

---

## 8. Ongoing Operations

### 8.1 Log locations and rotation

All service logs go to the systemd journal. Query them with:

```bash
# API server logs
sudo journalctl -u framework-api -n 100 --no-pager

# Follow ingestion logs live
sudo journalctl -u kbf-ingestion -f

# All KBF services together
sudo journalctl -u framework-api -u kbf-ingestion -u kbf-scheduler --since "1 hour ago"
```

To ship logs to OCI Logging (optional):
```bash
# Install OCI Unified Monitoring Agent
sudo bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-unified-monitoring-agent/main/scripts/install.sh)"
# Then configure log source in OCI Console → Observability → Logging → Log Sources
```

Log rotation is handled automatically by systemd journal (default: max 100 MB per service,
oldest entries pruned). To check current journal disk usage:
```bash
sudo journalctl --disk-usage
```

To retain longer history, edit `/etc/systemd/journald.conf`:
```ini
[Journal]
SystemMaxUse=2G
MaxRetentionSec=30day
```

### 8.2 Monitoring

**Healthz polling:**
```bash
# Set up a simple cron on the VM to alert if healthz fails
(crontab -l; echo "*/5 * * * * curl -s http://localhost:8080/healthz | grep -q '\"status\": \"ok\"' || echo 'KBF healthz FAILED' | mail -s 'KBF Alert' admin@yourcompany.com") | crontab -
```

**Cost telemetry:**
```bash
# Check cost for the last 7 days
curl -s "https://<your-hostname>/api/v1/metrics/cost?startDate=2026-05-03&endDate=2026-05-10" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

Expected response shows token usage by persona and operation (ingestion / retrieval / synthesis).
Review this weekly against your OCI GenAI spend budget set in `prod.yaml` under
`openai.spend_cap_usd_per_month`.

**ADB automatic backups:** Oracle ADB automatically backs up to object storage every 24 hours.
Retention is 60 days by default. No action needed to enable — verify in:
`OCI Console → Autonomous Database → [your DB] → Backups`

### 8.3 Setting up webhooks for incremental ingestion

For Confluence:
```
Confluence Admin → Webhooks → Add webhook
  URL: https://<your-hostname>/webhooks/confluence
  Secret: <value of vault://kb/confluence-webhook-secret>
  Events: page_created, page_updated, page_archived
```

For Jira:
```
Jira Administration → System → WebHooks → Create a WebHook
  URL: https://<your-hostname>/webhooks/jira
  Issue Related Events: jira:issue_created, jira:issue_updated
  JQL filter: project IN (OPS, P2T, ENG)   (adjust to your project keys)
```

The framework's `webhook_router` (`framework/ingestion/webhook_router.py`) validates
HMAC signatures using the webhook secrets from Vault and queues changes for the
incremental ingestion pipeline.

### 8.4 Updating the framework

```bash
# On the VM
cd /opt/kbf/app

# Pull latest
git pull

# If Python dependencies changed
source .venv/bin/activate
pip install -e framework/
pip install -r framework/requirements.txt

# If DB schema changed (check migration files for new schemas)
python -m framework.cli.kb_cli migrate --schema kb_incidents --env prod
# (repeat for any other schemas that changed)

# Restart all services
sudo systemctl restart framework-api framework-ingestion framework-scheduler

# Verify health
curl -s http://localhost:8080/healthz
```

**Zero-downtime rolling restart (if running multiple VMs):** Not covered here — v1 uses a
single VM. For rolling deploys, set up an OCI Load Balancer + multiple VMs in the future.

### 8.5 Adding new personas or skills

Via the MCP `authorSkill` tool (interactive, recommended):
```
In Claude Code: "I want to create a new workflow skill for the TPM persona that
                 generates weekly escalation reports."

Claude Code calls authorSkill → server-side state machine guides through
IDENTIFY_PERSONA → ... → PROMOTE → DONE
```

Via `kb-cli skill-builder` (CLI):
```bash
# On the VM
python -m framework.cli.kb_cli skill-builder \
  --intent-file /tmp/my_intent.yaml
```

After a skill is promoted, the ingestion service automatically picks up the new
persona builder config (it polls the config directory every 60 seconds) and
begins incremental ingestion for the new knowledge bases.

### 8.6 Backup strategy

| What | Where | Retention | How to restore |
|------|-------|-----------|---------------|
| ADB data (all schemas) | OCI-managed automatic backup | 60 days | OCI Console → ADB → Backups → Restore |
| Wiki content bodies | Git repo (the canonical repo you cloned from) | Git history | `git clone` from canonical remote |
| Workflow skill YAMLs + extraction schemas | Git repo | Git history | Same |
| Eval baselines | OCI Object Storage `kb-raw-prod/eval/baselines/` | Lifecycle policy (365 days) | `oci os object get` |
| Vault secrets | OCI Vault (HA, replicated) | OCI-managed | Re-run `bootstrap-vault.sh` from values you have |

Secrets stored in Vault are not backed up by the framework — they are backed up by OCI
automatically. Keep your own record of the secret values in a team password manager.

---

## 9. Troubleshooting

### 9.1 ADB connection failures

**Symptom:** `healthz` shows `"adb": "error"` or Python raises `DPY-3001: python-oracledb thin mode cannot be used`.

**Cause and fix:** The `oracledb` library must run in thick mode when using a wallet.
Verify the Instant Client is installed and the library path is set:
```bash
python3 -c "import oracledb; oracledb.init_oracle_client(); print(oracledb.clientversion())"
```
Expected: `(21, x, x, x, x)`. If this raises, Instant Client is missing or not in `LD_LIBRARY_PATH`.
Re-run §3.4 and check `/etc/profile.d/oracle.sh`.

**Symptom:** `ORA-01017: invalid username/password`.

**Cause and fix:** The password in Vault does not match what was set during `CREATE USER`.
Update the Vault secret:
```bash
oci vault secret update-base64 \
  --secret-id <secret-ocid> \
  --secret-content-content "$(printf '%s' '<new-password>' | base64)"
```

**Symptom:** `TNS:could not resolve the connect identifier`.

**Cause and fix:** `TNS_ADMIN` is not set or the wallet is in the wrong directory.
Verify: `echo $TNS_ADMIN` and `ls $TNS_ADMIN/tnsnames.ora`. The service name in your
`adb.service_name` config must match exactly what is in `tnsnames.ora`.

### 9.2 OCI GenAI rate limits

**Symptom:** `429 Too Many Requests` in ingestion or synthesis logs.

**Cause:** Your OCI GenAI service tier has a lower RPM cap than the default 600 in
`framework/config/adapters/llm.yaml`.

**Fix:** Reduce the rate limit:
```yaml
# framework/config/adapters/llm.yaml
oci_genai:
  rate_limit:
    requests_per_minute: 60    # adjust to your actual tier
```
Then restart the API service:
```bash
sudo systemctl restart framework-api
```

Also consider reducing `openai.rate_tier` in `prod.yaml` and enabling request batching
in the ingestion pipeline (coming in a future phase).

### 9.3 Ingestion failures

**Symptom:** Ingestion logs show "HMAC verification failed" for webhooks.

**Fix:** The webhook secret in OCI Vault does not match what was configured at the source
(Confluence/Jira). Update the Vault secret to match the webhook configuration in the
source system, or recreate the webhook in the source system using the current secret value.

**Symptom:** Ingestion logs show "400 Bad Request" from Confluence or Jira API.

**Fix:** The PAT in Vault has expired or been revoked. Generate a new PAT in Confluence
or Jira and update the Vault secret (`confluence-readonly` or `jira-readonly`).

**Symptom:** Vector embeddings are not being generated (`SELECT COUNT(*) FROM kb_incidents.chunks WHERE embedding IS NULL` is non-zero after ingestion completes).

**Fix:** The in-DB embedding credential may not be set up correctly. Run the embedding
procedure manually:
```sql
sqlplus KB_INCIDENTS_RW/"<password>"@kbfprod_high
EXEC batch_insert_datasets_vectors_kbi;
EXIT;
```
Check for errors. If `DBMS_CLOUD.CREATE_CREDENTIAL` was not run (§5.3), complete that step first.

**Symptom:** `MissingMetadataError` during ingestion.

**Fix:** The persona builder config is missing a required field. Run:
```bash
python -m framework.cli.kb_cli validate framework/persona_builders/<persona>.yaml
```
The validator prints which fields are missing. All `ContentItem` fields must be set:
`persona_visibility`, `owner`, `classification`, `source_sha`, `parser_version`, `schema_version`.

### 9.4 MCP connection failures

**Symptom:** Claude Code shows "Failed to connect to MCP server."

**Fix checklist:**
1. Verify the server is running: `curl -s https://<your-hostname>/healthz`
2. Verify the bearer token is not expired or revoked: `python -m framework.cli.kb_cli token list`
3. Verify the `url` in your `.claude/mcp.json` uses `https://` (not `http://`)
4. Verify Nginx is running: `sudo systemctl status nginx`
5. Verify the TLS certificate is valid: `openssl s_client -connect <your-hostname>:443 -brief`

**Symptom:** Claude Code connects but reports "Tool not found: askKnowledgeBase."

**Fix:** The MCP server may have started before the workflow skills were registered.
Restart the API service:
```bash
sudo systemctl restart framework-api
sleep 5
curl -s http://localhost:8080/mcp/tools/list
```

### 9.5 Author-skill session issues

**Symptom:** `GET /api/v1/kb/authorSkill` returns an empty sessions list even after starting a session.

**Fix:** The `kb_shim.author_skill_sessions` table may not have been created. Run:
```bash
python -m framework.cli.kb_cli migrate --schema kb_shim --env prod
```

**Symptom:** Session returns `409 session_expired`.

**Cause:** The 7-day TTL has elapsed. This is correct behavior.

**Fix:** Start a new session. The expired session is retained for 30 days for audit.

### 9.6 Nginx / TLS issues

**Symptom:** `https://<your-hostname>/healthz` returns connection refused.

**Fix:** Check Nginx status and config:
```bash
sudo systemctl status nginx
sudo nginx -t              # test config
sudo journalctl -u nginx -n 50 --no-pager
```

**Symptom:** Certificate errors from clients.

**Fix:** Renew the Let's Encrypt certificate:
```bash
sudo certbot renew --force-renewal
sudo systemctl reload nginx
```

For a self-signed cert (no DNS/public domain): use `openssl req -x509 -newkey rsa:4096 -nodes`
and configure Nginx with `ssl_verify_client off`. MCP clients may need to set
`ssl_verify: false` — only appropriate for internal deployments.

### 9.7 Eval CI gate regressions

**Symptom:** `kb-cli eval` reports recall@10 < 0.80 for ops_eng.

**Fix sequence:**
1. Check `eval/runs/<latest-run>.md` for which gold-set questions regressed.
2. Was the parser schema changed? Validate with `kb-cli validate`.
3. Was the embedding model changed? The embedding dim is pinned to 3072 (text-embedding-3-large).
   If the model changed, bump `schema_version` in the persona builder and re-ingest (full reindex).
4. If a regression is acceptable (e.g., gold set is outdated), update the baseline:
   ```bash
   python -m framework.cli.kb_cli eval --update-baseline framework/persona_builders/ops-eng.yaml
   ```
   This requires a review from the Architect before merging.

### 9.8 Cost spike

**Symptom:** Cost telemetry shows unexpected token usage surge.

**Investigation:**
```bash
curl -s "https://<your-hostname>/api/v1/metrics/cost" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

Then:
```sql
-- Find the persona and time window
SELECT persona, DATE_TRUNC('hour', created_at) AS hour,
       SUM(prompt_tokens + completion_tokens) AS tokens
FROM   kb_shim.cost_log
WHERE  created_at > SYSDATE - 1
GROUP BY persona, hour
ORDER BY tokens DESC;
```

Common causes:
- A full re-ingest was triggered (expected; high but temporary)
- A runaway webhook is replaying the same document repeatedly — check `change_detection` logs
- Over-extraction in an extraction schema — review and narrow the schema's field count

---

## 10. Configuration Reference

### 10.1 Environment variables (.env file)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `KBF_ENV` | yes | `dev` | Environment name. Must be `prod` for production. |
| `KBF_CONFIG` | yes | `framework/config/dev.yaml` | Path to env config YAML. |
| `KBF_LLM_PROVIDER` | yes | `stub` | LLM provider. Set to `oci_genai` for production. |
| `STORAGE_BACKEND` | yes | `filestore` | Storage backend. Set to `oracle_adb` for production. |
| `TNS_ADMIN` | yes (ADB) | — | Path to directory containing Oracle wallet files. |
| `OCI_GENAI_ENDPOINT` | yes (GenAI) | — | OCI GenAI Inference endpoint URL for your region. |
| `OCI_GENAI_COMPARTMENT_ID` | yes (GenAI) | — | OCID of the compartment for GenAI quota attribution. |
| `KBF_SECRETS_BACKEND` | yes | `local` | Secrets backend. Set to `vault` for production. |
| `OCI_AUTH_METHOD` | yes (OCI) | `config_file` | OCI auth method. Set to `instance_principal` on VM. |
| `MCP_TRANSPORT` | yes | `stdio` | MCP transport mode. Set to `sse` for remote server. |
| `MCP_PORT` | no | `8080` | Port for the FastAPI server to bind on. |
| `KBF_LOG_LEVEL` | no | `INFO` | Log level: `DEBUG`, `INFO`, `WARN`, `ERROR`. |
| `PYTHONPATH` | yes | — | Must include the repo root so `import framework` works. |
| `KBF_SECRETS_FILE` | laptop only | `~/.kbf/secrets.yaml` | Path to local secrets file (laptop mode only). |
| `KBF_STORE_BACKEND` | laptop only | `filestore` | Set to `filestore` for laptop mode. |
| `KBF_STORE_ROOT` | laptop only | `~/.kbf/store` | Root directory for filestore backend. |

### 10.2 prod.yaml fields

| Field path | Type | Description |
|------------|------|-------------|
| `env` | string | Must be `prod` |
| `region` | string | OCI region identifier (e.g., `us-ashburn-1`) |
| `adb.service_name` | string | ADB service name from `tnsnames.ora` (use `_high` for API) |
| `adb.wallet_path` | string | Absolute path to wallet directory on the VM |
| `adb.admin_user` | string | ADB admin username (`ADMIN`) |
| `adb.admin_password_secret` | string | Vault reference (`vault://kb/adb-admin-prod`) |
| `adb.schemas.kb_incidents.user` | string | Schema user for incident vector store |
| `adb.schemas.kb_shim.user` | string | Schema user for shim tables (sessions, cost log, tokens) |
| `vault.vault_ocid` | string | OCID of the OCI Vault |
| `vault.master_key_ocid` | string | OCID of the master encryption key |
| `vault.compartment_ocid` | string | OCID of the compartment containing the vault |
| `object_storage.namespace` | string | OCI tenancy namespace (for object storage) |
| `object_storage.bucket` | string | Bucket name for raw dumps and eval artifacts |
| `compute.mcp_server_endpoint` | string | Public HTTPS URL of the framework (used in health checks) |
| `openai.api_key_secret` | string | Vault reference for OpenAI API key (fallback only) |
| `openai.models.embedding` | string | Pinned embedding model. Do not change without a full reindex. |
| `oci_genai.endpoint` | string | OCI GenAI Inference endpoint URL |
| `oci_genai.auth` | string | `instance_principal` on VM; `config_file` on laptop |
| `orchestrator.routing_thresholds.tier1_workflow_match` | float | 0.85 — confidence floor for workflow skill match |
| `orchestrator.routing_thresholds.tier2_kb_retrieval` | float | 0.60 — confidence floor for KB retrieval |
| `orchestrator.routing_thresholds.tier3_multi_persona` | float | 0.40 — confidence floor for multi-persona fanout |
| `observability.log_level` | string | `INFO` for production |
| `eval.baseline_storage` | string | OCI Object Storage path for eval baselines |

### 10.3 Port reference

| Port | Protocol | Bound to | Service | Purpose |
|------|----------|----------|---------|---------|
| 443 | HTTPS | 0.0.0.0 | Nginx | External TLS: MCP SSE, REST API, webhooks |
| 80 | HTTP | 0.0.0.0 | Nginx | ACME challenge + redirect to 443 |
| 8080 | HTTP | 127.0.0.1 | FastAPI / Uvicorn | Internal API (Nginx proxies here) |
| 22 | SSH | 0.0.0.0 | sshd | VM admin access |
| 1522 | TCP | outbound | oracledb | ADB wallet connection (outbound only) |

Port 8080 is bound to localhost only — it is not reachable from the internet. All external
traffic goes through Nginx on 443.

### 10.4 Security list rules reference

For the OCI security list (`kb-framework-sl`):

Ingress rules:
| Source CIDR | Protocol | Port | Description |
|-------------|----------|------|-------------|
| `<office-CIDR>` | TCP | 22 | SSH admin |
| `0.0.0.0/0` | TCP | 443 | HTTPS (MCP SSE + REST API) |

Egress rules:
| Destination | Protocol | Port | Description |
|-------------|----------|------|-------------|
| `0.0.0.0/0` | TCP | 443 | Outbound HTTPS (OCI APIs, Confluence, Jira, GitHub) |
| `0.0.0.0/0` | TCP | 1522 | Oracle ADB wallet connection |

Do NOT open port 8080 in the security list. It is internal-only.

### 10.5 Vault secret slug reference

All secrets follow the naming convention `vault://kb/<slug>`. Below are all slugs
provisioned by `bootstrap-vault.sh prod`:

| Slug | Contents | Used by |
|------|----------|---------|
| `adb-admin-prod` | ADB ADMIN password | `kb-cli migrate`, `check-config.py` |
| `kb-incidents-rw-prod` | KB_INCIDENTS_RW schema password | `framework.stores.incident_vector_store` |
| `kb-wiki-rw-prod` | KB_WIKI_RW schema password | `framework.stores.wiki_metadata_store` |
| `kb-code-rw-prod` | KB_CODE_RW schema password | Phase 2 code wiki store |
| `kb-fa-rw-prod` | KB_FA_RW schema password | Phase 4 FA graph store |
| `kb-shim-rw-prod` | KB_SHIM_RW schema password | Shim tables (sessions, cost, tokens) |
| `confluence-readonly` | Confluence PAT | `framework.adapters.confluence_adapter` |
| `confluence-webhook-secret` | HMAC shared secret for Confluence webhooks | `framework.ingestion.webhook_router` |
| `jira-readonly` | Jira PAT | `framework.adapters.jira_adapter` |
| `jira-webhook-secret` | HMAC shared secret for Jira webhooks | `framework.ingestion.webhook_router` |
| `git-readonly` | SSH private key for wiki/skill repo access | `framework.adapters.git_adapter` |
| `openai-api-key` | OpenAI API key (fallback) | `framework.core.llm` when `KBF_LLM_PROVIDER=openai_direct` |
| `udap-jdbc-url` | UDAP/Sentinel JDBC connection URL | `framework.adapters.udap_adapter` |
| `udap-user` | UDAP read-only user | Same |
| `udap-password` | UDAP read-only password | Same |
| `bearer-<consumer>` | Per-consumer bearer token (one per MCP client) | `framework.deploy.mcp_server` auth middleware |

---

## References

- PDD V3 (external surface, deployment topology, auth model): `docs/wiki/pdd/PDD-Knowledge-Builder-Framework-v3.md`
- PDD V2 (internal architecture): `docs/wiki/pdd/PDD-Knowledge-Builder-Framework-v2.md`
- Laptop quickstart (local dev without provisioning): `docs/wiki/engineering/laptop-quickstart.md`
- Developer guide (Phase 1 setup): `docs/wiki/engineering/dev-guide.md`
- Operations runbook (day-2 ops): `docs/wiki/engineering/runbook.md`
- Config schema: `framework/config/_schema.json`
- Production config: `framework/config/prod.yaml`
- Vault bootstrap script: `framework/scripts/bootstrap-vault.sh`
- Config check script: `framework/scripts/check-config.py`
- Incident vector store DDL: `framework/stores/sql/kb_incidents.sql`
- MCP server entry point: `framework/deploy/mcp_server.py`
- ADR-001 (tech stack): `docs/wiki/adr/ADR-001-tech-stack-baseline.md`
- ADR-010 (configuration plane): `docs/wiki/adr/ADR-010-configuration-plane.md`
- ADR-012 (in-DB embedding): `docs/wiki/adr/ADR-012-in-db-embedding.md`
- ADR-014 (LLM via OCI GenAI): `docs/wiki/adr/ADR-014-llm-via-oci-genai.md`
