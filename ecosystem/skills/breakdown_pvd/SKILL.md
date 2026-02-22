---
name: breakdown_pvd
description: "Break down a PVD/tech-design document into Jira tasks (dev Improvement + optional QA Support) with plan_proposal preview"
argument-hint: "<confluence-url | gdocs-url | figma-url | file-path | inline-text> [--no-qa]"
disable-model-invocation: true
---

Break down a PVD or tech-design document into Jira work items and propose them as a plan.

**Argument:** `$ARGUMENTS`

## Instructions

### Step 1: Parse `$ARGUMENTS`

Split `$ARGUMENTS` into the source reference and optional flags.

**Source detection:**
- Contains `atlassian.net/wiki` → Confluence page
- Contains `docs.google.com` → Google Doc
- Contains `figma.com` → Figma design
- Looks like a file path (starts with `/`, `./`, or `~`, or contains `.md`/`.txt`) → local file
- Otherwise → treat entire argument as inline text

**Flags:**
- `--no-qa` → skip QA task generation

Store the detected source type and the cleaned URL/path/text.

### Step 2: Read `.jira.json` defaults

Check if `$WORKSPACE/.jira.json` exists. If it does, read it and extract:
- `project_key` (default project for Jira)
- `product_team` (default product team)
- `default_priority` (default priority)
- `labels` (default labels array)

If not found, use these defaults:
- `project_key`: `"PROJ"`
- `product_team`: `"MyTeam"`
- `default_priority`: `"Medium"`
- `labels`: `["pvd-breakdown"]`

### Step 3: Fetch document content

Based on the source type detected in Step 1:

**Confluence:**
Extract the page ID from the URL. Confluence URLs look like:
- `https://xxx.atlassian.net/wiki/spaces/SPACE/pages/PAGE_ID/Title`

```
mcp__atlassian__getConfluencePage with pageId="<PAGE_ID>"
```

**Google Docs:**
First list accounts, then read the document:
```
mcp__google__listAccounts
mcp__google__readGoogleDoc with documentId="<DOC_ID>"
```
Extract the document ID from the URL: `docs.google.com/document/d/<DOC_ID>/...`

**Figma:**
Extract fileKey and nodeId from the URL:
- `figma.com/design/:fileKey/:fileName?node-id=:nodeId` (convert `-` to `:` in nodeId)
- `figma.com/design/:fileKey/branch/:branchKey/:fileName` (use branchKey as fileKey)

```
mcp__figma__get_design_context with fileKey="<fileKey>" nodeId="<nodeId>"
```
Set `has_figma=true` and store the Figma URL for design links.

**Local file:**
Read the file directly from `$WORKSPACE/<path>`.

**Inline text:**
Use `$ARGUMENTS` (minus any flags) as the document content directly.

### Step 4: Fetch Jira metadata for form fields

Fetch project list and field options for the form:

```
mcp__atlassian__getVisibleJiraProjects
```
Build project options from the result: `[{"label": "KEY — Name", "value": "KEY"}, ...]`

```
mcp__atlassian__getJiraIssueTypeMetaWithFields with projectKey="<default_project_key>" issueTypeId="<improvement_type_id>"
```

From the field metadata, extract the custom fields configured in `.jira.json`:
- **Product Team** (`<product_team_field>`): list of allowed values → build options
- **Subteam** (`<subteam_field>`): list of allowed values → build options (prepend `{"label": "— None —", "value": ""}`)
- **Priority**: list of priorities → build options

If the metadata calls fail, use hardcoded fallback options:
- Projects: `[{"label": "PROJ — My Project", "value": "PROJ"}]`
- Product Team: `[{"label": "TeamA", "value": "TeamA"}, {"label": "TeamB", "value": "TeamB"}]`
- Subteam: `[{"label": "— None —", "value": ""}, {"label": "SubteamA", "value": "SubteamA"}]`

### Step 5: Analyze & decompose into work items

Read through the document content carefully. For each distinct feature, requirement, or component:

