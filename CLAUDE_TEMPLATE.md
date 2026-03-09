# Agent Studio Bot Builder — CLAUDE.md

You are building a PolyAI Agent Studio bot. Follow these steps exactly.

---

## ENVIRONMENT

Every `las` command must be prefixed exactly like this:
```
TERM=dumb POLYCTX_TOKEN_FILE=$POLYCTX_TOKEN_FILE /Users/shawnwen/local_agent_studio/.venv/bin/las <subcommand>
```
**Never run `las` without this prefix.**

After `las init`, all project files live at: `<cwd>/<ACCOUNT_ID>/<PROJECT_ID>/`
Always `cd` into that directory before running `las push` or `las pull`.

---

## REFERENCE PROJECTS

Before writing any files, read the reference doc to find similar examples:
```
cat ~/local_agent_studio/src/poly/docs/reference_projects.md
```
Find the closest vertical match in the Pattern Library, then read that project's `rules.txt` and flows to inform your build. Copy patterns, not code.

---

## BUILD STEPS

### 1. Init
```bash
TERM=dumb POLYCTX_TOKEN_FILE=$POLYCTX_TOKEN_FILE /Users/shawnwen/local_agent_studio/.venv/bin/las init \
  --region <REGION> --account_id <ACCOUNT_ID> --project_id <PROJECT_ID>
cd <ACCOUNT_ID>/<PROJECT_ID>
```

### 2. Write all files (everything at once)

Write in this order:
1. `agent_settings/rules.txt`
2. `functions/start_function.py` and `functions/end_function.py`
3. All other global functions in `functions/`
4. All topic YAMLs in `topics/`
5. All flow configs in `flows/<name>/flow_config.yaml`
6. All flow step YAMLs in `flows/<name>/steps/`
7. All flow functions in `flows/<name>/functions/`

**⚠️ CRITICAL — Flow naming rules:**

1. **Flow names must use plain words only** — no special characters, no ampersands, no abbreviations.
   - ✅ `Identity Verification`, `Check Balance`, `Payment Transfer`
   - ❌ `ID&V`, `IDV`, `Bal Chk`, `Xfer`

2. **Flow folder name MUST match `clean_name` of the flow `name`** exactly.
   `clean_name` = lowercase, non-alphanumeric chars → `_`, strip leading/trailing `_`.
   - `Identity Verification` → folder `identity_verification`
   - `Check Balance` → folder `check_balance`
   - `Payment Transfer` → folder `payment_transfer`

3. **Never invent short folder names** (e.g. `idnv`, `idv`, `chkbal`, `pmttfr`). Always derive from the full flow name.

### 3. Push everything in one go
```bash
TERM=dumb POLYCTX_TOKEN_FILE=$POLYCTX_TOKEN_FILE /Users/shawnwen/local_agent_studio/.venv/bin/las push --force --skip-validation
```

That's it. The LAS CLI handles ordering internally — functions are always created before flow steps. No staging needed.

**⚠️ Do NOT run `las pull` during a build.** Pull overwrites local files with whatever is in Agent Studio — if steps/functions haven't been pushed yet, pull will delete your local files.

---

## STANDARD CONFIGURATION FILES

Write these files exactly as shown on every build. Do not skip any of them.

### voice/speech_recognition/asr_settings.yaml — ALWAYS enable barge-in
```yaml
barge_in: true
interaction_style: balanced
```

### voice/configuration.yaml
```yaml
greeting:
  welcome_message: "Hello! Welcome to [Company Name]. How can I help you today?"
  language_code: en-GB
style_prompt:
  prompt: "Keep responses concise and natural for voice. Avoid lists and bullet points. Speak in full sentences. Don't say 'certainly' or 'absolutely'."
disclaimer_messages:
  message: "This call may be recorded for training and quality purposes."
  enabled: true
  language_code: en-GB
```

### agent_settings/personality.yaml
```yaml
adjectives:
  Polite: true
  Calm: true
  Kind: true
custom: ""
```

### agent_settings/role.yaml
```yaml
value: Customer Service Representative
additional_info: ""
custom: ""
```

### agent_settings/rules.txt
```
You are [Agent Name], a virtual assistant for [Company Name].

Keep responses short and natural — this is a voice call.
Never use bullet points, numbered lists, or markdown formatting.
Never say "certainly", "absolutely", or "of course".

If the user wants to [use case 1], call {{fn:start_<flow>_flow}}.
If the user wants to [use case 2], call {{fn:start_<flow>_flow}}.
```

### project.yaml — always present after las init, do not recreate

---

## FILE FORMATS

### agent_settings/rules.txt
```
You are [Name], a [role] for [Company].

If the user wants to [use case 1], call {{fn:start_flow1_flow}}.
If the user wants to [use case 2], call {{fn:start_flow2_flow}}.
```

### functions/start_function.py — REQUIRED
```python
from _gen import *  # <AUTO GENERATED>

def start_function(conv: Conversation):
    return {}
```

### functions/end_function.py — REQUIRED
```python
from _gen import *  # <AUTO GENERATED>

def end_function(conv: Conversation):
    return {}
```

### functions/start_<flow>_flow.py
```python
from _gen import *  # <AUTO GENERATED>

@func_description("Called when user wants to [do X]")
def start_<flow>_flow(conv: Conversation):
    conv.goto_flow("<Flow Name>")
    return {"end_turn": False}
```

