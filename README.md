# Agent Studio Builder

A web-based chat UI for building [PolyAI Agent Studio](https://studio.polyai.app) agents using Claude Code. Describe what you want to build — the builder writes all the files, fixes naming issues automatically, and pushes to Agent Studio for you.

![Agent Studio Builder](public/polyai-logo.png)

## Architecture

```
Browser (public/index.html)
    ↕ HTTP/SSE
Node Server (server.js)          ← auth relay, project setup, job proxy
    ↕ HTTP
PTB Server (ptb/server.py)       ← spawns Claude Code, streams events, runs las push
    ↕ subprocess
Claude Code + las CLI            ← writes agent files, pushes to Agent Studio
```

## Prerequisites

- [Node.js](https://nodejs.org) 18+
- [Python](https://python.org) 3.11+
- [Claude Code CLI](https://claude.ai/code) (`claude`)
- [Local Agent Studio CLI](https://github.com/PolyAI-LDN/local_agent_studio) (`las`)
- A PolyAI Agent Studio account

## Setup

### 1. Node server

```bash
npm install
cp .env.example .env
# Edit .env with your settings
node server.js
```

### 2. PTB Python server

```bash
cd ptb
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY and paths to claude/las binaries
python server.py
```

### 3. Open the UI

Visit [http://localhost:8081](http://localhost:8081)

## Usage

1. Click **Connect** and sign in with your PolyAI account
2. Describe the agent you want to build (vertical, use cases, flows)
3. Watch Claude Code build and push it to Agent Studio in real time
4. Go to Agent Studio → Branches → merge the new branch to make it live

## Features

- 🔴 **Live build streaming** — see every file Claude writes as it happens
- 🔁 **Token refresh** — automatic re-auth popup if your session expires mid-build
- 📁 **Auto file naming** — fixes flow folder and step file names before push
- 🛡️ **Job guard** — prevents duplicate builds for the same project
- 💬 **Session management** — switch projects, clear history, persistent chat on refresh

## Environment Variables

See `.env.example` for the Node server and `ptb/.env.example` for the PTB server.

## License

MIT
