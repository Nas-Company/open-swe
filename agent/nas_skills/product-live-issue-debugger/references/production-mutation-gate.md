# Production Mutation Gate

Read this file completely before proposing or executing any production write.

## Core Contract

Production reads are allowed when authorized by the incident scope. Production writes are forbidden until the user explicitly approves the exact proposed mutation in the current conversation.

Investigation, debugging, code-fix authorization, urgency, credential availability, and approval for an earlier mutation do not authorize a new production write.

## Covered Operations

- Database insert, update, delete, migration, repair, or aggregation with output stages
- Membership, entitlement, role, user, subscription, product-access, or configuration changes
- Cache invalidation/deletion, queue replay, message redrive, webhook retry, or job invocation
- AWS restart, scaling, deploy, Lambda invocation with side effects, or configuration change
- Refund, cancellation, provider retry, credential rotation, or provider dashboard/API change
- State-changing HTTP request or application endpoint
- Sending a customer/internal message, updating a ticket, merging a PR, or deploying a release

## Required Proposal

Present all fields before asking for approval:

1. **Target:** environment, system/service, database/collection, or provider resource.
2. **Identity:** exact redacted record/resource identifiers and tenant/community.
3. **Guarded action:** exact filter/preconditions and proposed update or command.
4. **Expected scope:** matched/modified count and customer-visible effect.
5. **Before state:** redacted read-only evidence captured immediately before the write.
6. **Risk:** blast radius, side effects, reversibility, and idempotency.
7. **Rollback:** exact reversal or recovery plan.
8. **Verification:** post-write read query/check and expected result.

General responses such as `fix it`, `go ahead`, or approval of a code change are not production mutation approval.

## Execution Rules

Even after exact approval:

1. Re-read the approved target, filter, action, and expected count.
2. Re-run the redacted before-state query.
3. Abort if state or expected count changed.
4. Use only an approved production write tool/operator.
5. Execute the smallest atomic guarded mutation.
6. Stop if matched/modified counts differ from approval.
7. Run post-write verification and retain rollback readiness.

Fresh approval is required when any target, filter, count, field, command, environment, side effect, or risk changes.

If an approved production write tool is unavailable, stop and report the blocker. Never extract credentials, connect with an unapproved client, or substitute a locally exposed secret.
