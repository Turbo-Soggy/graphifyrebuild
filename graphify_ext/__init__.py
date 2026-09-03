"""graphify-ext: customization layer on top of stock graphify.

Requirement 1: per-branch incremental graph caching (branch_cache, hooks_ext).
Requirement 2: AppSec fix-context layer (blast_radius, edge_inject,
               config_link, test_link, triage, verify_fix).
"""

__version__ = "0.1.0"

# Edges written into graph.json by this package carry this origin marker so
# they can be removed/re-applied idempotently and never mistaken for
# extractor output.
EXT_ORIGIN = "graphify-ext"
