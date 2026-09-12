"""Fail-closed configuration and supervision for the universal hub role."""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from proxy.render_config import ProxyConfigError, render_outputs


class HubConfigError(ValueError):
    """The universal runtime environment is incomplete or ambiguous."""


_READERS = ("cwa", "kavita")
_INTERNAL_PORTS = {
    "cwa": (8391, 8080),
    "kavita": (8392, 8081),
}
_PROVIDER_OVERRIDES = {
    "LLM_PROVIDER": "LLM_PROVIDER",
    "LLM_MODEL": "LLM_MODEL",
    "LLM_API_KEY": "LLM_API_KEY",
    "LLM_CUSTOM_ENDPOINT": "LLM_CUSTOM_ENDPOINT",
    "LLM_CUSTOM_API_KEY": "LLM_CUSTOM_API_KEY",
    "LLM_FALLBACK_PROVIDER": "LLM_FALLBACK_PROVIDER",
    "LLM_FALLBACK_MODEL": "LLM_FALLBACK_MODEL",
    "LLM_FALLBACK_API_KEY": "LLM_FALLBACK_API_KEY",
    "LLM_FALLBACK_CUSTOM_ENDPOINT": "LLM_FALLBACK_CUSTOM_ENDPOINT",
    "LLM_FALLBACK_CUSTOM_API_KEY": "LLM_FALLBACK_CUSTOM_API_KEY",
    "LOCAL_URL": "BT_LOCAL_URL",
}
_PROCESS_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPATH",
    "TZ",
    "BT_ALLOW_PRIVATE_LAN",
    "BT_API_TOKEN",
    "BT_AUTH_MAX_INFLIGHT_PER_CLIENT",
    "BT_AUTH_RATE_LIMIT_PER_MINUTE",
    "BT_BATCH_MAX_TOKENS",
    "BT_BATCH_SIZE",
    "BT_BATCH_SOURCE_TOKEN_BUDGET",
    "BT_CACHE_HARDEN_EXISTING_DIR",
    "BT_CACHE_HIT_FLUSH_THRESHOLD",
    "BT_CACHE_MAX_ENTRIES",
    "BT_CACHE_OPERATOR_GROUP_ACCESS",
    "BT_CACHE_SCOPE_MAX_CHARS",
    "BT_CACHE_TTL_DAYS",
    "BT_CONTEXT_WINDOW",
    "BT_CLIENT_PREFETCH_GAP_MS",
    "BT_MAX_BATCH_PARAGRAPHS",
    "BT_MAX_CONTENT_LENGTH",
    "BT_MAX_PARAGRAPH_CHARS",
    "BT_MAX_TOKENS",
    "BT_MAX_UPSTREAM_RESPONSE_BYTES",
    "BT_OUTPUT_TOKEN_FACTOR",
    "BT_OUTPUT_TOKEN_FLOOR",
    "BT_RATE_LIMIT_MAX_CLIENTS",
    "BT_RATE_LIMIT_PER_MINUTE",
    "BT_RATE_LIMIT_RETRY_AFTER",
    "BT_REQUEST_DEADLINE_SECONDS",
    "BT_REQUEST_MAX_ATTEMPTS",
    "BT_REQUEST_MAX_INPUT_BYTES",
    "BT_REQUEST_MAX_OUTPUT_TOKENS",
    "BT_READER_AUTH_MAX_RESPONSE_BYTES",
    "BT_READER_AUTH_TIMEOUT_SECONDS",
    "BT_SESSION_MAX_ENTRIES",
    "BT_SESSION_TTL_SECONDS",
    "BT_SINGLEFLIGHT_MAX_ENTRIES",
    "BT_TIMEOUT",
    "BT_UPSTREAM_QUEUE_TIMEOUT",
    *_PROVIDER_OVERRIDES.values(),
}


def _boolean(source: Mapping[str, str], name: str) -> bool:
    value = source.get(name, "false")
    if value not in {"true", "false"}:
        raise HubConfigError(f"{name} must be exactly true or false")
    return value == "true"


def _required(source: Mapping[str, str], name: str) -> str:
    value = source.get(name, "")
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise HubConfigError(f"{name} must be one clean non-empty ASCII value")
    return value


