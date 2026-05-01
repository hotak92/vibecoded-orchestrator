"""vco_lib — single source of truth for VibeCoded Tools project init/update.

Modules:
    project_init — sanitization, schema definitions, collection-name
                   derivation, drift detection, rebuild dispatch.

This package is callable both from Python (`import vco_lib.project_init`)
and from Rust (subprocess: `python -m vco_lib.project_init <subcommand>`).
"""
