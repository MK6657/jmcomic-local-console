"""Explicit owned transport shape for SDK fakes used by download behavior tests.

Pure metadata/image fakes do their work locally. An accidental transport request
still fails: this helper does not bypass transfer budgets or permit networking.
"""
from types import SimpleNamespace


def owned_client(client=None, *, root=None):
    """Give a particular fake its own mutable session, as the real client contract requires."""
    client = SimpleNamespace() if client is None else client
    def unexpected_request(*args, **kwargs):
        raise AssertionError("This local SDK fake must not make transport requests")
    if root is None:
        root = SimpleNamespace(get=unexpected_request, post=unexpected_request)
    if not hasattr(root, "get"):
        root.get = unexpected_request
    if not hasattr(root, "post"):
        root.post = unexpected_request
    root.session = SimpleNamespace(curl_options={}, retry=None, close=lambda: None)
    client._jm_downloader_shared = False
    client.get_root_postman = lambda: root
    return client