def _positive_int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name, str(default))
    if not isinstance(raw, str) or not raw.isdecimal():
        raise HubConfigError(f"{name} must be a positive integer")
    value = int(raw, 10)
    if value <= 0:
        raise HubConfigError(f"{name} must be a positive integer")
    return value


def _bounded_int(
    source: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = source.get(name, str(default))
    if not isinstance(raw, str) or not raw.isdecimal():
        raise HubConfigError(
            f"{name} must be an integer from {minimum} to {maximum}"
        )
    value = int(raw, 10)
    if not minimum <= value <= maximum:
        raise HubConfigError(
            f"{name} must be an integer from {minimum} to {maximum}"
        )
    return value


def _origin(source: Mapping[str, str], name: str, *, upstream_port: int | None = None) -> str:
    raw = _required(source, name)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise HubConfigError(f"{name} must be one exact http(s) origin") from exc
    hostname = parsed.hostname or ""
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise HubConfigError(f"{name} must be one exact http(s) origin")
    if upstream_port is not None and parsed.scheme != "http":
        raise HubConfigError(f"{name} must use http for upstream connection")
    host_only = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if parsed.scheme == "https" else 80
    if port is not None and upstream_port is None and port == default_port:
        port = None
    if port is not None:
        normalized_host = f"{host_only}:{port}"
        acceptable = {normalized_host.casefold()}
    elif upstream_port is not None:
        normalized_host = f"{host_only}:{upstream_port}"
        acceptable = {normalized_host.casefold(), host_only.casefold()}
    else:
        normalized_host = host_only
        acceptable = {
            normalized_host.casefold(),
            f"{host_only}:{default_port}".casefold(),
        }
    if parsed.netloc.casefold() not in acceptable:
        raise HubConfigError(f"{name} must contain one exact authority")
    if upstream_port is None and parsed.scheme != "https":
        loopback = hostname.casefold() == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
        if not loopback:
            raise HubConfigError(f"{name} requires HTTPS outside loopback development")
    return f"{parsed.scheme}://{normalized_host}"


def _connector_id(source: Mapping[str, str], name: str) -> str:
    value = _required(source, name)
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise HubConfigError(f"{name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise HubConfigError(f"{name} must be a canonical UUID")
    return value


def _provider_environment(source: Mapping[str, str], reader: str) -> dict[str, str]:
    environment = {
        name: value
        for name, value in source.items()
        if name in _PROCESS_KEYS and isinstance(value, str)
    }
    prefix = f"BT_{reader.upper()}_"
    for override, target in _PROVIDER_OVERRIDES.items():
        name = prefix + override
        if name in source:
            value = source[name]
            if not isinstance(value, str):
                raise HubConfigError(f"{name} must be a string")
            environment[target] = value
    return environment


def _validate_provider_environment(environment: Mapping[str, str], reader: str) -> None:
    providers = {
        "local", "openai", "anthropic", "gemini", "groq", "together",
        "minimax", "deepseek", "openrouter", "openai-compatible",
    }

    def validate_role(prefix: str, *, fallback: bool) -> None:
        provider = environment.get(prefix + "PROVIDER", "")
        model = environment.get(prefix + "MODEL", "")
        api_key = environment.get(prefix + "API_KEY", "").strip()
        custom_endpoint = environment.get(prefix + "CUSTOM_ENDPOINT", "")
        custom_key = environment.get(prefix + "CUSTOM_API_KEY", "").strip()
        if fallback and not provider and not model and not api_key and not custom_endpoint and not custom_key:
            return
        if provider not in providers or not model:
            raise HubConfigError(f"{reader} provider and model must be explicit and supported")
        if provider == "local":
            local_url = environment.get("BT_LOCAL_URL", "")
            try:
                parsed = urlsplit(local_url)
            except ValueError as exc:
                raise HubConfigError(f"{reader} local provider URL is invalid") from exc
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path != "/v1/chat/completions"
                or parsed.query
                or parsed.fragment
                or api_key
                or custom_endpoint
                or custom_key
            ):
                raise HubConfigError(
                    f"{reader} local provider requires one exact local endpoint and no key"
                )
        elif provider == "openai-compatible":
            try:
                parsed = urlsplit(custom_endpoint)
            except ValueError as exc:
                raise HubConfigError(f"{reader} custom provider endpoint is invalid") from exc
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path != "/v1/chat/completions"
                or parsed.query
                or parsed.fragment
                or api_key
                or not custom_key
            ):
                raise HubConfigError(
                    f"{reader} custom provider requires HTTPS endpoint and dedicated key"
                )
        elif not api_key or custom_endpoint or custom_key:
            raise HubConfigError(
                f"{reader} remote provider requires its generic API key only"
            )

    validate_role("LLM_", fallback=False)
    validate_role("LLM_FALLBACK_", fallback=True)


