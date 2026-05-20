# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Integration tests for v0.2.21 hub + resolver + access-matrix.

These tests differ from ``tests/test_*.py`` in that they actually
spawn the ``vct-hub`` binary, write a real launcher.db, and exercise
end-to-end resolver responses. They share a per-run sandbox via
:mod:`tests.common.sandbox` so concurrent CI runs cannot collide and
no real ``~/.vct/`` state is touched."""
