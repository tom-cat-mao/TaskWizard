"""Recall-domain tests: the semantic index, lesson injection and repair.

The recall layer is observe-only by default (``shadow``) and injects only
approved lessons in ``on`` mode (P0 #16a). These files pin the selection path,
the embedding warm-up, the replay/exemplar metric, the outcome audit and the
offline repair packages. Everything is local: hash embedders, fake vectors,
``tmp_path`` stores.
"""
