# n8n workflows

Three workflows, exported as JSON and committed. Import through the n8n UI:
**Workflows → Import from File**.

Built against **n8n 2.x**. Every node type and version below was checked against
a live 2.39.6 instance rather than written from memory, and each node
configuration was validated against that instance's own schema.

| File | Trigger | What it does |
|---|---|---|
| `01-bill-polling.json` | Schedule, every 5 minutes | Calls `/runs/poll`, splits the returned run ids, calls 02 for each |
| `02-bill-processing.json` | Called by 01 | Reconcile, then semantic review if the gate permits, then notify |
| `03-error-handler.json` | Error trigger | Shapes a failure into a readable record. Set as the error workflow on both others |

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
   `03-error-handler`.
4. Save each, then activate `01-bill-polling`.

## Two details in the JSON worth knowing

**`fullResponse` and `neverError`** are set on every HTTP node. The first gives
the IF nodes a `statusCode` to branch on; the second makes a 4xx a value to
branch on rather than a thrown node error, so the retryable and failed paths can
be distinguished.

**`onError: continueRegularOutput`** on Process Each Bill. One malformed bill
should not cost you the other eight.

## Export hygiene

Re-export after any change, commit the file, and scan before committing:

```bash
./scripts/verify_no_secrets.sh
```

That runs `scripts/check_workflow_exports.py`, which parses the JSON and
inspects the places a credential can actually land. A credential can end up
inside a node parameter, and a workflow export is a file you will commit
repeatedly.
