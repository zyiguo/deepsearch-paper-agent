"""Bounded real-model/real-Redis baseline and singleflight verification.

Only the fixed synthetic input below is sent to the configured embedding model.
The two groups each have four concurrent callers; they use independent random
Redis namespaces and the same model configuration. No automatic retry is made.
Public JSON contains aggregate receipts, shapes and digests, never configuration
paths, URLs, credentials, response bodies, source text or vector coordinates.
The durable cost ledger is retained under the new output directory's private/.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
from threading import Barrier, Event, Lock
import time
from unittest.mock import patch
from uuid import uuid4

from deepsearch_agent.adapters import embedding_runtime
from deepsearch_agent.adapters.embedding_runtime import EmbeddingSettings, RemoteEmbeddingClient
from deepsearch_agent.adapters.governance_config import GovernanceSettings
from deepsearch_agent.adapters.infrastructure import (
    CachedEmbeddingClient, InfrastructureSettings, build_redis,
)
from deepsearch_agent.adapters.persistence.governance import GovernanceStore
from deepsearch_agent.application.governance import Governance
from deepsearch_agent.core.governance import usage_context
from deepsearch_agent.core.models import ModelServiceError


SYNTHETIC_TEXT = "Synthetic cache probe: a small blue cube rests beside a red sphere."
CALLERS = 4
HTTP_REQUEST_LIMIT = 5
GROUP_LIMITS = {"baseline": 4, "protected": 1}
SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    "scripts/verify_cache_real_model.py",
    "src/deepsearch_agent/adapters/infrastructure.py",
    "src/deepsearch_agent/adapters/embedding_runtime.py",
    "src/deepsearch_agent/adapters/governed_calls.py",
    "src/deepsearch_agent/adapters/governance_config.py",
    "src/deepsearch_agent/adapters/persistence/governance.py",
    "src/deepsearch_agent/application/governance.py",
    "src/deepsearch_agent/core/governance.py",
)
CURRENT_GROUP = ContextVar("cache_probe_group", default=None)


class ProbeFailure(RuntimeError):
    """Only fixed codes supplied by this script are safe to publish."""


def require(condition, code):
    if not condition:
        raise ProbeFailure(code)


def safe_error(error):
    result = {"exception_type": type(error).__name__}
    if isinstance(error, ProbeFailure):
        result["error_code"] = str(error)
    elif isinstance(error, ModelServiceError):
        result["error_code"] = "model_response_or_transport_failure"
    else:
        result["error_code"] = "verification_failed"
    return result


def source_hashes():
    return {name: hashlib.sha256((SOURCE_ROOT / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES}


def metric_count(service, name, outcome):
    with service.metrics.lock:
        return service.metrics.counters[("deepsearch_" + name + "_total",
                                         (("outcome", outcome),))]


def vector_digest(vector):
    encoded = json.dumps(vector, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class HttpBudget:
    """A request hook counts attempts before transport sends, not supplier bills."""

    def __init__(self):
        self.lock = Lock()
        self.attempts = Counter()
        self.aborted = Event()

    def before_request(self, request):
        group = CURRENT_GROUP.get()
        with self.lock:
            require(group in GROUP_LIMITS, "http_group_context_missing")
            require(not self.aborted.is_set(), "stopped_after_model_failure")
            require(sum(self.attempts.values()) < HTTP_REQUEST_LIMIT,
                    "http_dispatch_hard_limit_reached")
            require(self.attempts[group] < GROUP_LIMITS[group],
                    "group_http_dispatch_limit_reached")
            self.attempts[group] += 1

    def count(self, group=None):
        with self.lock:
            return self.attempts[group] if group else sum(self.attempts.values())

    def client_class(self):
        # Subclass the actual client and append only a request hook. In particular,
        # do not inject a transport or alter proxy, trust_env, TLS or timeout.
        original = embedding_runtime.httpx.Client
        budget = self

        class AuditedClient(original):
            def __init__(self, *args, **kwargs):
                hooks = {name: list(values) for name, values in
                         (kwargs.get("event_hooks") or {}).items()}
                hooks.setdefault("request", []).append(budget.before_request)
                kwargs["event_hooks"] = hooks
                super().__init__(*args, **kwargs)

        return AuditedClient


class GatedProvider:
    """Hold real delegate calls until the main thread observes overlap."""

    def __init__(self, delegate, budget):
        self.delegate, self.budget = delegate, budget
        self.lock = Lock()
        self.released = Event()
        self.permitted = False
        self.entries = self.provider_batches = self.successful_batches = 0

    def embed(self, texts):
        require(texts == [SYNTHETIC_TEXT], "non_synthetic_or_unbounded_input")
        with self.lock:
            self.entries += 1
        require(self.released.wait(20), "provider_overlap_gate_timeout")
        require(self.permitted, "provider_gate_not_authorized_by_observation")
        require(not self.budget.aborted.is_set(), "stopped_after_model_failure")
        with self.lock:
            self.provider_batches += 1
        try:
            # The production delegate supplies the metered invocation boundary.
            vectors = self.delegate.embed(texts)
        except BaseException:
            self.budget.aborted.set()
            raise
        with self.lock:
            self.successful_batches += 1
        return vectors

    def release(self, *, permitted):
        self.permitted = permitted
        self.released.set()

    def counts(self):
        with self.lock:
            return {"provider_gate_entries": self.entries,
                    "provider_batches": self.provider_batches,
                    "successful_provider_batches": self.successful_batches}


def receipts(store, group):
    # Raw rows and identifiers remain in private/cost.sqlite3. Rate=1 converts
    # reported tokens to internal units; no supplier currency price is assumed.
    with store.connect() as db:
        rows = db.execute("SELECT status,reserved,actual,rate FROM cost_reservations "
                          "WHERE run_id=? ORDER BY created_at,id", (group,)).fetchall()
    known = [row["actual"] for row in rows if row["actual"] is not None]
    known_tokens = [row["actual"] // row["rate"] for row in rows
                    if row["actual"] is not None and row["rate"] > 0]
    return {"reservation_count": len(rows),
            "statuses": dict(Counter(row["status"] for row in rows)),
            "reserved_internal_units": sum(row["reserved"] for row in rows),
            "known_reported_tokens": sum(known_tokens) if known_tokens else None,
            "reported_tokens": sum(known_tokens) if rows and len(known_tokens) == len(rows) else None,
            "known_actual_internal_units": sum(known) if known else None,
            "actual_internal_units": sum(known) if rows and len(known) == len(rows) else None,
            "unknown_cost_entries": sum(row["status"] == "unknown" for row in rows),
            "entries": [{"status": row["status"],
                         "reserved_internal_units": row["reserved"],
                         "reported_tokens": row["actual"] // row["rate"]
                         if row["actual"] is not None and row["rate"] > 0 else None,
                         "actual_internal_units": row["actual"]} for row in rows]}


def clean_namespace(backend):
    namespace = backend.settings.namespace
    require(bool(re.fullmatch(r"realcache_[a-f0-9]{32}", namespace)),
            "cleanup_namespace_invalid")
    prefix = namespace + ":"
    removed = 0
    for key in backend.client.scan_iter(match=prefix + "*", count=50):
        decoded = key.decode() if isinstance(key, bytes) else key
        require(isinstance(decoded, str) and decoded.startswith(prefix),
                "cleanup_key_outside_namespace")
        removed += backend.client.delete(key)
    return removed


def run_group(name, settings, service, store, budget, redis_url):
    protected = name == "protected"
    row = {"case": name, "status": "running", "callers": CALLERS,
           "singleflight_enabled": protected, "ttl_jitter_ratio": .1 if protected else 0,
           "input_sha256": hashlib.sha256(SYNTHETIC_TEXT.encode()).hexdigest()}
    backend = provider = cache = None
    outputs = []
    started = time.monotonic()
    try:
        backend = build_redis(InfrastructureSettings(
            redis_url=redis_url, namespace="realcache_" + uuid4().hex,
            cache_ttl=120, cache_ttl_jitter_ratio=.1 if protected else 0,
            singleflight_enabled=protected, singleflight_wait_seconds=90,
            singleflight_max_keys=16, socket_timeout=1, cooldown=.2))
        require(backend is not None and backend.client.ping(), "real_redis_unavailable")
        provider = GatedProvider(RemoteEmbeddingClient(settings), budget)
        cache = CachedEmbeddingClient(provider, backend, settings.identity,
                                      settings.dimensions, scope="synthetic-cache-probe")
        barrier = Barrier(CALLERS + 1)
        leader_before = metric_count(service, "embedding_singleflight", "leader")
        follower_before = metric_count(service, "embedding_singleflight", "follower")

        def call():
            token = CURRENT_GROUP.set(name)
            try:
                with usage_context(service, user="synthetic-probe-user",
                                   workspace="synthetic-probe-workspace", run_id=name,
                                   operation_id=None):
                    barrier.wait(timeout=15)
                    return cache.embed([SYNTHETIC_TEXT])
            finally:
                CURRENT_GROUP.reset(token)

        with ThreadPoolExecutor(max_workers=CALLERS, thread_name_prefix="cache-probe") as pool:
            futures = [pool.submit(call) for _ in range(CALLERS)]
            try:
                barrier.wait(timeout=15)
                until = time.monotonic() + 12
                while time.monotonic() < until:
                    leaders = metric_count(service, "embedding_singleflight", "leader") - leader_before
                    followers = metric_count(service, "embedding_singleflight", "follower") - follower_before
                    entries = provider.counts()["provider_gate_entries"]
                    if ((protected and entries == 1 and leaders == 1 and followers == 3
                         and cache.inflight_count == 1)
                            or (not protected and entries == 4)):
                        break
                    require(not any(f.done() for f in futures), "caller_finished_before_gate_release")
                    time.sleep(.01)
                else:
                    raise ProbeFailure("required_overlap_not_observed")
                row["overlap_observation"] = {"provider_gate_entries": entries,
                    "singleflight_leaders": leaders, "singleflight_followers": followers,
                    "inflight_keys": cache.inflight_count}
                provider.release(permitted=True)
                errors = []
                for future in futures:
                    try:
                        outputs.append(future.result(timeout=95))
                    except Exception as error:
                        errors.append(error)
                if errors:
                    # Preserve the model error over the deliberately sanitized
                    # follower fan-out error, without ever persisting its text.
                    raise next((e for e in errors if isinstance(e, ModelServiceError)), errors[0])
            finally:
                if not provider.released.is_set():
                    provider.release(permitted=False)
        require(cache.inflight_count == 0, "inflight_registry_not_empty")
        require(all(len(batch) == 1 and cache.valid(batch[0]) for batch in outputs),
                "model_vectors_invalid")
        digests = [vector_digest(batch[0]) for batch in outputs]
        if protected:
            require(len(set(digests)) == 1, "followers_not_aligned_with_leader")
        expected = GROUP_LIMITS[name]
        require(provider.counts()["provider_batches"] == expected,
                "unexpected_provider_batch_count")
        require(budget.count(name) == expected, "unexpected_http_dispatch_attempt_count")
        before_hit = (provider.counts()["provider_batches"], budget.count(name))
        token = CURRENT_GROUP.set(name)
        try:
            with usage_context(service, user="synthetic-probe-user",
                               workspace="synthetic-probe-workspace", run_id=name,
                               operation_id=None):
                hit = cache.embed([SYNTHETIC_TEXT])
        finally:
            CURRENT_GROUP.reset(token)
        require((provider.counts()["provider_batches"], budget.count(name)) == before_hit,
                "cache_hit_dispatched_additional_provider_call")
        require(len(hit) == 1 and cache.valid(hit[0]) and vector_digest(hit[0]) in digests,
                "cache_hit_vector_not_aligned")
        ttl = backend.client.ttl(cache.key(SYNTHETIC_TEXT))
        require(0 < ttl <= 120, "cache_entry_missing_or_invalid_ttl")
        cost = receipts(store, name)
        require(cost["reservation_count"] == expected, "unexpected_metered_reservation_count")
        require(set(cost["statuses"]) <= {"settled", "unknown"}, "unexpected_cost_receipt_state")
        row.update(status="passed", vector_shape=[CALLERS, 1, settings.dimensions],
                   vector_sha256=digests, cached_vector_sha256=vector_digest(hit[0]),
                   cache_hit_without_extra_dispatch=True, cache_ttl_seconds_observed=ttl)
    except (Exception, KeyboardInterrupt) as error:
        budget.aborted.set()
        row.update(status="failed", **safe_error(error))
    finally:
        row["duration_seconds"] = round(time.monotonic() - started, 3)
        row["http_dispatch_attempts"] = budget.count(name)
        try:
            row["cost_receipt"] = receipts(store, name)
        except Exception as error:
            row["cost_receipt"] = None
            row["receipt_read"] = {"status": "failed", **safe_error(error)}
            row["status"] = "failed"
            budget.aborted.set()
        if provider is not None:
            row.update(provider.counts())
        if cache is not None:
            row["inflight_keys_after_completion"] = cache.inflight_count
        if backend is not None:
            try:
                row["own_redis_keys_removed"] = clean_namespace(backend)
            except Exception as error:
                row["cleanup"] = {"status": "failed", **safe_error(error)}
                row["status"] = "failed"
                budget.aborted.set()
            finally:
                backend.close()
    return row, outputs[0][0] if outputs and row["status"] == "passed" else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path,
                        help="New directory only; never reuse an interrupted run")
    parser.add_argument("--model-config", type=Path, default=Path(".env.embedding"))
    parser.add_argument("--redis-url", default=os.environ.get("TEST_REDIS_URL", ""),
                        help="Prefer TEST_REDIS_URL to keep credentials out of shell history")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        print(json.dumps({"status": "failed", "error_code": "output_must_be_new_directory",
                          "exception_type": type(error).__name__}))
        return 2
    report = {"schema_version": 1, "kind": "real_embedding_real_redis_paired_cache_probe",
              "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "running", "cases": [], "http_dispatch_hard_limit": HTTP_REQUEST_LIMIT,
              "automatic_network_retries": 0, "input_origin": "fixed_synthetic_literal",
              "receipt_storage": "private/cost.sqlite3", "cost_currency": None,
              "cost_unit_definition": "reported tokens multiplied by internal embedding rate 1",
              "http_dispatch_attempt_definition": "request hook before transport send; does not prove server receipt or billing",
              "provider_batch_definition": "real metered RemoteEmbeddingClient.embed invocations; one input per batch"}
    budget = HttpBudget()
    service = None
    try:
        require(bool(args.redis_url), "redis_url_required")
        frozen = source_hashes()
        report["source_sha256"] = frozen
        settings = EmbeddingSettings.load(args.model_config)
        require(settings.backend == "openai_compatible", "real_embedding_backend_required")
        require(bool(re.fullmatch(r"[A-Za-z0-9_./:@+-]{1,120}", settings.model)),
                "model_name_not_safe_for_public_report")
        report["model"] = {"name": settings.model, "dimensions": settings.dimensions,
                           "identity_hash": settings.identity}
        private = args.output / "private"
        private.mkdir(mode=0o700)
        store = GovernanceStore(private / "cost.sqlite3")
        service = Governance(store, GovernanceSettings(
            user_daily_units=20000, workspace_daily_units=20000,
            global_provider=4, workspace_provider=4, user_provider=4,
            embedding_unit_rate=1))
        report["governance"] = {"user_daily_units": 20000, "workspace_daily_units": 20000,
                                "global_provider": 4, "workspace_provider": 4, "user_provider": 4}
        references = []
        # This standalone process patches only the constructor used by the actual
        # production embedding adapter, restoring it on both success and failure.
        with patch.object(embedding_runtime.httpx, "Client", budget.client_class()):
            for name in ("baseline", "protected"):
                require(source_hashes() == frozen, "source_changed_during_probe")
                row, reference = run_group(name, settings, service, store, budget, args.redis_url)
                report["cases"].append(row)
                if row["status"] != "passed":
                    break  # Never retry a failed paid case or start the next group.
                references.append(reference)
        if len(references) == 2:
            aligned = all(math.isclose(a, b, rel_tol=1e-5, abs_tol=1e-7)
                          for a, b in zip(*references))
            report["paired_vector_alignment"] = {"same_dimensions": len(references[0]) == len(references[1]),
                "within_tolerance": aligned, "relative_tolerance": 1e-5, "absolute_tolerance": 1e-7,
                "vector_sha256": [vector_digest(v) for v in references]}
            require(aligned, "paired_vectors_not_aligned")
        require(source_hashes() == frozen, "source_changed_during_probe")
        require(len(report["cases"]) == 2 and all(r["status"] == "passed" for r in report["cases"]),
                "paired_probe_incomplete")
        require(budget.count() == HTTP_REQUEST_LIMIT, "unexpected_paired_http_dispatch_count")
        report["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="failed", **safe_error(error))
    finally:
        if service is not None:
            service.close()
        report["http_dispatch_attempts"] = budget.count()
        report["summary"] = {"passed": sum(r["status"] == "passed" for r in report["cases"]),
                             "failed": sum(r["status"] == "failed" for r in report["cases"]),
                             "runner_failed": report["status"] == "failed",
                             "paired_verification_complete": report["status"] == "passed"}
        (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n",
                                                  encoding="utf-8")
    print(json.dumps({"report": "report.json", "status": report["status"],
                      "http_dispatch_attempts": report["http_dispatch_attempts"], **report["summary"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
