# Incident Report Template

Use this structure for every completed investigation. Lead with the verdict and distinguish facts from inference.

## Verdict

- **Status:** Confirmed / Mitigated / Resolved / Not reproduced / Root cause not confirmed
- **Impact:** Who or what is affected and whether impact continues
- **Root cause:** One sentence, or `Not confirmed`
- **Owning layer:** Frontend / Backend service / Production data / External provider / User guidance

## Evidence

- **Reported time:** Timestamp and timezone
- **Investigated window:** UTC start/end
- **Identifiers:** Redacted user, community, product, request/session/trace IDs
- **Lark context:** Relevant message/thread evidence without unnecessary PII
- **Logs/observability:** Service, region/log group, bounded timestamp, result
- **Provider/Admin evidence:** Narrow read-only criteria and relevant invariant
- **API/reproduction:** Sanitized request, expected result, actual status/body/headers
- **Code path:** Repository, route, controller/service/provider path, relevant lines/change
- **Facts vs inference:** Label any remaining hypothesis explicitly

## Remediation

- **Performed:** Read-only checks or code fix
- **Recommended:** Smallest owning-layer fix or user guidance
- **Why this fix:** Connect it to the first incorrect state transition
- **Rollback:** Required for risky delivery or a separately approved production mutation

## Verification

- Original symptom or closest objective invariant
- Tests/checks run and exact result
- Production check completed or proposed
- Checks not run and reason

## Delivery

- Branch/worktree and commit
- PR URL and conflict status for each required base
- Companion context PR when required
- Merge/deployment status; never imply deployment from PR creation

## Remaining Risk or Blocker

- Unknowns, scope limits, provider/tool access, follow-up owner, and next concrete check

## Copy-Ready Product Live Issues Reply

Write a concise standalone draft containing:

1. What was checked
2. Confirmed cause or `not yet confirmed`
3. Current customer impact/workaround
4. Fix performed or required owning team
5. Verification/PR status and next step

Draft only. Never send it automatically. Do not include raw logs, secrets, credentials, unnecessary PII, speculative certainty, or internal debugging narration.
