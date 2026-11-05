"""NerveShield Phase 3, task 3.2: CPU classifier baseline + synthetic harness.

Everything in this package works on SYNTHETIC, template-generated feature
records. Quality numbers computed here say nothing about real-world detection,
and timings measured on a development machine are not representative of the
GTi14 CPU or the Intel Arc GPU. Classifier output is an UNCALIBRATED raw score
and is never an authority (I-08, R-007.2). This package is intentionally not
imported by the worker, policy, API or startup code.
"""
