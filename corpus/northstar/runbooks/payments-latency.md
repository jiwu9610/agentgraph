# Runbook — payments latency / capture errors

Use when payments-api capture error rate is elevated or checkout-web timeouts
spike.

## Steps

1. Confirm symptom on payments-api capture dashboard.
2. Check ledger-service error rate and ledger-db connection pool saturation.
3. If ledger-db pool is saturated: raise pool limit 20%, shed non-critical
   reporting queries for 30 minutes.
4. Page the payments-api on-call (see org.md).
5. If Brightline Retail is impacted, notify their account channel within 15
   minutes.

## Related

- Incident pattern: INC-1042
- Deeper ledger repair: ledger-replay runbook
