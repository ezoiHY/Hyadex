"""Job context uses each caller's provider policy without application globals."""
import unittest

from fastapi import HTTPException
from hyadex.jobs.context import resolve_job_context_contract


class JobContextPolicyTests(unittest.TestCase):
    def test_different_runtime_policies_do_not_share_default_or_allowlist(self):
        def resolve(default, enabled, backend=None):
            return resolve_job_context_contract(
                {}, "chat", backend, context_mode_explicit=True,
                enabled_backends=enabled, default_backend=default,
            )

        self.assertEqual(resolve("claude", {"claude"}, "claude"), ("chat", "claude"))
        self.assertEqual(resolve("codex", {"codex"}, "codex"), ("chat", "codex"))
        with self.assertRaisesRegex(HTTPException, "backend must be one of"):
            resolve("claude", {"claude"}, "codex")
        self.assertEqual(resolve("claude", {"claude"}), ("chat", None))

    def test_legacy_cross_provider_omission_and_explicit_chat_keep_their_contract(self):
        kwargs = {"enabled_backends": {"claude", "codex"}, "default_backend": "claude"}
        self.assertEqual(resolve_job_context_contract(
            {"backend": "claude"}, None, "codex", context_mode_explicit=False, **kwargs,
        ), ("standalone", "codex"))
        with self.assertRaisesRegex(HTTPException, "must use the parent chat"):
            resolve_job_context_contract(
                {"backend": "claude"}, "chat", "codex", context_mode_explicit=True, **kwargs,
            )
