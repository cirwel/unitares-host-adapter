"""Host-specific bindings for the UNITARES host adapter.

Each binding module maps the three UnitaresAdapter delivery modes
(explicit / ambient / gated) onto a host's lifecycle hook taxonomy.

Bindings should stay thin — roughly 30-80 lines each. Logic belongs in core.
"""
