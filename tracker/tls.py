"""HTTPS trust that works on every install.

Python trusts only certificates it can read directly. The Windows store holds just the roots Windows
has needed so far, and a packaged macOS app looks in an OpenSSL folder of the machine that built it.
Browsers verify on their own, so a site can open fine while the app fails with
CERTIFICATE_VERIFY_FAILED. assets/cacert.pem is Mozilla's root list; it is added to the system's
roots rather than replacing them, so a company or school root keeps working.
"""
import ssl
import urllib.request

from .paths import APP_DIR


def context():
    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations(cafile=str(APP_DIR / "assets" / "cacert.pem"))
    except (OSError, ssl.SSLError):
        pass
    return ctx


def install():
    """Every urlopen in this process uses these roots from now on."""
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.HTTPSHandler(context=context())))
