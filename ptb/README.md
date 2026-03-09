# Prompt to Build

A WebSocket-first API server that takes a natural language prompt and builds a complete PolyAI Agent Studio project using Claude Code, then pushes it to Agent Studio via `las push`.

Open `http://localhost:8788` after starting — you get a terminal-style UI that streams Claude's output token by token, just like native Claude.

---

## What you need before starting

| Requirement | How to get it |
|---|---|
| Python 3.11+ | `brew install python` / `apt install python3.11` |
| Git | `brew install git` / `apt install git` |
| Claude Code CLI | See step 1 below |
| Anthropic API key | See step 1 below |
| `las` CLI | See step 2 below (PolyAI internal) |
| `polyctx` auth | See step 2 below (PolyAI internal) |

---

## Setup

### Step 1 — Install Claude Code

```bash
npm install -g @anthropic-ai/claude-code
```

Then authenticate:
```bash
claude
# Follow the login prompt — you'll need an Anthropic account at console.anthropic.com
# Once logged in, Ctrl+C to exit. You're now authenticated.
```

Or use an API key directly (no interactive login needed):
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Verify it works:
```bash
claude --version
```

---

### Step 2 — Install las CLI and authenticate polyctx

> **Note:** `las` and `polyctx` are PolyAI internal tools — you need access to the internal repos.

```bash
# Clone and install local agent studio
git clone <internal-las-repo-url>
cd local_agent_studio
pip install -e .

# Install polyctx (comes with poly_core)
git clone <internal-poly-core-repo-url>
cd poly_core
pip install -e .

# Authenticate to Agent Studio (change region as needed)
polyctx auth add <your-email@poly-ai.com> --region uk-1
# Follow the browser OAuth flow
```

Verify both work:
```bash
las --help
polyctx --help
```