### flows/<name>/flow_config.yaml
```yaml
name: Flow Name
description: What this flow does.
start_step: First Step Name
```
`start_step` must be the step **NAME** (string), not an ID.

### flows/<name>/steps/<name>.yaml
```yaml
step_type: advanced_step
name: Step Name
asr_biasing:
  is_enabled: false
  alphanumeric: false
  name_spelling: false
  numeric: false
  party_size: false
  precise_date: false
  relative_date: false
  single_number: false
  time: false
  yes_no: false
  address: false
  custom_keywords: []
dtmf_config:
  is_enabled: false
  inter_digit_timeout: 0
  max_digits: 0
  end_key: "#"
  collect_while_agent_speaking: false
  is_pii: false
prompt: |-
  ## What to do
  Ask the customer for X.

  ### Customer provides X
  Call {{ft:function_name}}.

  ### Customer wants to cancel
  Acknowledge and exit the flow.
```

### flows/<name>/functions/<name>.py
```python
from _gen import *  # <AUTO GENERATED>

@func_description("Called when customer provides X")
@func_parameter("param_name", "Description")
def function_name(conv: Conversation, flow: Flow, param_name: str):
    conv.state.param_name = param_name
    flow.goto_step("Next Step Name")
    return {"end_turn": False}
```

### topics/<name>.yaml
```yaml
example_queries:
- phrase 1
- phrase 2
content: |-
  Factual answer. No function calls here.
actions: |-
  ## Condition
  Call {{fn:function_name}}.
```

---

## CRITICAL RULES

| Rule | Detail |
|---|---|
| Import | Always `from _gen import *  # <AUTO GENERATED>` (line is stripped before push; never use `from imports import *`) |
| Return | Every function must return a dict: `return {}` or `return {"end_turn": False}` |
| File name | Must exactly match function name: `my_func.py` → `def my_func(...)` |
| Flow nav | `flow.goto_step("Step Name")` within a flow; `conv.goto_flow("Flow Name")` from global |
| Flow exit | `conv.exit_flow()` to return to main agent |
| flow_config.yaml | File must be named exactly `flow_config.yaml` (not `flow.yaml`) |
| step_type | Always `advanced_step`. Never use `default_step` — it is not a valid type. |
| Step conditions | Use `### Condition Name` headings inside the `prompt` field. Never add a `conditions:` YAML block to steps. |
| Entity alphanumeric | Set `validation_type` to `custom` (with a regex) or use `free_text` — never leave `validation_type` empty or `none`. |

---

## ERROR REFERENCE

**`401 JWT token exp claim failed`**
Token expired. **Do NOT try to fix it yourself.** Instead, run these commands to trigger the auth popup in the user's browser and wait for them to sign in:

```bash
# Trigger the sign-in popup
curl -s -X POST "$PTB_HOST/jobs/$PTB_JOB_ID/reauth"
# Wait until user completes sign-in (blocks up to 5 min)
curl -s "$PTB_HOST/jobs/$PTB_JOB_ID/reauth/wait?timeout=300"
# Now retry the failing command
```

Use this whenever you see: `401`, `token expired`, `Unauthorized`, `Authentication failed`, or `can't compare offset-naive` from `las push` or any PolyAI CLI.

**`No project configuration found`**
You're in the wrong directory. Run `cd <ACCOUNT_ID>/<PROJECT_ID>/` first.

**`Flow config not found for flow id: None`**
`start_step` is set to an ID instead of a name, or file is named `flow.yaml` instead of `flow_config.yaml`.

---

## COMPLETION FORMAT
```
✅ Build complete!
Branch: <branch ID>
Region: <region> | Project: <project_id>
Flows: <list>
Next step: Go to Agent Studio → Branches → merge <branch ID> to make live.
```

---

## CURSOR RULES
__CURSOR_RULES__

---

## MODE AWARENESS

You may be starting a **new build** or **continuing an existing project**. The build prompt will tell you which.

- **New build**: Follow the full INTAKE section, then BUILD STEPS 1–12.
- **Continue**: Skip `las init`. The project files are already present. Review existing files first with `ls` and `cat`, understand what's built, then ask the user what they want to add or change. Only push what has changed.

---

## INTAKE — ASK EVERYTHING UPFRONT

Before writing a single file, your FIRST response must collect all required information in one message. Format it exactly like this:

---
Before I start building, I need a few details:

**1. What's the Account ID and Project ID?**
(The project must already exist in Agent Studio)

**2. What's the agent's name and company name?**

**3. What vertical / industry?**
e.g. banking, hospitality, healthcare, retail, telecoms...

**4. Inbound or outbound?**

**5. Voice, chat, or both?**

**6. What should the bot handle?**
List the main use cases (e.g. check balance, report fraud, make a booking...)

**7. Are you optimising for higher containment or higher CSAT?**
Containment = resolve more calls without a human. CSAT = make customers happier, even if that means transferring sometimes.
---

Wait for the user's answers. Do not start building until you have all 7 answers. Once you have them, proceed directly to STEP 1 — do not ask any further questions.

**If the user has already provided some or all of this in their initial message, extract what you can and only ask for what's missing.**

---

## BUILD TASK
__BUILD_TASK__
