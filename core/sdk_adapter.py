"""Construction and transport access for the installed jmcomic SDK.

Importing this module does not initialize logging, a database, clients or network
connections. Callers own their clients, retry order, budgets and shutdown.
"""
from copy import deepcopy
import threading

_configure_lock = threading.RLock()


def configure_sdk() -> None:
    """Apply the same explicit, repeatable SDK policy for every application entry.

    Keep constructor-time API domain updates and /setting cookie requests disabled.
    Domain selection itself stays with the SDK and follows the configured impl.
    No client is created and no domain discovery is performed here.
    """
    with _configure_lock:
        # Claim the library logger before importing the SDK so its default stdout
        # handler is never needed; claiming twice also handles the opposite order.
        from .logger import claim_library_loggers
        from jmcomic import JmModuleConfig
        JmModuleConfig.FLAG_ENABLE_JM_LOG = True
        JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = False
        JmModuleConfig.FLAG_API_CLIENT_REQUIRE_COOKIES = False
        claim_library_loggers()


def build_option(config: dict | None = None):
    """Construct a fresh option from shared settings, or an explicit config snapshot.

    Caller-owned dictionaries are copied because SDK defaults/plugins may mutate
    their configuration. This function does not initialize the settings database;
    the application entry point owns that lifecycle.
    """
    configure_sdk()
    from jmcomic import JmModuleConfig
    if config is None:
        from .settings import build_jmcomic_option
        config = build_jmcomic_option()
    return JmModuleConfig.option_class().construct(deepcopy(config))


def new_client(option, **overrides):
    """Create one client, leaving ownership and closing to the caller.

    SDK arguments such as cache, domain_list, timeout and session settings retain
    their normal meaning; per-operation transport overrides do not mutate option.
    HTML without configured/known domains still uses the SDK's normal discovery.
    """
    configure_sdk()
    return option.new_jm_client(**overrides)


def cached_client(option):
    """Get the option client also used by SDK top-level download entry points.

    The CLI must close the transport actually used by download_album/download_batch,
    rather than construct a separate unused client. Application-level caching and
    leases are still owned by jm_service and do not use this helper.
    """
    configure_sdk()
    return option.build_jm_client()


def transport_and_session(client):
    """Return the root GET/POST transport and the session it actually uses."""
    root = client.get_root_postman()
    return root, root.session


def client_domains(client) -> tuple[str, ...]:
    """Snapshot distinct nodes already selected by this client, in SDK order."""
    return tuple(dict.fromkeys(domain for domain in (client.get_domain_list() or ()) if domain))


def select_client_domain(client, domain: str) -> None:
    """Restrict an exclusively owned check client to one node for its next call."""
    client.set_domain_list([domain])


def image_domains() -> tuple[str, ...]:
    """Read the SDK's current image hosts, independent of the metadata client."""
    from jmcomic import JmModuleConfig
    return tuple(JmModuleConfig.DOMAIN_IMAGE_LIST)


def get_image_once(client, url: str, timeout):
    """Issue one postman GET with the SDK's image headers, outside its client retry loop."""
    from jmcomic import JmModuleConfig
    kwargs = {"headers": JmModuleConfig.new_html_headers()}
    client.update_request_with_specify_domain(kwargs, None, True)
    return client.postman.get(url, timeout=timeout, **kwargs)