> ⚠️ **Known limitation:** `polyctx` currently uses a JWT token that expires every 15 minutes. Since builds can take longer than that, `las push` may fail with an auth error if the token expires mid-build. Workaround: re-run the build if you hit this. **This will be fixed next week when polyctx switches to API keys** — see [Switching to API keys](#switching-to-api-keys) below.

---

### Step 3 — Clone and configure

```bash
git clone https://github.com/shawnwun/prompt-to-build.git
cd prompt-to-build

# Install Python deps
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Configure
cp .env.example .env
```

Edit `.env` — at minimum set:
```bash
API_KEY_USER1=your-secret-key-here   # generate with: openssl rand -hex 32
ANTHROPIC_API_KEY=sk-ant-...         # skip if you logged in interactively above
CLAUDE_BIN=/path/to/claude           # find with: which claude
LAS_BIN=/path/to/las                 # find with: which las
```

---

### Step 4 — Run

```bash
source .env
python3 server.py
```

Open **http://localhost:8788**

---

## Using the web UI

1. Enter your API key (from `.env` → `API_KEY_USER1`)
2. Fill in Account ID, Project ID, Region — these come from your Agent Studio project URL
3. Describe the agent you want to build
4. Click **Build Agent** — Claude Code streams output live in the terminal panel
5. When done, you'll get a branch name — go to Agent Studio and merge it

**Tip:** Press `Cmd+Enter` (Mac) or `Ctrl+Enter` (Windows) to submit the prompt.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `API_KEY_USER1` … `API_KEY_USER7` | `ptb-dev-changeme` | One key per user. Always set at least USER1. |
| `ANTHROPIC_API_KEY` | — | Required if not using interactive Claude login |
| `CLAUDE_BIN` | `claude` | Full path to `claude` binary |
| `LAS_BIN` | `las` | Full path to `las` binary |
| `PORT` | `8788` | Server port |
| `JOBS_DIR` | `/tmp/ptb_jobs` | Where build workdirs and logs are stored |

Generate a secure API key:
```bash
openssl rand -hex 32
```

---

## Docker

```bash
cp .env.example .env
# Edit .env

# Update these two paths to your host binaries:
# CLAUDE_BIN_HOST=/usr/local/bin/claude
# LAS_BIN_HOST=/path/to/las

docker compose up --build
```

> **Note:** The container mounts your host `claude` and `las` binaries — you still need them installed on the host. For a fully self-contained image you'd need to bake them in, which requires the Anthropic npm package and PolyAI internal access.

---

## AWS Deployment (Fargate — automated)

This is the recommended path for spinning up dedicated containers per user with no manual intervention.

### Prerequisites
- AWS CLI configured (`aws configure`)
- Docker installed
- Access to the internal `las` repo

### Step 1 — One-time infrastructure setup

Run once to create ECR, ECS cluster, IAM roles and security group:

```bash
pip install boto3

export AWS_REGION=eu-west-2
export LAS_REPO_URL=https://<token>@github.com/polyai/local-agent-studio.git

bash infra/setup_aws.sh
```

This builds and pushes the Docker image to ECR with `claude` and `las` baked in.

### Step 2 — Provision a user

```bash
python3 infra/provision_user.py provision \
  --user alice \
  --anthropic-key sk-ant-... \
  --polyai-key <key>        # available next week — omit for now
```

That's it. In ~2 minutes you'll get:
```
✅ User 'alice' is ready!

  URL:     http://1.2.3.4:8788
  API key: ptb-alice-abc123...

Share the URL and API key with alice.
```

### Step 3 — List users

```bash
python3 infra/provision_user.py list
```

### Step 4 — Tear down a user

```bash
python3 infra/provision_user.py destroy --user alice
```

### What gets created per user
- AWS Secrets Manager secret (`ptb/<username>`) — stores all keys
- ECS task definition (`ptb-<username>`)
- Fargate task (1 vCPU, 2GB RAM) with a public IP
- CloudWatch log group (`/ptb/<username>`)

### Updating the image (e.g. after a code change)

```bash
export LAS_REPO_URL=https://<token>@github.com/polyai/local-agent-studio.git
bash infra/setup_aws.sh   # rebuilds and pushes new image

# Re-provision users to pick up the new image
python3 infra/provision_user.py provision --user alice --anthropic-key sk-ant-...
```

---

## AWS Deployment (EC2)

```bash
# 1. Launch EC2 — Ubuntu 22.04, t3.medium or larger
# 2. SSH in and run:

# Install Node (for Claude Code)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs git python3.11 python3.11-venv

# Install Claude Code
sudo npm install -g @anthropic-ai/claude-code

# Install las + polyctx (internal PolyAI setup)
# ... follow internal docs ...

# Clone and set up
git clone https://github.com/shawnwun/prompt-to-build.git
cd prompt-to-build
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env   # fill in your keys

# Run as a systemd service (keeps it alive on reboot)
sudo tee /etc/systemd/system/ptb.service > /dev/null <<EOF
[Unit]
Description=Prompt to Build
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/prompt-to-build
EnvironmentFile=/home/ubuntu/prompt-to-build/.env
ExecStart=/home/ubuntu/prompt-to-build/.venv/bin/python server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ptb

# Check it's running
sudo systemctl status ptb
curl http://localhost:8788/health
```

Open port 8788 in your EC2 security group to allow inbound traffic.

---

## Scaling to 7 users

Each user gets their own EC2 instance with:
- A unique `API_KEY_USER1` in their `.env`
- Their own `polyctx` session (authenticated to their Agent Studio account)

To add more users to a single instance, add more keys to `.env`:
```bash
API_KEY_USER1=key-for-alice
API_KEY_USER2=key-for-bob
# ... up to USER7
```

Builds run concurrently — each in an isolated temp directory. For guaranteed performance per user, use one instance per person.

---

## Switching to API keys

When polyctx switches to API key auth (expected next week), update two things:

**1. `.env`** — add:
```bash
POLYAI_API_KEY=your-api-key-here
```

**2. `server.py`** — find the `TODO: API KEY` comment and update the `env` block:
```python
env = {
    **os.environ,
    "POLYAI_API_KEY": os.environ["POLYAI_API_KEY"],  # replaces JWT auth
}
```

The `las` and `polyctx` CLIs should pick up the env var automatically once they support it. No other changes needed.

---

## WebSocket API reference

**Connect:**
```
ws://<host>:8788/ws/build?token=<api-key>
```

**Send** (JSON, after connecting):
```json
{
  "account_id": "PLATFORM",
  "project_id": "PROJECT-XXXXXXXX",
  "region": "uk-1",
  "prompt": "Build a hotel booking agent for a Marriott property...",
  "webhook_url": "https://optional-callback.com/done"
}
```

**Receive** (stream of JSON messages):

| `type` | Fields | When to use |
|---|---|---|
| `started` | `job_id` | Store job ID for log replay |
| `token` | `text` | Append inline — this is the live Claude output |
| `status` | `message` | Show dimmed — system info |
| `divider` | `message` | Visual separator |
| `done` | `message`, `branch` | Build finished — show branch name |
| `error` | `message` | Build failed |
| `warning` | `message` | Non-fatal |

---

## REST API (fallback)

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/build` | Bearer | Fire-and-forget build, returns `job_id` |
| `GET` | `/jobs/{id}` | Bearer | Status + branch name |
| `GET` | `/jobs/{id}/logs?offset=0` | Bearer | Replay log from disk |
| `GET` | `/jobs` | Bearer | List your jobs |
| `GET` | `/health` | None | Health check |

Auth: `Authorization: Bearer <api-key>`

---

## Test client (CLI)

```bash
pip install websockets
python3 scripts/test_ws.py --host localhost:8788 --token your-api-key
```

---

## Project structure

```
prompt-to-build/
├── server.py           # FastAPI + WebSocket server + build runner
├── static/
│   └── index.html      # Terminal-style web UI
├── scripts/
│   └── test_ws.py      # CLI test client
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```
