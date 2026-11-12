# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Offline training tools for the shipped RL reranker weights.

Maintainer- and volunteer-side, never part of the reranker image (the root
``.dockerignore`` keeps this directory out of the build context):

- ``export``        — launcher.db (read-only) -> text-free float16 shards
                      (every field documented in ``FIELDS.md``);
- ``shard_trainer`` — train + evaluate one profile from shards; ``ship``
                      installs passing checkpoints into ``weights/``;
- ``eval_harness``  — NDCG/MRR/Recall vs plain cosine ordering and the
                      owner's unlock bar;
- ``transfer``      — leave-one-project-out training (``lopo``) and the
                      online per-project adaptation simulation (``adapt``).
"""
