# Northstar Cloud — evaluation corpus

Fictional **B2B payments SaaS**. Small on purpose: every multi-hop question in
`evals/golden_kg.json` is answerable from these files, and every wrong answer
usually means a missed edge — not "the docs were incomplete."

## Ontology (closed world)

**Entity types:** `Person`, `Team`, `Service`, `Datastore`, `Incident`,
`Customer`, `Runbook`

**Relation types:**
- `OWNS` (Person|Team → Service|Datastore)
- `MEMBER_OF` (Person → Team)
- `DEPENDS_ON` (Service → Service|Datastore)
- `CAUSED_BY` (Incident → Service|Datastore)
- `IMPACTS` (Incident → Customer|Service)
- `MITIGATED_BY` (Incident → Runbook)
- `ONCALL_FOR` (Person → Service)
- `DOCUMENTED_IN` (Service|Incident → Runbook)

If extraction invents a type outside this list, the KG ingest layer must
reject or quarantine it.

## Files

| Path | What it encodes |
|------|-----------------|
| `org.md` | People, teams, ownership, on-call |
| `services.md` | Services + primary datastores |
| `dependencies.md` | Explicit service→service / service→datastore edges |
| `customers.md` | Named customers and which services they use |
| `incidents/INC-1042.md` | A real-shaped incident with cause + impact |
| `incidents/INC-1099.md` | A second incident (disambiguation + multi-hop) |
| `runbooks/payments-latency.md` | Mitigation steps linked from INC-1042 |
| `runbooks/ledger-replay.md` | Mitigation for ledger issues |

## How to use

```bash
# build the graph
python -c "from docsbot.kg.ingest import ingest_corpus; print(ingest_corpus('corpus/northstar'))"
```

## Provenance drill

Corrupt a triple in the DB or a line in `org.md`, re-ingest, and use the
Phoenix traces to confirm that per-fact provenance points at the right file.
