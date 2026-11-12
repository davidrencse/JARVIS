"""MAIN continuity prerequisites (docs/design/mobile-continuity, milestone H2).

`authority` is the single-writer lease/epoch/transition/admission store meant to run on the
stable gateway host (Oracle in the design). `clock` is the PC-side cached-validity view.

Status: tested on loopback with real components, not deployed. The only imports from the
running JARVIS are lazy, default-off hooks (cluster_cli, cluster, remote, rest, phone) that do
nothing until a cutover closes them; see OPERATIONAL-INTEGRATION.md. No gateway is
provisioned, `cluster.main_device` stays the live authority, and no executor enforces these
epochs in production. It must not be described as failover.
"""
