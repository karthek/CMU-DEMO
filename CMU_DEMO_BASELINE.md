# CMU Demo Baseline

This independent repository supports the September 16 CMU capstone demonstration of the model-agnostic travel agent.

- Production source branch: `feature/v9-live-replanning`
- Production source commit: `c1b7194691a16b7fd4440f1990c9ab03fced99de`
- Baseline: V9 through Phase 5F-A, the provider-neutral calendar read contract.
- Source files were exported from the exact tracked production commit; production Git history and local runtime artifacts were not copied.

The production and demo repositories have independent Git histories. Demo changes do not automatically become production changes. Production-worthy demo improvements require later review before porting.

This baseline adds no demo features. The deterministic, host/model-agnostic core and the rule "Agent prepares; human commits" remain unchanged. Calendar mutation may never be autonomous.

## Test portability adjustment

Application source is an exact snapshot of production commit `c1b7194691a16b7fd4440f1990c9ab03fced99de`. One compatibility test originally read the production Git tag `v7`. Because CMU-DEMO intentionally has independent Git history, the authentic V7 input/output schema reference is preserved in `tests/fixtures/v7/mcp_schemas.json`, with its source commit and blob identity. The test now reads that frozen fixture and retains exact schema comparisons. The fixture must not be silently updated when current MCP schemas change.

Only this test, the added reference fixture, and this provenance document differ from the production snapshot. Runtime/application behavior was not changed.
