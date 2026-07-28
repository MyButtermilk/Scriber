# Python Runtime AMD v1

This profile is the AMD-only continuation of `python-runtime-ab-v1`. It uses
the same frozen variant matrix, sample counts, practical gain threshold,
bootstrap confidence, resource limits, zero-error rules, and tie-break order.
It requires one AMD screening block and two independent, counterbalanced AMD
FullLocal clean-install/reboot blocks.

The profile produces useful evidence for the pinned AMD host, but it is not
cross-vendor release evidence. Its decision therefore always records
`scope=amd-only` and `productionPromotionAuthorized=false`. It must not replace
the universal production default or be merged into the normal AutoResearch
baseline/champion data. An AMD result can nominate a variant for further work;
the existing `python-runtime-ab-v1` remains the contract for a universal
Intel/AMD promotion.

Missing Intel hardware is represented honestly by this narrower profile, never
by copied, synthesized, or zero-valued Intel measurements.

The stock PyInstaller 6.20 frozen interpreter ignores `PYTHON_JIT` because it
uses CPython's isolated initialization. O1/C1/T1 consequently fail the runtime
policy and are not eligible candidates for this profile unless a separate,
reviewed native-launcher contract can switch only the JIT state without
enabling the rest of Python's process environment. Source-runtime JIT results
must not be substituted for installed frozen evidence.
