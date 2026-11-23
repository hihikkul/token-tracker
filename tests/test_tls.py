"""HTTPS context: strict RFC 5280 checks off (proxy-issued certificates), verification still on."""
import ssl
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker import tls


class TlsContext(unittest.TestCase):
    def test_strict_x509_checks_are_off_but_verification_stays_on(self):
        ctx = tls.context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertFalse(ctx.verify_flags & getattr(ssl, "VERIFY_X509_STRICT", 0))


if __name__ == "__main__":
    unittest.main()