def _allocations(
    source: Mapping[str, str], readers: tuple[str, ...], name: str, default: int
) -> dict[str, int]:
    total = _positive_int(source, name, default)
    override_names = {reader: f"BT_{reader.upper()}_{name[3:]}" for reader in readers}
    present = [reader for reader, override in override_names.items() if override in source]
    if present:
        if len(present) != len(readers):
            raise HubConfigError(f"all enabled readers must define a {name} allocation")
        values = {
            reader: _positive_int(source, override_names[reader], 1)
            for reader in readers
        }
        if sum(values.values()) > total:
            raise HubConfigError(f"per-reader {name} allocations exceed the hub total")
        return values
    if len(readers) == 1:
        return {readers[0]: total}
    if total < len(readers):
        raise HubConfigError(f"{name} must provide at least one slot per enabled reader")
    base, remainder = divmod(total, len(readers))
    return {
        reader: base + (1 if index < remainder else 0)
        for index, reader in enumerate(readers)
    }


@dataclass(frozen=True, slots=True)
class ReaderRuntime:
    name: str
    api_port: int
    proxy_port: int
    published_port: int
    public_origin: str
    upstream: str
    version: str
    auth_profile: str
    environment: dict[str, str] = field(repr=False)
    proxy_environment: dict[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    name: str
    argv: tuple[str, ...]
    environment: dict[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class HubConfig:
    readers: tuple[ReaderRuntime, ...]

    @classmethod
    def from_environment(cls, source: Mapping[str, str] | None = None) -> "HubConfig":
        values = source if source is not None else os.environ
        enabled = tuple(
            reader
            for reader in _READERS
            if _boolean(values, f"BT_ENABLE_{reader.upper()}")
        )
        if not enabled:
            raise HubConfigError("at least one reader must be enabled")

        concurrency = _allocations(values, enabled, "BT_MAX_CONCURRENT", 2)
        upstream_inflight = _allocations(
            values, enabled, "BT_MAX_UPSTREAM_INFLIGHT", 2
        )
        batch_size = _bounded_int(values, "BT_BATCH_SIZE", 5, 1, 50)
        batch_source_token_budget = _bounded_int(
            values, "BT_BATCH_SOURCE_TOKEN_BUDGET", 0, 0, 1_000_000
        )
        batch_max_tokens = _bounded_int(
            values, "BT_BATCH_MAX_TOKENS", 8192, 1, 1_000_000
        )
        client_prefetch_gap_ms = _bounded_int(
            values, "BT_CLIENT_PREFETCH_GAP_MS", 0, 0, 10_000
        )
        max_batch_paragraphs = _bounded_int(
            values, "BT_MAX_BATCH_PARAGRAPHS", 50, 1, 1_000
        )
        if batch_size > max_batch_paragraphs:
            raise HubConfigError(
                "BT_BATCH_SIZE must not exceed BT_MAX_BATCH_PARAGRAPHS"
            )
        runtimes: list[ReaderRuntime] = []
        published_ports: set[int] = set()
        for reader in enabled:
            prefix = f"BT_{reader.upper()}_"
            expected_upstream_port = 8083 if reader == "cwa" else 5000
            public_origin = _origin(values, prefix + "PUBLIC_ORIGIN")
            # Support CWA_URL and KAVITA_URL aliases if READER_UPSTREAM is not set
            upstream_key = prefix + "READER_UPSTREAM"
            if upstream_key not in values:
                if reader == "cwa" and ("CWA_URL" in values or "CWA_UPSTREAM" in values):
                    values = dict(values)
                    values[upstream_key] = values.get("CWA_URL") or values.get("CWA_UPSTREAM")
                elif reader == "kavita" and ("KAVITA_URL" in values or "KAVITA_UPSTREAM" in values):
                    values = dict(values)
                    values[upstream_key] = values.get("KAVITA_URL") or values.get("KAVITA_UPSTREAM")
            upstream = _origin(
                values,
                upstream_key,
                upstream_port=expected_upstream_port,
            )
            version = _required(values, prefix + "READER_VERSION")
            if reader == "kavita" and version != "0.9.0.2":
                raise HubConfigError("Kavita reader version is not certified")
            if reader == "cwa" and not (
                re.fullmatch(r"4\.[0-9]+\.[0-9]+", version) or version == "3.1.4"
            ):
                raise HubConfigError("CWA reader version is unsupported")
            auth_profile = _required(values, prefix + "AUTH_PROFILE")
            supported_auth = (
                {"reader-session"}
                if reader == "cwa"
                else {"reader-session"}
            )
            if auth_profile not in supported_auth:
                raise HubConfigError(f"{prefix}AUTH_PROFILE is unsupported")
            connector_id = _connector_id(values, prefix + "READER_CONNECTOR_ID")
            published_port = _positive_int(values, prefix + "PUBLISHED_PORT", 0)
            if published_port > 65535 or published_port in published_ports:
                raise HubConfigError("enabled reader published ports must be distinct")
            published_ports.add(published_port)
            api_port, proxy_port = _INTERNAL_PORTS[reader]

            environment = _provider_environment(values, reader)
            _validate_provider_environment(environment, reader)
            environment.update(
                {
                    "PORT": str(api_port),
                    "DB_PATH": f"/app/data/{reader}/translations.db",
                    "BT_CACHE_DIR": f"/app/data/{reader}",
                    "BT_AUTH_MODE": (
                        "forwarded"
                        if auth_profile == "authentik-forwarded"
                        else "reader_session"
                    ),
                    "BT_READER_TYPE": reader,
                    "BT_READER_AUTH_URL": (
                        upstream + "/ajax/emailstat"
                        if reader == "cwa"
                        else upstream + "/api/Account"
                    ),
                    "BT_READER_VERSION": version,
                    "BT_READER_CONTRACT_VERSION": (
                        "cwa-epub-v1"
                        if reader == "cwa"
                        else "kavita-0.9.0.2-epub-v1"
                    ),
                    "BT_READER_CONNECTOR_ID": connector_id,
                    "BT_PUBLIC_ORIGIN": public_origin,
                    "BT_SESSION_KEY_PATH": f"/app/data/{reader}/reader_session_key",
                    "BT_SESSION_COOKIE_NAME": (
                        f"__Host-bt-{reader}-session"
                        if public_origin.startswith("https://")
                        else f"bt-{reader}-session"
                    ),
                    "BT_TRUSTED_PROXIES": "127.0.0.1/32",
                    "BT_MAX_CONCURRENT": str(concurrency[reader]),
                    "BT_MAX_UPSTREAM_INFLIGHT": str(upstream_inflight[reader]),
                    "BT_BATCH_SIZE": str(batch_size),
                    "BT_BATCH_SOURCE_TOKEN_BUDGET": str(
                        batch_source_token_budget
                    ),
                    "BT_BATCH_MAX_TOKENS": str(batch_max_tokens),
                    "BT_CLIENT_PREFETCH_GAP_MS": str(
                        client_prefetch_gap_ms
                    ),
                    "BT_MAX_BATCH_PARAGRAPHS": str(max_batch_paragraphs),
                }
            )
            browser_auth = "reader_session"
            proxy_environment = {
                "BT_PROXY_NAMESPACE": reader,
                "BT_PROXY_PORT": str(proxy_port),
                "BT_API_UPSTREAM": f"http://127.0.0.1:{api_port}",
                "BT_PUBLIC_ORIGIN": public_origin,
                "BT_READER_TYPE": reader,
                "BT_READER_UPSTREAM": upstream,
                "BT_READER_VERSION": version,
                "BT_READER_CONTRACT_VERSION": environment[
                    "BT_READER_CONTRACT_VERSION"
                ],
                "BT_BROWSER_AUTH_MODE": browser_auth,
                "BT_BROWSER_CREDENTIALS": (
                    "include" if browser_auth == "forwarded" else "same-origin"
                ),
                "BT_SESSION_COOKIE_NAME": environment["BT_SESSION_COOKIE_NAME"],
                "BT_BROWSER_CONFIG_PATH": f"/tmp/nginx/browser-config-{reader}.json",
                "BT_CWA_MAX_BODY_SIZE": values.get("BT_CWA_MAX_BODY_SIZE", "2g"),
                "BT_CWA_IDENTITY_HEADER": values.get(
                    "BT_CWA_IDENTITY_HEADER", "Remote-User"
                ),
                "BT_UI_VERSION": values.get("BT_UI_VERSION", "dev"),
                "BT_BATCH_SIZE": str(batch_size),
                "BT_CLIENT_PREFETCH_GAP_MS": str(client_prefetch_gap_ms),
            }
            runtimes.append(
                ReaderRuntime(
                    name=reader,
                    api_port=api_port,
                    proxy_port=proxy_port,
                    published_port=published_port,
                    public_origin=public_origin,
                    upstream=upstream,
                    version=version,
                    auth_profile=auth_profile,
                    environment=environment,
                    proxy_environment=proxy_environment,
                )
            )
        return cls(tuple(runtimes))


def process_specs(config: HubConfig) -> tuple[ProcessSpec, ...]:
    """Return the exact fail-fast child process contract for one hub."""
    specs = [
        ProcessSpec(
            name=f"api-{reader.name}",
            argv=(
                "gunicorn",
                "--bind",
                f"127.0.0.1:{reader.api_port}",
                "--workers",
                "1",
                "--threads",
                "8",
                "--worker-tmp-dir",
                "/dev/shm",
                "--timeout",
                "120",
                "server:app",
            ),
            environment=dict(reader.environment),
        )
        for reader in config.readers
    ]
    nginx_environment = {
        name: value
        for name, value in config.readers[0].environment.items()
        if name in {"HOME", "LANG", "LC_ALL", "PATH", "TZ"}
    }
    specs.append(
        ProcessSpec(
            name="nginx",
            argv=(
                "nginx",
                "-c",
                "/app/proxy/nginx-main.conf",
                "-e",
                "/dev/stderr",
                "-g",
                "daemon off;",
            ),
            environment=nginx_environment,
        )
    )
    return tuple(specs)


def _ensure_private_directory(path: Path, *, create: bool) -> None:
    if path.is_symlink():
        raise HubConfigError("hub data directories must not be symbolic links")
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    try:
        metadata = path.stat()
    except OSError as exc:
        raise HubConfigError("hub data directory is unavailable") from exc
    mode = metadata.st_mode & 0o7777
    if (
        not path.is_dir()
        or metadata.st_uid != os.geteuid()
        or mode not in {0o700, 0o750, 0o2700, 0o2750}
    ):
        raise HubConfigError(
            "hub data directories must be owned and privately mode 0700 or 2750"
        )


def prepare_runtime(
    config: HubConfig,
    *,
    data_root: Path = Path("/app/data"),
    runtime_dir: Path = Path("/tmp/nginx"),
    template_path: Path = Path("/app/proxy/nginx.conf.template"),
    runner=subprocess.run,
) -> None:
    """Validate all mutable state and generated config before serving traffic."""
    _ensure_private_directory(data_root, create=False)
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for stale in (
        "proxy-cwa.conf",
        "proxy-kavita.conf",
        "browser-config-cwa.json",
        "browser-config-kavita.json",
    ):
        try:
            (runtime_dir / stale).unlink()
        except FileNotFoundError:
            pass
    for temporary in (
        "client_temp",
        "proxy_temp",
        "fastcgi_temp",
        "uwsgi_temp",
        "scgi_temp",
    ):
        (runtime_dir / temporary).mkdir(mode=0o700, exist_ok=True)

    for reader in config.readers:
        _ensure_private_directory(data_root / reader.name, create=True)
        render_outputs(
            template_path,
            runtime_dir / f"proxy-{reader.name}.conf",
            runtime_dir / f"browser-config-{reader.name}.json",
            reader.proxy_environment,
        )
        runner(
            (
                sys.executable,
                "-c",
                "from cache import init_db; init_db(); import server",
            ),
            env=reader.environment,
            check=True,
        )

    nginx = process_specs(config)[-1]
    runner(
        (
            "nginx",
            "-t",
            "-c",
            "/app/proxy/nginx-main.conf",
            "-e",
            "/dev/stderr",
        ),
        env=nginx.environment,
        check=True,
    )


def _stop_processes(processes: list[object]) -> None:
    for process in processes:
        try:
            process.terminate()
        except OSError:
            pass
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()


def supervise(
    config: HubConfig,
    *,
    popen_factory=subprocess.Popen,
    sleep=time.sleep,
    install_signal_handlers: bool = True,
) -> int:
    """Run every enabled reader and fail the whole container on one child exit."""
    processes: list[object] = []
    stopping = False
    old_handlers: dict[signal.Signals, object] = {}

    def request_stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    if install_signal_handlers:
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
    try:
        specs = process_specs(config)
        for spec in specs:
            print(f"[hub] event=child_start name={spec.name}", file=sys.stderr)
            processes.append(popen_factory(spec.argv, env=spec.environment))
        while not stopping:
            for spec, process in zip(specs, processes):
                returncode = process.poll()
                if returncode is not None:
                    print(
                        f"[hub] event=child_exit name={spec.name} status={returncode}",
                        file=sys.stderr,
                    )
                    _stop_processes(processes)
                    return returncode or 1
            sleep(1)
        _stop_processes(processes)
        return 0
    except BaseException:
        # A failed spawn or a monitor fault must not orphan children that
        # already started: stop everything before propagating the failure.
        _stop_processes(processes)
        raise
    finally:
        if install_signal_handlers:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)


def _ping_reader(proxy_port: int) -> bool:
    """Probe one reader ping endpoint; True only on an exact HTTP 200."""
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{proxy_port}/bt-api/ping",
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=4) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        return False


