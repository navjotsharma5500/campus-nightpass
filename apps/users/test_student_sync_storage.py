import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from django.core import signing
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from .services.student_sync_storage import (
    claim_preview, cleanup_expired, create_preview, decode_token, preview_token, read_preview,
)


class PreviewStorageTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        overrides = override_settings(STUDENT_SYNC_TEMP_DIR=directory.name)
        overrides.enable()
        self.addCleanup(overrides.disable)
        self.payload = {"headers": ["email"], "rows": [{"email": "test@example.com"}], "mode": "full"}
        self.metadata = create_preview(self.payload, 1, "test-session")

    def test_another_process_can_read_preview(self):
        code = (
            "import django, json, sys; django.setup(); from django.conf import settings; "
            "settings.STUDENT_SYNC_TEMP_DIR=sys.argv[1]; "
            "from apps.users.services.student_sync_storage import read_preview; "
            "print(json.dumps(read_preview(json.loads(sys.argv[2]))))"
        )
        result = subprocess.run([sys.executable, "-c", code, str(self.directory), json.dumps(self.metadata)],
                                env={**os.environ, "DJANGO_SETTINGS_MODULE": os.environ.get("DJANGO_SETTINGS_MODULE", "core.settings")},
                                check=True, capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(result.stdout), self.payload)

    def test_claim_blocks_concurrent_apply_and_restores_after_failure(self):
        with self.assertRaisesRegex(RuntimeError, "failure"):
            with claim_preview(self.metadata) as payload:
                self.assertEqual(payload, self.payload)
                with self.assertRaises(signing.BadSignature):
                    with claim_preview(self.metadata):
                        self.fail("Second claim must not succeed")
                raise RuntimeError("failure")
        self.assertEqual(read_preview(self.metadata), self.payload)
        with claim_preview(self.metadata):
            pass
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_safe_stale_file_cleanup_command(self):
        old = time.time() - 2000
        expired = self.directory / (self.metadata["id"] + ".json")
        os.utime(expired, (old, old))
        unrelated = self.directory / "unrelated.json"
        unrelated.write_text("preserve")
        os.utime(unrelated, (old, old))
        active = create_preview(self.payload, 1, "test-session")  # Opportunistic cleanup.
        self.assertFalse(expired.exists())
        claim = self.directory / ("a" * 64 + ".applying")
        claim.write_text("active")
        os.utime(claim, (old, old))
        output = io.StringIO()
        call_command("cleanup_student_sync", stdout=output)
        self.assertTrue(claim.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(read_preview(active), self.payload)
        os.utime(claim, (old - 86400, old - 86400))
        self.assertEqual(cleanup_expired(), 1)
        self.assertFalse(claim.exists())

    def test_signed_path_injection_is_rejected(self):
        token = preview_token({**self.metadata, "id": "../../outside"}, {})
        with self.assertRaises(signing.BadSignature):
            decode_token(token, 1, "test-session")

    def test_file_integrity_and_missing_file_fail_safely(self):
        path = self.directory / (self.metadata["id"] + ".json")
        path.write_text('{"rows": []}')
        with self.assertRaises(signing.BadSignature):
            read_preview(self.metadata)
        path.unlink()
        with self.assertRaises(signing.BadSignature):
            read_preview(self.metadata)

    def test_new_options_cannot_extend_original_expiry(self):
        with patch("apps.users.services.student_sync_storage.time.time", return_value=time.time() + 1801):
            token = preview_token(self.metadata, {"clear": "all"})
            with self.assertRaises(signing.SignatureExpired):
                decode_token(token, 1, "test-session")

    def test_cleanup_failure_does_not_report_committed_apply_as_failed(self):
        with self.assertLogs("apps.users.student_sync", level="ERROR"), patch.object(Path, "unlink", side_effect=OSError("disk error")):
            with claim_preview(self.metadata):
                pass
        self.assertEqual(len(list(self.directory.glob("*.applying"))), 1)
