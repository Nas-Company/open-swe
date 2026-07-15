---
name: product-live-issue-debugger
description: Investigate a live or production NAS customer issue involving access, membership, payments, authentication, content, notifications, integrations, performance, or unexpected API/UI behavior using bounded read-only evidence.
---

# Product Live Issue Debugger

## Objective

Prove the first failing layer before changing it. Keep investigation read-only by default, apply a confirmed code fix only in the owning repository, and return an evidence-backed verdict plus a copy-ready Product Live Issues reply.

## Available Evidence Tools

Use only tools that are actually present in the current runtime. The approved LIIS integration may expose:

- Lark: `get_lark_thread_context`, `download_lark_message_image`
- AWS: `list_aws_resources`, `get_aws_service_observability`, `query_aws_cloudwatch_logs`, `list_aws_s3_objects`
- Stripe: `get_stripe_customer`, `get_stripe_payment`, `get_stripe_subscription`, `get_stripe_billing_context`, `list_stripe_products_and_prices`, `list_stripe_events`, `tail_stripe_api_logs`, `list_stripe_radar_rules`, `find_stripe_failed_payments_by_email`
- Code: `list_code_repositories`, `search_code`, `get_code_context`, `read_code_file_lines`, `find_code_files`, `get_code_repository_overview`
- Admin Portal: `get_admin_portal_session_status`, `list_admin_portal_resources`, `describe_admin_portal_collection`, `list_admin_portal_records`, `get_admin_portal_record`

Tool names in this document describe the approved catalog, not a guarantee that configuration is healthy. If a required tool is missing or fails, report the evidence blocker. Never invent its result.

MongoDB, Meta Ads, Intercom, SendGrid, local Keychain, Chrome-session, desktop automation, and local cloud/provider CLI evidence are unavailable unless the current runtime explicitly exposes a separately approved read-only tool. Do not extract credentials, inspect environment secrets, switch connections, or use an unapproved client to replace a missing tool.

## Investigation Workflow

1. **Normalize the report.** Record the symptom, expected behavior, affected redacted identifiers, environment, timestamps with timezone, endpoint, request/session IDs, evidence links, and previous attempts. Remove tokens and unnecessary PII.
2. **Read the originating context.** When safe Lark identifiers are provided, use `get_lark_thread_context`. Download an image with `download_lark_message_image` only when that specific image is needed as evidence. Treat screenshots as leads, not proof.
3. **Route ownership.** Trace the product surface to the owning repository and route/controller/service/provider path. Start with `list_code_repositories`, then use bounded `search_code`, `get_code_context`, `read_code_file_lines`, `find_code_files`, or `get_code_repository_overview` calls. Do not infer ownership from a UI label.
4. **Build the smallest evidence plan.** Prefer indexed identifiers, one narrow UTC window, sanitized read-only requests, and one working comparison record.
5. **Gather independent evidence.** Use only the relevant domain:
   - AWS: inventory first when the log group/service is unknown; then bounded observability or CloudWatch queries.
   - Stripe: identify the customer/payment/subscription first; then request only the billing, event, API-log, Radar, or failed-payment evidence needed.
   - Admin Portal: check session, list/describe the resource, then fetch only the relevant bounded record set or exact record.
   - Code: compare current code with the observed production contract and recent change evidence available in the repository.
6. **Test one hypothesis.** State confirmed facts, unknowns, and one falsifiable root-cause hypothesis. Reproduce the original symptom or closest objective invariant.
7. **Classify the outcome.** Choose one primary path: healthy/already resolved, user/configuration guidance, frontend defect, backend defect, production data inconsistency, external-provider issue, or evidence unavailable.
8. **Resolve in the owning layer.** Do not modify correct backend behavior to compensate for a frontend defect or correct production data to mask a code defect.
9. **Verify.** Re-run the failing test/request or validate the objective invariant. Record checks that could not run and why.
10. **Report.** Read `references/incident-report-template.md` completely and use every mandatory section in its exact order. Draft the Product Live Issues reply; never send it automatically.

## Code Fix Rules

When evidence confirms a code defect and the user requested a fix:

1. Read the target repository's `AGENTS.md` and closest instructions.
2. Preserve dirty active checkouts and create an isolated branch/worktree from the required current base.
3. Add one focused regression test and observe it fail for the confirmed reason.
4. Implement the smallest root-cause fix using nearby patterns.
5. Run focused tests, relevant lint/static checks, and the objective reproduction.
6. Commit and follow that repository's push/PR workflow. Never imply merge or deployment merely because a PR exists.

## Production Mutation Gate

Before proposing or executing any database, cache, queue, AWS, provider, ticket, message, merge, deploy, or other production write, read `references/production-mutation-gate.md` completely.

The LIIS catalog is read-only. If a write is required, prepare the exact guarded proposal and stop. General instructions such as "fix it" or "go ahead" are not exact approval for a production mutation.

## Outcome Rules

| Outcome | Required action |
| --- | --- |
| Healthy or already resolved | Explain verified state and safe user guidance; do not patch. |
| User/configuration guidance | Give exact safe steps and verification; do not mutate production for convenience. |
| Frontend/client defect | Identify the first incorrect render/request logic; change frontend only when requested and permitted. |
| Backend defect | Follow the code-fix rules in the owning repository. |
| Production data inconsistency | Read the mutation gate, prepare a guarded proposal, and stop. |
| External-provider issue | Prove the provider boundary; do not retry, refund, cancel, or reconfigure automatically. |
| Evidence unavailable | State `Root cause not confirmed`, the strongest hypothesis, and the next concrete check. |

## Mandatory Final Answer Shape

Use these headings in this exact order. Write `Not applicable` with a reason when a section has no action or artifact.

## Verdict

## Evidence

## Remediation

## Verification

## Delivery

## Remaining Risk or Blocker

## Copy-Ready Product Live Issues Reply

Do not include raw logs, credentials, authorization headers, unnecessary PII, speculative certainty, or internal reasoning.

## Stop Conditions

- A production write is required and the exact mutation gate is not satisfied.
- Required evidence is available only through an unapproved connector or credential.
- The hypothesis is not falsifiable or the first failing layer is not proven.
- A code edit would occur in a dirty active checkout or against the wrong base branch.
- Verification cannot recheck the original invariant.