> **Target size: XS or S.** A single task sized M or larger is **NOT allowed**. Always split into multiple smaller, independently deliverable tasks.

**Dev task (Improvement):**
- **Summary**: concise feature title
- **Objective**: what this achieves and why
- **Acceptance Criteria**: checklist of verifiable criteria
- **Technical Notes**: implementation approach, API changes, schema
- **Dependencies**: other tasks, systems, or external services
- **Design Link**: Figma URL if `has_figma=true`, otherwise `N/A`

**QA task (QA Support)** — unless `--no-qa` flag:
- **Summary**: same feature title (no `[QA]` prefix — issue type makes it clear)
- **Test Scenarios**: derived from acceptance criteria
- **Test Types**: unit, integration, E2E areas
- **Regression Areas**: features that could be affected
- **Edge Cases**: boundary conditions, error states

**T-shirt sizing reference:**

| Size | Sprints | When to use |
|------|---------|-------------|
| XS | 2 | Config change, copy update, minor UI tweak |
| S | 4 | Single-component, straightforward API |
| M | 6 | **MUST split** → at least 2 S/XS tasks (more if complexity warrants) |
| L | 8 | **MUST split** → at least 3 S/XS tasks (scale with scope) |
| XL | 10 | **MUST split** → at least 4 S/XS tasks (as many as needed) |

**Mandatory split rule:** If your initial analysis produces a task sized M or larger, you MUST split it before proceeding. Every final task must be XS or S. The number of resulting tasks is **dynamic** — split as many times as needed until each piece is independently deliverable at XS/S size. Do not artificially limit or pad the count; let the document's complexity drive it. Apply these split strategies:

1. **Layer split**: Backend API → Frontend UI → Data migration (each as separate task)
2. **Sub-feature split**: Each independently usable feature becomes its own task
3. **Workflow step split**: Schema/model → CRUD/service → UI/component → Validation/edge-cases
4. **Component split**: Each distinct UI component or service module as a separate task

**Split rules:**
- Each split task must be independently deliverable and testable
- QA tasks follow a 1:1 mapping — one QA task per dev task after splitting
- Prefer more small tasks over fewer large tasks; aim for XS when possible
- Maintain dependency order between split tasks using `depends_on_step`

### Step 6: Build plan_proposal with form_fields

Construct a plan_proposal message. Each dev task is a plan step, each QA task is a plan step that depends on its corresponding dev step.

**Description templates for plan steps:**

Dev step description:
```
Create Jira Improvement in {{project_key}}: <summary>

Use mcp__atlassian__createJiraIssue:
- projectKey: {{project_key}}
- issueTypeName: Improvement
- summary: <summary>
- description: (see below)
- additional_fields: {"priority":{"name":"<priority>"}, "<product_team_field>":{"value":"{{product_team}}"}, "labels":["pvd-breakdown"]}

After creation, call mcp__atlassian__editJiraIssue to set:
- <subteam_field> (Subteam): {{subteam}} (if not empty)
- <design_review_field> (Design Review Required): true (if Figma link exists)

After ALL post-creation edits are done, report the created issue to the user by sending:
  curl -s -X POST $HUB/messages -H 'Content-Type: application/json' \
    -d '{"sender":"$AGENT_NAME","receiver":"user","content":"Created: **<ISSUE_KEY>** — <summary>","msg_type":"info"}'
(Replace <ISSUE_KEY> with the key returned by createJiraIssue, <summary> with the issue summary)

---
Description for the Jira issue:

## Objective
<what this achieves and why>

## Acceptance Criteria
- [ ] <criterion 1>
- [ ] <criterion 2>

## Technical Notes
- <implementation approach, API changes, schema>

## Dependencies
- <other tasks, systems>

## Design Link
<figma URL or N/A>

---
*T-shirt: <SIZE> (<N> sprints) | Source: <doc URL>*
```

