# PostHog Self-driving setup report

## Summary

PostHog Self-driving is configured with Session Replay, Error Tracking, and Support enabled, plus health, error, and support signal sources. The inbox will begin receiving scout findings within about 30 minutes at [Self-driving inbox](https://us.posthog.com/project/595052/inbox).

This is a Flask application with a server-side PostHog SDK and no browser `posthog-js` initialization. Replay scanners are armed, but browser recordings will not arrive until the web client is instrumented.

## AI data processing

Approved.

## GitHub

The PostHog GitHub App was already connected before this setup. GitHub Issues was not selected as an inbox responder, so no GitHub Issues source was enabled.

## Products enabled

| Product | Result | Notes |
|---|---|---|
| Session Replay | enabled but inert | Server-side recording is on, but this Flask app has no browser `posthog-js` initialization to record user sessions. |
| Error Tracking | enabled | The existing Python SDK configuration enables exception autocapture. |
| Support | enabled | Tickets will begin arriving only after an inbound email, inbox, or Slack channel is connected in PostHog. |

## Signal sources

| Signal source | Action | Result |
|---|---|---|
| `signals_scout` / `cross_source_issue` | left at platform default | Scout findings are enabled by default; no opt-out row was created. |
| `health_checks` / `health_issue` | enabled | Source config `01a07150-c9c2-71eb-abdd-58621c3793a8`. |
| `error_tracking` / `issue_created` | enabled | Source config `01a07150-ca09-7dbd-a109-2c411fcb8c00`. |
| `error_tracking` / `issue_reopened` | enabled | Source config `01a07150-ca0b-7365-861e-1bcfb18ab234`. |
| `error_tracking` / `issue_spiking` | enabled | Source config `01a07150-c9f1-74a6-af83-c80202907809`. |
| `conversations` / `ticket` | enabled | Source config `01a07150-cbaf-76ab-b052-289ea56c1e4d`; remains idle until a Support channel is connected. |
| Session Replay source | skipped | Replay reaches the inbox through Replay Vision scanners, not a source-config row. |

## Connected tools

The connected-tools selection explicitly included **None of these**. No external-tool responder was enabled.

| Tool | Status |
|---|---|
| GitHub Issues | not used as an inbox responder |
| Linear | not used |
| Jira | not used |
| Sentry | not used |
| Zendesk | not used |

## Scout troop

The scout fleet was materialized with 27 built-in scouts. Four daily scouts are active (four of the verified 100 daily runs available); 23 are disabled to keep the troop focused.

- **Verified budget:** 100 maximum runs/day; 0 used today; 100 remaining.
- **Banner:** “Scouts are in early access. Each project gets up to 100 scout runs a day. Contact team-self-driving@posthog.com if you need more.”

| Scout | Status | Reason |
|---|---|---|
| `general` | enabled | Covers cross-product correlations and unassigned product surfaces. |
| `product-analytics` | enabled | The app captures core inbox and task-assistant product activity. |
| `health-checks` | enabled | Prioritizes actionable PostHog setup health findings. |
| `observability-gaps` | enabled | Identifies significant captured events with no insight or alert coverage. |
| `ai-observability` | disabled | The app uses OpenAI, but no PostHog LLM traces were confirmed. |
| `anomaly-detection` | disabled | No established saved dashboard or insight baseline was found. |
| `apm` | disabled | No APM or OpenTelemetry surface was confirmed. |
| `conversations` | disabled | Support tickets are covered by the native Support source. |
| `csp-violations` | disabled | No CSP reporting configuration was found. |
| `customer-analytics` | disabled | No PostHog account/group analytics surface was confirmed. |
| `data-pipelines` | disabled | No CDP, batch-export, or Hog Flow surface was confirmed. |
| `data-warehouse` | disabled | No warehouse data source was selected or detected. |
| `error-tracking` | disabled | Covered by the native Error Tracking sources. |
| `experiments` | disabled | No active experimentation surface was confirmed. |
| `feature-flags` | disabled | No feature-flag usage was confirmed. |
| `inbox-validation` | disabled | Fresh setup has no resolved Self-driving reports to re-measure yet. |
| `insight-alerts` | disabled | No configured insight-alert surface was confirmed. |
| `logs` | disabled | No PostHog logs product was confirmed. |
| `mcp-tool-calls` | disabled | No product-specific MCP telemetry surface was confirmed. |
| `replay-vision` | disabled | No prior Replay Vision observations existed; it can be enabled later for aggregate scanner trends. |
| `revenue-analytics` | disabled | No payment or revenue data surface was found. |
| `session-replay` | disabled | Covered by the Replay Vision scanners below. |
| `skills-store` | disabled | No skills-store maintenance surface was identified. |
| `surveys` | disabled | No survey usage was confirmed. |
| `tasks` | disabled | No PostHog Tasks delivery surface was confirmed. |
| `web-analytics` | disabled | No browser web-analytics instrumentation was confirmed. |
| `web-vitals` | disabled | No browser web-vitals instrumentation was confirmed. |

## Custom scouts

No custom scouts were created. Two candidates were proposed: a task-creation liveness check and an AI task-assistant adoption check. The selection also included **None — keep the built-in troop**, so the safe decline option prevailed.

The candidates are covered sufficiently for now by the general, product-analytics, health-checks, and observability-gaps scouts. If a custom scout is later noisy, set `emit: false` on its config in PostHog to retain dry-run evidence without sending findings to the inbox.

## Replay Vision scanners

A scanner is an LLM that watches individual session recordings on a schedule and pushes confirmed defects to the inbox. It is the only component in this setup that spends Replay Vision quota. Scanner findings arrive at half weight and need corroboration before becoming an inbox report.

No session recordings existed during setup. Both scanners are armed and will begin scanning after browser recording is instrumented and recordings arrive.

| Brief | Scanner | Status | Scope | Sampling | Estimated monthly spend |
|---|---|---|---|---|---|
| Breakage monitor | `Broken action inbox flow` (`01a07156-6fff-7ee9-98db-0bcdfc1ebd0f`) | created | URL-scoped to the Action Inbox’s root task workflow, where users create/update tasks, connect sources, and use AI task help. | 50% | 0 observations / 0 credits (no recordings yet) |
| Frustration monitor | `Action inbox user frustration` (`01a07156-7fcf-7f97-afe2-5780c28798f1`) | created | Sessions containing a rage click only; no URL scope was added, preserving monitor separation. | 100% | 0 observations / 0 credits (no recordings yet) |

The authoritative Replay Vision sizing skill was not available in the project skills store, so capacity could not be independently checked before creation. The scanner API’s current estimate is zero because there are no recordings.

## Follow-ups

- [ ] Add browser-side `posthog-js` initialization using the existing environment configuration, without disabling session recording, so Session Replay and the scanners can receive recordings.
- [ ] Connect an inbound email, inbox, or Slack channel to PostHog Support to start producing ticket findings.
- [ ] Recheck Replay Vision credit estimates after recorded sessions begin arriving.

## What happens next

The scout coordinator picks up the new configurations within about 30 minutes. Runs draw from the verified daily budget, findings cluster into Self-driving reports, and immediately actionable reports can begin coding tasks.

## Files changed

- Created `posthog-self-driving-report.md`.
- No application source, dependency, or environment files were modified.
