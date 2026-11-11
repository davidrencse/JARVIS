"""MAIN continuity prerequisites (docs/design/mobile-continuity, milestone H2).

`authority` is the single-writer lease/epoch/transition/admission store meant to run on the
stable gateway host (Oracle in the design). `clock` is the PC-side cached-validity view.

Status: isolated prototype with tests only. Nothing in the running JARVIS imports this yet,
no gateway is provisioned, `cluster.main_device` stays the live authority, and no executor
enforces these epochs. It must not be described as failover.
"""