QA step description:
```
Create Jira QA Support in {{project_key}}: <summary>

Use mcp__atlassian__createJiraIssue:
- projectKey: {{project_key}}
- issueTypeName: QA Support
- summary: <summary>
- description: (see below)
- additional_fields: {"priority":{"name":"<priority>"}, "<product_team_field>":{"value":"{{product_team}}"}, "labels":["pvd-breakdown"]}

After creation:
- Set <subteam_field> (Subteam): {{subteam}} (if not empty) via mcp__atlassian__editJiraIssue
- Link to dev task via mcp__atlassian__jiraWrite with method createIssueLink (type: "Relates", inwardIssue: this QA issue, outwardIssue: dev issue from previous step)

After ALL post-creation edits and linking are done, report the created issue to the user by sending:
  curl -s -X POST $HUB/messages -H 'Content-Type: application/json' \
    -d '{"sender":"$AGENT_NAME","receiver":"user","content":"Created QA: **<ISSUE_KEY>** — <summary>","msg_type":"info"}'
(Replace <ISSUE_KEY> with the key returned by createJiraIssue, <summary> with the issue summary)

---
Description for the Jira issue:

## Objective
Verify: <dev task summary>

## Test Scenarios
- [ ] <scenario 1>
- [ ] <scenario 2>

## Test Types
- Unit: <areas>
- Integration: <points>
- E2E: <flows>

## Regression Areas
- <affected features>

## Edge Cases
- <boundary conditions, error states>

---
*T-shirt: <SIZE> | Source: <doc URL>*
```

**Send the plan_proposal:**

Build a content summary with a markdown table of all tasks:

```
PVD Breakdown: <document title> — N dev + M QA tasks

| # | Type | Summary | Size |
|---|------|---------|------|
| 1 | Dev | <summary> | M |
| 2 | QA | <summary> | S |
...
```

Send the message:

```bash
curl -s -X POST $HUB/messages -H 'Content-Type: application/json' \
  -d '{
    "sender": "$AGENT_NAME",
    "receiver": "user",
    "msg_type": "plan_proposal",
    "content": "<summary with table>",
    "form_fields": [
      {
        "key": "project_key",
        "label": "Jira Project",
        "type": "select",
        "options": [<project options from step 4>],
        "default": "<from .jira.json or PROJ>",
        "required": true
      },
      {
        "key": "product_team",
        "label": "Product Team",
        "type": "select",
        "options": [<product team options from step 4>],
        "default": "<from .jira.json or MyTeam>",
        "required": true
      },
      {
        "key": "subteam",
        "label": "Subteam",
        "type": "select",
        "options": [<subteam options from step 4>],
        "default": "",
        "required": false
      }
    ],
    "plan_steps": [
      {
        "description": "<dev step description with {{placeholders}}>",
        "assigned_to": "$AGENT_NAME",
        "priority": 5
      },
      {
        "description": "<qa step description with {{placeholders}}>",
        "assigned_to": "$AGENT_NAME",
        "priority": 5,
        "depends_on_step": 0
      }
    ]
  }'
```

**Important:** Use `{{project_key}}`, `{{product_team}}`, and `{{subteam}}` as placeholders in step descriptions. The hub will substitute them with the user's selected values when the plan is approved.

### Step 7: Save report

```bash
mkdir -p "$WORKSPACE/.multiagent/reports"
```

Save a summary report to `$WORKSPACE/.multiagent/reports/pvd-breakdown_<timestamp>.md` containing:
- Document source URL/path
- Number of tasks generated
- Task summary table
- T-shirt size breakdown

### Jira Field Reference

| Field | Dev Task (Improvement) | QA Task (QA Support) |
|-------|----------------------|---------------------|
| Issue Type | Improvement | QA Support |
| Summary | Feature title | Feature title (no prefix) |
| Description | Dev template + checklist | QA template + scenarios |
| Priority | From analysis | Matches dev task |
| Product Team | From form `{{product_team}}` | From form `{{product_team}}` |
| Labels | `["pvd-breakdown"]` | `["pvd-breakdown"]` |
| Subteam | Set via post-edit `{{subteam}}` | Set via post-edit `{{subteam}}` |
| Design Review | Set via post-edit if Figma link | N/A |
| Links | — | "Relates" → dev issue |
