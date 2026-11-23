"""Construction and transport access for the installed jmcomic SDK.

Importing this module does not initialize logging, a database, clients or network
connections. Callers own their clients, retry order, budgets and shutdown.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, wait
from functools import lru_cache
import math
import threading
from urllib.parse import urljoin, urlsplit

_configure_lock = threading.RLock()
_diagnostic_domain = None


@lru_cache(maxsize=None)
def _option_type(base):
    class ManagedOption(base):
        @classmethod
        def merge_default_dict(cls, user_dict, default_dict=None):
            """SDK defaults borrow DEFAULT_PROXIES by reference; never merge into it."""
            if default_dict is None:
                default_dict = cls.default_dict()
                defaults = default_dict["client"]["postman"]["meta_data"]
                metadata = user_dict.get("client", {}).get("postman", {}).get("meta_data", {})
                defaults["proxies"] = None if "proxies" in metadata else deepcopy(defaults.get("proxies"))
            return super().merge_default_dict(user_dict, default_dict)

        def new_jm_client(self, *args, **kwargs):
            return new_client(self, *args, **kwargs)

    return ManagedOption


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
        from jmcomic import JmImageTool, JmModuleConfig
        JmModuleConfig.FLAG_ENABLE_JM_LOG = True
        JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = False
        JmModuleConfig.FLAG_API_CLIENT_REQUIRE_COOKIES = False
        # Guard JPEG saves when image_format=jpg encounters RGBA/LA/P source frames.
        if not getattr(JmImageTool.save_image, "_atelier_rgb_guard", False):
            _orig_save_image = JmImageTool.save_image.__func__

            def _safe_save_image(cls, image, filepath: str):
                if (
                    str(filepath).lower().endswith((".jpg", ".jpeg"))
                    and getattr(image, "mode", None) not in ("RGB", "L", None)
                ):
                    image = image.convert("RGB")
                return _orig_save_image(cls, image, filepath)

            _safe_save_image._atelier_rgb_guard = True
            _wrapped_save_image = classmethod(_safe_save_image)
            _wrapped_save_image._atelier_rgb_guard = True
            JmImageTool.save_image = _wrapped_save_image
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
    return _option_type(JmModuleConfig.option_class()).construct(deepcopy(config))


def discovery_deadline(option, timeout=None):
    from . import metadata_budget
    saved = float(option.client.postman.meta_data.get("timeout", 30))
    seconds = min(saved, float(timeout) if timeout is not None else saved, 120.0)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("域名发现需要有限的正超时")
    return metadata_budget.deadline_after(seconds)


def close_transport(owner):
    root = owner.get_root_postman()
    for target in (getattr(root, "session", None), root, owner):
        close = getattr(target, "close", None)
        if close is not None:
            close()
            return


def publish_diagnostic_domain(option=None):
    """Mirror only the winning option's resolved domain for the existing diagnostic reader."""
    from jmcomic import JmModuleConfig
    global _diagnostic_domain
    with _configure_lock:
        known = getattr(JmModuleConfig, "DOMAIN_HTML", None)
        if known and known != _diagnostic_domain:
            return
        if option is None:
            if _diagnostic_domain is not None:
                JmModuleConfig.DOMAIN_HTML = None
            _diagnostic_domain = None
        else:
            future = getattr(option, "_html_discovery", None)
            if future and future.done() and not future.cancelled() and future.exception() is None:
                JmModuleConfig.DOMAIN_HTML = _diagnostic_domain = future.result()


