# n8n workflows

Three workflows, exported as JSON and committed. Import through the n8n UI:
**Workflows → Import from File**.

Built against **n8n 2.x**. Every node type and version below was checked against
a live 2.39.6 instance rather than written from memory, and the original node
configurations were validated against that instance's own schema. Nodes added
since were not: the status checks and Stop and Error nodes in 02 copy
configurations already validated in 01, but three settings appear in no
validated node: the **Alert** node in 03 sends a JSON body (`sendBody`,
`specifyBody`, `jsonBody`), the three HTTP nodes in 02 route their own failures
to an error output (`onError: continueErrorOutput`), and Shape Failure in 03
reads the run id out of the message with a regular expression. After importing,
open those nodes and confirm none shows a warning. It retries three times, five
seconds apart, and its key includes the failure time, so it stays unique if
n8n's execution ids restart after a reset.

| File | Trigger | What it does |
|---|---|---|
| `01-bill-polling.json` | Schedule, every 5 minutes | Calls `/runs/poll`, splits the returned run ids, calls 02 for each |
| `02-bill-processing.json` | Called by 01 | Reconcile, then semantic review if the gate permits, then notify |
| `03-error-handler.json` | Error trigger | Shapes a failure and posts it to `#ap-alerts` through the service's `/alerts`. The error workflow of both others |

## Node versions

Worth recording, because n8n moves and a stale `typeVersion` imports as a
greyed-out node with no useful message.

| Node | Type | Version |
|---|---|---|
| Schedule Trigger | `scheduleTrigger` | 1.4 |
| HTTP Request | `httpRequest` | 4.5 |
| If | `if` | 2.3 |
| Split Out | `splitOut` | 1 |
| Execute Sub-workflow | `executeWorkflow` | 1.3 |
| Execute Workflow Trigger | `executeWorkflowTrigger` | 1.2 |
| Edit Fields | `set` | 3.5 |
| No Operation | `noOp` | 1 |
| Stop and Error | `stopAndError` | 1 |
| Error Trigger | `errorTrigger` | 1 |

Two of these changed shape rather than just number. The **If** node moved to a
filter-style `conditions` object with `combinator` and typed `operator`, and the
old `itemLists` splitting operation became its own **Split Out** node.

## Before importing

Create one credential in n8n, of type **Header Auth**:

- Name: `Policy service bearer`
- Header name: `Authorization`
- Header value: `Bearer <INTERNAL_BEARER_TOKEN from .env>`

The literal word `Bearer`, a space, then the token. A missing space there is the
most common cause of every HTTP node returning 401.

That is the **only** secret n8n holds. No database credential, no Xero
credential, no Slack token (ADR-002).

`POLICY_SERVICE_URL` is set on the n8n container by `docker-compose.yml`, to
`http://policy_service:8000`. Service name, not `localhost`: inside a container,
localhost is that container.

## After importing

1. Open each workflow and re-select the `Policy service bearer` credential on
   every HTTP Request node. Credential ids do not survive an export, which is
   deliberate: an export carrying a working credential would be a secret in your
   git history.
2. In `01-bill-polling`, open **Process Each Bill** and select
   `02-bill-processing` from the workflow dropdown. The committed file has
   `REPLACE_WITH_02_WORKFLOW_ID`, because a workflow id is specific to your
   instance.
3. On 01 and 02, open **Settings** and set the error workflow to
   `03-error-handler`. The committed files have `REPLACE_WITH_03_WORKFLOW_ID`
   for the same reason.
4. In Slack, create `#ap-alerts` and invite the bot, as for the five triage
   channels (`docs/runbook.md`, Slack setup). n8n holds no Slack token (ADR-002), so 03 posts through the
   service, which uses its own.
5. Save each, then activate `01-bill-polling`.

## Two details in the JSON worth knowing

**`fullResponse` and `neverError`** are set on every HTTP node. The first gives
the IF nodes a `statusCode` to branch on; the second makes a 4xx a value to
branch on rather than a thrown node error, so the retryable and failed paths can
be distinguished. In 02, each of the three service calls is followed by a
`statusCode == 200` check, and Notify Succeeded also requires `body.posted` or
`body.already`, because a 200 alone is not proof that a card exists. Anything
else goes to a Stop and Error node, whose message names the run. So does a node
that fails outright, such as a timeout, through its error output. The error
workflow alerts `#ap-alerts` with the bill that failed, rather than the bill landing in Closed Without Review
as though it had nothing to review.

**`onError: continueRegularOutput`** on Process Each Bill. One malformed bill
should not cost you the other eight.

## Export hygiene

Re-export after any change, commit the file, and scan before committing:

```bash
./scripts/verify_no_secrets.sh
```

That runs `scripts/strip_workflow_meta.py`, which removes instance-specific
values and puts the `REPLACE_WITH_02_WORKFLOW_ID` placeholder back on Process
Each Bill and `REPLACE_WITH_03_WORKFLOW_ID` back in the error-workflow setting,
so a re-export cannot commit your instance's workflow ids. It then runs
`scripts/check_workflow_exports.py`, which parses the JSON and inspects the
places a credential can actually land. A credential can end up
inside a node parameter, and a workflow export is a file you will commit
repeatedly.