def healthcheck(config: HubConfig | None = None) -> int:
    selected = config or HubConfig.from_environment()
    ports = [reader.proxy_port for reader in selected.readers]
    if len(ports) <= 1:
        return 0 if all(_ping_reader(port) for port in ports) else 1
    # Readers are independent listeners: probe them concurrently so one slow
    # reader cannot push the whole healthcheck past the orchestrator budget.
    with ThreadPoolExecutor(max_workers=len(ports)) as executor:
        healthy = list(executor.map(_ping_reader, ports))
    return 0 if all(healthy) else 1


def _probe_reader_providers(environment: dict[str, str], script: str, runner) -> int:
    """Probe one reader provider set in an isolated, bounded child process."""
    try:
        result = runner(
            [sys.executable, "-c", script],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1
    return 0 if result.returncode == 0 else 1


def healthcheck_providers(
    config: HubConfig | None = None,
    *,
    runner=subprocess.run,
) -> int:
    """Probe every reader provider in an isolated, bounded child process."""
    selected = config or HubConfig.from_environment()
    script = (
        "from translator import check_backend_health,create_work_budget;"
        "h=check_backend_health(create_work_budget());"
        "raise SystemExit(0 if h and all("
        "v.get('status')=='ok' for v in h.values()) else 3)"
    )
    readers = list(selected.readers)
    if len(readers) <= 1:
        codes = [
            _probe_reader_providers(reader.environment, script, runner)
            for reader in readers
        ]
    else:
        # Provider probes are per-reader isolated subprocesses: run them
        # concurrently so the serial 120s budgets cannot stack past the
        # orchestrator healthcheck timeout.
        with ThreadPoolExecutor(max_workers=len(readers)) as executor:
            codes = list(
                executor.map(
                    lambda reader: _probe_reader_providers(
                        reader.environment, script, runner
                    ),
                    readers,
                )
            )
    return 0 if all(code == 0 for code in codes) else 1


def main(argv: list[str] | None = None) -> int:
    """Validate, preflight, and run the universal one-container topology."""
    arguments = argv if argv is not None else sys.argv[1:]
    try:
        config = HubConfig.from_environment()
        if arguments == ["--healthcheck"]:
            return healthcheck(config)
        if arguments == ["--healthcheck-providers"]:
            return healthcheck_providers(config)
        if arguments:
            raise HubConfigError("unsupported hub runtime argument")
        prepare_runtime(config)
        for reader in config.readers:
            print(
                "[hub] event=reader_ready "
                f"reader={reader.name} listener={reader.proxy_port} api={reader.api_port}",
                file=sys.stderr,
            )
        return supervise(config)
    except (HubConfigError, ProxyConfigError, OSError, subprocess.SubprocessError) as exc:
        print(f"[hub] ERROR: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