def _discover_html(config, deadline, stop):
    """Follow the SDK's permanent (301) hops to its terminal 302, on one budget."""
    from common import Postmans
    from curl_cffi import CurlOpt
    from jmcomic import JmModuleConfig, ResponseUnexpectedException
    from .metadata_budget import remaining
    from .upstream import UpstreamFailure
    metadata = config["meta_data"]
    proxy_options = {key: value for key, value in (metadata.get("curl_options") or {}).items()
                     if key in (CurlOpt.NOPROXY, CurlOpt.PROXY)}
    probe = Postmans.create(data={"type": "curl_cffi_session", "meta_data": {
        "impersonate": metadata.get("impersonate"), "proxies": metadata.get("proxies"),
        "headers": JmModuleConfig.new_html_headers(), "curl_options": proxy_options,
        "timeout": remaining(deadline, stop),
    }})
    try:
        url = JmModuleConfig.JM_REDIRECT_URL
        for _ in range(8):
            response = probe.get(url, allow_redirects=False, timeout=remaining(deadline, stop))
            remaining(deadline, stop)
            location = response.headers.get("Location")
            if response.status_code >= 400:
                raise ResponseUnexpectedException("网页域名发现请求失败", {"resp": response})
            if response.status_code not in (301, 302) or not location:
                raise UpstreamFailure("invalid_response", "网页域名发现未返回有效重定向")
            try:
                url = urljoin(url, location)
                target = urlsplit(url)
                if (target.scheme not in ("http", "https") or not target.hostname or target.username is not None
                        or target.port == 0):
                    raise ValueError
            except ValueError:
                raise UpstreamFailure("invalid_response", "网页域名发现返回无效地址") from None
            if response.status_code == 302:
                return target.netloc
        raise UpstreamFailure("invalid_response", "网页域名发现重定向过多")
    finally:
        close_transport(probe)


def _html_domain(option, config, deadline):
    """One owned flight per option generation; no waiting or transport under a lock."""
    from . import background_calls, metadata_budget
    from .logger import bind_request_id
    stop = background_calls.stop_event()
    metadata_budget.remaining(deadline, stop)
    with _configure_lock:
        future = getattr(option, "_html_discovery", None)
        if future is None or (future.done() and (future.cancelled() or future.exception() is not None)):
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="html-discovery")
            try:
                future = background_calls.submit(pool, bind_request_id(_discover_html), deepcopy(config), deadline, stop)
                option._html_discovery = future
            finally:
                pool.shutdown(wait=False)
    while not future.done():
        wait((future,), timeout=min(0.05, metadata_budget.remaining(deadline, stop)))
    metadata_budget.remaining(deadline, stop)
    return future.result()


def new_client(option, domain_list=None, impl=None, cache=None, domain_retry_strategy=None,
               *, image_only=False, deadline=None, **overrides):
    """Resolve domains before allocating a client session. Transfer ownership only on success.

    JmOption.new_jm_client allocates its postman before domain discovery and does
    not close it if discovery, the client constructor, or cache setup raises.
    """
    from common import Postmans, str_to_list
    from jmcomic import CacheRegistry, JmModuleConfig, JmHtmlClient
    configure_sdk()
    impl = impl or option.client.impl
    client_type = impl if isinstance(impl, type) else JmModuleConfig.client_impl_class(impl)
    config = deepcopy(option.client.postman.src_dict)
    config["meta_data"].update(overrides)
    domains = option.client.get("domain", []) if domain_list is None else domain_list
    if not isinstance(domains, (list, tuple, str)):
        domains = domains.get(client_type.client_key, [])
    if isinstance(domains, str):
        domains = str_to_list(domains)
    if not domains:
        if image_only:
            domains = image_domains()
        elif issubclass(client_type, JmHtmlClient):
            domains = JmModuleConfig.DOMAIN_HTML_LIST
            if not domains:
                with _configure_lock:
                    known = getattr(JmModuleConfig, "DOMAIN_HTML", None)
                    if known == _diagnostic_domain:
                        known = None
                if not known:
                    limit = discovery_deadline(option, overrides.get("timeout"))
                    known = _html_domain(option, config, min(deadline, limit) if deadline is not None else limit)
                domains = [known]
        else:
            domains = option.decide_client_domain(client_type.client_key)

    postman = Postmans.create(data=config)
    try:
        client = client_type(postman=postman, domain_list=list(domains), retry_times=option.client.retry_times,
                             domain_retry_strategy=domain_retry_strategy)
        CacheRegistry.enable_client_cache_on_condition(option, client, option.client.cache if cache is None else cache)
        return client
    except BaseException:
        close_transport(postman)
        raise


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
