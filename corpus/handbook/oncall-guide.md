# Northstar Cloud — On-Call Guide

On-call at Northstar means deciding, in the first five minutes, whether
something is a blip or an incident. This document exists so that decision comes
from a checklist, not adrenaline.

## Rotation structure

Three rotations. **Payments** covers edge-gateway, payments-api, risk-engine,
and the processor adapters. **Ledger and settlement** covers ledger-service and
settlement-worker. **Platform** covers Kafka, PostgreSQL, Redis, reporting-api,
webhook-dispatcher.

Each runs one week, Wednesday 10:00 to Wednesday 10:00. Wednesday handoffs are
deliberate: nobody should inherit an unfamiliar system on a Friday afternoon.
Each has a primary and a secondary; the secondary is paged if the primary has
not acknowledged in 5 minutes.

An incident commander rotation sits above all three, staffed by senior engineers
and managers. The IC does not debug. The IC sets severity, assigns roles, owns
communication, and declares the incident over.

Expected volume is fewer than 3 pages per week per rotation. Above 5, the next
primary spends the week on alert quality, not project work.

## Severity definitions

**SEV1 — money is moving wrongly or not at all.** Acknowledge in 5 minutes,
incident declared immediately, IC paged automatically. Examples: ledger-service
unavailable, so nothing can be recorded and therefore nothing approved;
edge-gateway down in all regions; a confirmed double-charge across merchants;
settlement submitting wrong amounts. Executive notification within 15 minutes,
status page within 20.

**SEV2 — significant degradation, money is still correct.** Acknowledge in 10
minutes. Examples: one processor down affecting 15% of volume with no fallback
enabled; authorization p99 above 4 seconds; webhook-dispatcher backed up over 30
minutes; reporting-api replica lag over 15 minutes against the documented 90
seconds. Status page within 45 minutes.

**SEV3 — contained, working around it is acceptable.** Acknowledge within 1 hour
during business hours, next business morning overnight. Examples: elevated
risk-engine timeouts producing more fail-open soft approvals than usual; one
merchant's declines spiking; a settlement batch replayable tomorrow.

**SEV4 — a defect worth tracking, not worth waking anyone.** Triage within two
business days. Examples: a misleading error message, a metric with the wrong
unit, a dead runbook link.

Severity follows observed impact, not cause. A one-line config typo that stops
all authorizations is a SEV1. Total failure of a service nobody currently
depends on is not.

## Your first five minutes

1. Acknowledge the page. This stops the escalation timer and tells everyone a
   human is present.
2. Say something in `#incident-response`, even "looking at AuthFailureRateHigh,
   nothing yet." Silence reads as absence.
3. Check the deploy feed. A change shipped in the last 30 minutes is the most
   likely cause of a novel failure. If a deploy correlates, roll it back before
   you finish diagnosing.
4. Establish blast radius: all merchants or one, all regions or one, all
   processors or one. This sets severity and usually points at the cause.
5. Decide: declare an incident, or handle quietly.

Mitigate before you diagnose. Restoring service and understanding a failure are
separate activities.

## Declaring versus handling quietly

Declare if any of these hold: customer-visible impact over 10 minutes, any
suspected money correctness issue, you need help from someone not already paged,
or you cannot state the blast radius with confidence. Uncertainty is a reason to
declare, not to wait.

Handle quietly if impact is bounded, understood, and fixed by something you can
do alone in minutes: restarting a stuck consumer, replaying a settlement batch,
rolling back a nightly risk model. Post a note in the on-call channel regardless.

## Handoffs

Handoff is a live conversation, not a document dump. The outgoing primary covers
every page and its resolution, anything still open with its next step, any
deploy freeze or feature flag left in a non-default position, and any merchant
watching closely.

Anything mid-flight gets an explicit owner. "Someone should look at this" is not
a handoff. The written note goes in the on-call channel within 30 minutes.

## Postmortems

Every SEV1 and SEV2 gets a written postmortem within 5 business days. SEV3 gets
one if it recurs or the responder thinks it instructive.

Postmortems are blameless in a specific sense: we describe what a person did and
what they knew, then ask why the system made that action reasonable. "Engineer
ran the wrong command" is not a root cause. "The `--force` flag skipped the
confirmation and the dry-run output was identical to the real output" is.

Every postmortem produces action items with named owners and dates. Items
without an owner are deleted rather than left to rot: a list of unassigned
intentions makes the next postmortem harder to take seriously.
