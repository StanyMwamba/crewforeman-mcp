"""Per-model usage aggregation for the 3.12.0 run record (DESIGN 6.1).

The module is deliberately pure: callers pass execution dictionaries and get a
JSON-ready aggregate back.  No session logs are read here and no state is
written.  Cost is only claimed when every contributing model is priced; an
unpriced model never receives a zero, a proxy price or a token-share split.

3.15.0 (MiMo) pricing: executions are priced from their event tokens
(uncached input, cache read/hit, output, reasoning) against the ``mimo_prices``
config table (a dict, or a JSON string as delivered from ``DSH_MIMO_PRICES``)
or the builtin ``api.MIMO_PRICES`` table.  A configured row overrides the
builtin per model, and ``cache_hit`` falls back to the input rate when the row
omits it.  Reasoning tokens are kept in their own bucket and, when no explicit
reasoning rate is configured, counted into the output cost (billed at the
output rate) because the MiMo event stream reports them separately from
output.  An unknown model stays unpriced (``cost_known`` false, cost ``None``)
— never zero.  Cost provenance decides whose number wins: ``cost_source``
``vendor`` (the CLI's own ``part.cost`` sum) and ``price_table`` (priced
upstream by the worker adapter) are used as-is and never re-priced here, while
the legacy ``mimo_events`` rows are priced from their event tokens only (a
supplied estimate is never a proxy for a missing price row).  The finished
aggregate carries a ``by_vendor`` rollup (vendor → tokens/cost/models) for the
per-vendor split surfaced in run records, job status and the spend/waste
reports.
"""

import json
import os
import re

try:  # pragma: no cover - api is present in supported builds
    from . import api
except Exception:  # pragma: no cover
    api = None

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")
_PROVIDER_HINTS = (
    ("deepseek", "deepseek-official"),
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("gemini", "google"),
    ("qwen", "alibaba"),
    ("mimo", "mimo"),
    ("xiaomi", "mimo"),
)
# Vendor display names for the per-vendor rollup.  Aliases are matched
# case-insensitively as substrings of the ``provider/model`` label.
_VENDOR_ALIASES = (
    ("Xiaomi (MiMo)", ("mimo", "xiaomi")),
    ("Kimi (Moonshot)", ("moonshot", "kimi")),
    ("DeepSeek", ("deepseek",)),
    ("Anthropic", ("anthropic", "claude", "sonnet", "opus")),
    ("OpenAI", ("openai", "gpt")),
    ("Google", ("google", "gemini")),
)
_SOURCE_RANK = {"candidate": 0, "slice": 1, "quality": 2, "reviewer": 3,
                "self": 4}


def _cfg(cfg, name, default):
    try:
        getter = getattr(cfg, "get", None)
        if callable(getter):
            value = getter(name, default)
        else:
            value = getattr(cfg, name, default)
        return default if value is None else value
    except Exception:
        return default


def _int(value, default=0):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number


def _float(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _num(source, names, default=0):
    if not isinstance(source, dict):
        return default
    for name in names:
        if source.get(name) is not None:
            return _float(source.get(name), default)
    return default


def _tokens(source, fallback=None):
    """Read one token bucket, accepting the SDK/session aliases.

    Returns ``(input, cache_read, output, reasoning, total, cache_write)``.
    Reasoning tokens are tracked separately (they are billed at the output
    rate unless a price row says otherwise) and are included in the derived
    total, as are cache-write tokens: the MiMo event stream reports each
    bucket disjointly and ``total`` is their sum.
    """
    if not isinstance(source, dict):
        source = {}
    fallback = fallback if isinstance(fallback, dict) else {}
    tokens_in = _num(source, ("tokens_in", "input_tokens", "input",
                              "prompt_tokens", "uncached_tokens"),
                      _num(fallback, ("tokens_in", "input_tokens", "input",
                                      "prompt_tokens", "uncached_tokens")))
    cache = _num(source, ("cache_read_tokens", "cache_read", "cache_hit_tokens",
                          "prompt_cache_hit_tokens"),
                 _num(fallback, ("cache_read_tokens", "cache_read",
                                 "cache_hit_tokens", "prompt_cache_hit_tokens")))
    cache_write = _num(source, ("cache_write_tokens", "cache_write"),
                       _num(fallback, ("cache_write_tokens", "cache_write")))
    out = _num(source, ("tokens_out", "output_tokens", "output",
                        "completion_tokens"),
               _num(fallback, ("tokens_out", "output_tokens", "output",
                               "completion_tokens")))
    reasoning = _num(source, ("reasoning_tokens", "reasoning"),
                     _num(fallback, ("reasoning_tokens", "reasoning")))
    total = _num(source, ("total_tokens", "total"),
                 _num(fallback, ("total_tokens", "total"),
                      tokens_in + cache + cache_write + out + reasoning))
    if total <= 0:
        total = tokens_in + cache + cache_write + out + reasoning
    return (int(round(tokens_in)), int(round(cache)), int(round(out)),
            int(round(reasoning)), int(round(total)),
            int(round(cache_write)))


def _norm_prices(entry, cache_default_to_input=False):
    """Normalise one price row to miss/hit/out (USD per 1M tokens).

    Key spellings are interchangeable: ``input``/``miss``, ``cache_read``/
    ``hit``/``cache_hit``, ``output``/``out``.  MiMo rows may omit the cache
    rate: with ``cache_default_to_input`` the cache-hit rate falls back to the
    input rate.  An optional ``reasoning``/``reasoning_out`` rate prices
    reasoning tokens; when absent they are billed at the output rate (see the
    module docstring).
    """
    if not isinstance(entry, dict):
        return None
    miss = entry.get("input", entry.get("miss"))
    hit = entry.get("cache_read", entry.get("hit", entry.get("cache_hit")))
    out = entry.get("output", entry.get("out"))
    reasoning = entry.get("reasoning", entry.get("reasoning_out"))
    if miss is None or out is None:
        return None
    if hit is None:
        if not cache_default_to_input:
            return None
        hit = miss
    try:
        prices = {"miss": float(miss), "hit": float(hit), "out": float(out)}
        if reasoning is not None:
            prices["reasoning"] = float(reasoning)
    except (TypeError, ValueError):
        return None
    return prices


def _price_table(value):
    """Return a price table dict from a dict or a JSON string (DSH_MIMO_PRICES)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _price_key_match(name, key, prefix=False):
    """Match full ``provider/model`` keys and bare model ids."""
    name = str(name or "")
    key = str(key or "")
    if not name or not key:
        return False
    name_bare = name.split("/", 1)[1] if "/" in name else name
    key_bare = key.split("/", 1)[1] if "/" in key else key
    if prefix:
        return (name.startswith(key)
                or (bool(name_bare) and name_bare.startswith(key_bare)))
    return (name == key
            or (bool(name_bare) and name_bare == key_bare))


_MIMO_VENDOR = "Xiaomi (MiMo)"


def _vendor_for(label):
    """Map a ``provider/model`` label to its canonical vendor name.

    Aliases are matched case-insensitively as substrings; anything that
    matches none of them is attributed to ``"unknown"``.
    """
    text = str(label or "").lower()
    if not text.strip():
        return "unknown"
    for vendor, aliases in _VENDOR_ALIASES:
        for alias in aliases:
            if alias in text:
                return vendor
    return "unknown"


def _lookup_prices(table, *names, prefix=False, cache_default_to_input=False):
    if not isinstance(table, dict):
        return None
    for name in names:
        for key, entry in table.items():
            if _price_key_match(name, key, prefix=prefix):
                found = _norm_prices(
                    entry, cache_default_to_input=cache_default_to_input)
                if found is not None:
                    return found
    return None


def _prices_for(model, bare, table, fallback=None, mimo_table=None):
    """Resolve price rows: mimo config, then the role tables, then builtins.

    Exact keys win over prefixes and a partial/invalid row is skipped so the
    lookup falls through instead of fabricating a price.  The dedicated
    ``mimo_table`` (config ``mimo_prices``, dict or JSON string) and the
    builtin ``api.MIMO_PRICES`` rows accept the ``cache_hit`` spelling and let
    a missing cache rate fall back to the input rate.
    """
    for prefix in (False, True):
        for candidate in (mimo_table, table, fallback):
            found = _lookup_prices(
                candidate, model, bare, prefix=prefix,
                cache_default_to_input=(mimo_table is not None
                                        and candidate is mimo_table))
            if found is not None:
                return found
    builtin_mimo = getattr(api, "MIMO_PRICES", {}) if api is not None else {}
    for prefix in (False, True):
        found = _lookup_prices(builtin_mimo, model, bare, prefix=prefix,
                               cache_default_to_input=True)
        if found is not None:
            return found
    builtin = getattr(api, "HARNESS_PRICES", {}) if api is not None else {}
    for prefix in (False, True):
        found = _lookup_prices(builtin, bare, model, prefix=prefix)
        if found is not None:
            return found
    return None


def _provider_for(model, execution=None, role_map=None):
    execution = execution or {}
    for key in ("provider", "vendor"):
        value = execution.get(key)
        if value:
            return str(value).strip().lower()
    bare = str(model or "").split("/", 1)[0]
    if isinstance(role_map, dict):
        hint = role_map.get(bare)
        if isinstance(hint, str) and hint and "/" not in hint and len(hint) < 40:
            return hint.strip().lower()
    for prefix, provider in _PROVIDER_HINTS:
        if bare.startswith(prefix):
            return provider
    return "unknown"


def label_for(model, execution=None, role_map=None):
    """Return a stable ``provider/model`` label (or "" for junk)."""
    text = str(model or "").strip()
    if not text:
        return ""
    if "/" in text:
        label = text
    else:
        label = "%s/%s" % (_provider_for(text, execution, role_map), text)
    if not _MODEL_RE.match(label):
        return ""
    return label


def _model_names(execution):
    raw = execution.get("models")
    if raw is None and isinstance(execution.get("usage"), dict):
        raw = execution["usage"].get("models")
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, dict):
        return [str(key) for key in raw if str(key).strip()]
    if isinstance(raw, (list, tuple)):
        names = []
        for item in raw:
            if isinstance(item, dict):
                value = item.get("model") or item.get("name")
            else:
                value = item
            if value:
                names.append(str(value))
        return names
    single = execution.get("model")
    return [str(single)] if single else []


def _entry_for(execution, name):
    raw = execution.get("models")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if str(key) == name and isinstance(value, dict):
                return value
    usage = execution.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("models"), dict):
        for key, value in usage["models"].items():
            if str(key) == name and isinstance(value, dict):
                return value
    return {}


def _role(execution):
    value = execution.get("role") or execution.get("kind") or "builder"
    return str(value).strip() or "builder"


def _is_reviewer(role):
    role = str(role or "").lower()
    return (role == "reviewer" or role.startswith("reviewer:")
            or role in ("revision", "quality", "judge")
            or role.startswith("revision:"))


def _successful(execution):
    if execution.get("success") is False:
        return False
    status = str(execution.get("status") or "").lower()
    return status not in ("error", "failed", "no_balance", "timeout", "stalled")


def _cost_source(execution):
    value = execution.get("cost_source")
    if value in ("session_logs", "sdk_usage", "mimo_events", "vendor",
                 "price_table", "unknown"):
        return value
    return "unknown"


class ModelUsage:
    """Accumulate executions, then materialise a bounded aggregate on finish.

    ``mimo_prices`` overrides the builtin ``api.MIMO_PRICES`` per model; it is
    a dict or a JSON string (as delivered from env ``DSH_MIMO_PRICES``).
    """

    def __init__(self, max_models=8, prices=None, reviewer_prices=None,
                 mimo_prices=None):
        self.max_models = max(0, _int(max_models, 8))
        self.prices = prices if isinstance(prices, dict) else {}
        self.reviewer_prices = (reviewer_prices
                                if isinstance(reviewer_prices, dict) else {})
        self.mimo_prices = _price_table(mimo_prices)
        self._rows = {}
        self._scores = []
        self._unattributed = 0
        self._other = {"tokens_in": 0, "cache_read_tokens": 0,
                       "cache_write_tokens": 0,
                       "tokens_out": 0, "reasoning_tokens": 0,
                       "total_tokens": 0,
                       "cost": 0.0, "cost_known": True}
        self._unpriced = set()
        self._wins = {}
        self._losses = {}

    def _row(self, label):
        row = self._rows.get(label)
        if row is None:
            provider, _sep, model = label.partition("/")
            vendor = provider
            if _vendor_for(label) == _MIMO_VENDOR:
                # MiMo executions attribute provider 'mimo' and the canonical
                # 'Xiaomi (MiMo)' vendor, whichever spelling the model id used.
                provider = "mimo"
                vendor = _MIMO_VENDOR
            row = {
                "provider": provider, "model": model or label,
                "vendor": vendor, "roles": [], "executions": 0,
                "tokens_in": 0, "cache_read_tokens": 0,
                "cache_write_tokens": 0, "tokens_out": 0,
                "reasoning_tokens": 0, "total_tokens": 0,
                "cost": 0.0, "cost_known": True,
                "cost_sources": [], "scores": [],
            }
            self._rows[label] = row
        return row

    def add_score(self, model, score, source="candidate", **kwargs):
        """Attach one aggregate score; candidate scores must come from here."""
        if score is None:
            self._unattributed += 1
            return
        label = label_for(model)
        if not label:
            self._unattributed += 1
            return
        try:
            value = float(score)
        except (TypeError, ValueError):
            self._unattributed += 1
            return
        source = str(source or "candidate").strip().lower()
        if source not in _SOURCE_RANK:
            source = "self"
        self._scores.append({"label": label, "score": value,
                             "source": source,
                             "win": bool(kwargs.get("win")),
                             "loss": bool(kwargs.get("loss"))})

    def _add_other(self, tokens, cost, cost_known):
        self._other["tokens_in"] += tokens[0]
        self._other["cache_read_tokens"] += tokens[1]
        self._other["cache_write_tokens"] += tokens[5]
        self._other["tokens_out"] += tokens[2]
        self._other["reasoning_tokens"] += tokens[3]
        self._other["total_tokens"] += tokens[4]
        if not cost_known or cost is None:
            self._other["cost_known"] = False
        else:
            self._other["cost"] += _float(cost)
        return self._other

    def add_execution(self, execution):
        """Add one execution mapping; unknown fields are ignored."""
        if not isinstance(execution, dict):
            return self
        names = _model_names(execution)
        labels = []
        names_by_label = {}
        for name in names:
            label = label_for(name, execution)
            if label and label not in labels:
                labels.append(label)
                names_by_label[label] = name
        role = _role(execution)
        reviewer = _is_reviewer(role)
        table = self.reviewer_prices if reviewer else self.prices
        tokens = _tokens(execution)
        score = execution.get("score")
        if score is None:
            score = execution.get("quality_score")
        if score is not None and not _successful(execution):
            score = None
        if score is not None and len(labels) != 1:
            self._unattributed += 1
            score = None
        if not labels:
            self._add_other(tokens, execution.get("estimated_cost_usd"),
                            bool(execution.get("cost_known")) and
                            execution.get("estimated_cost_usd") is not None)
            return self

        # A multi-model dict can carry a token bucket per model.  A bare list
        # cannot be split safely, so its tokens/cost are counted but kept whole.
        models_dict = isinstance(execution.get("models"), dict)
        per_model = models_dict and len(labels) > 1
        if len(labels) > 1 and not per_model:
            self._add_other(tokens, execution.get("estimated_cost_usd"),
                            bool(execution.get("cost_known")) and
                            execution.get("estimated_cost_usd") is not None)
        for label in labels:
            entry = (_entry_for(execution, names_by_label.get(label, label))
                     if models_dict else {})
            if entry:
                model_tokens = _tokens(entry, execution)
            elif len(labels) == 1:
                model_tokens = tokens
            else:
                model_tokens = (0, 0, 0, 0, 0, 0)
            row = self._row(label)
            row["executions"] += 1
            row["tokens_in"] += model_tokens[0]
            row["cache_read_tokens"] += model_tokens[1]
            row["cache_write_tokens"] += model_tokens[5]
            row["tokens_out"] += model_tokens[2]
            row["reasoning_tokens"] += model_tokens[3]
            row["total_tokens"] += model_tokens[4]
            if role not in row["roles"]:
                row["roles"].append(role)
            source = _cost_source(execution)
            if source not in row["cost_sources"]:
                row["cost_sources"].append(source)
            bare = label.split("/", 1)[1] if "/" in label else label
            resolved = _prices_for(
                label, bare, table, self.prices if reviewer else None,
                mimo_table=self.mimo_prices)
            supplied = (entry.get("estimated_cost_usd")
                        if entry.get("estimated_cost_usd") is not None
                        else execution.get("estimated_cost_usd"))
            supplied_known = bool(entry.get("cost_known", execution.get("cost_known")))
            if source == "mimo_events":
                # Legacy event-derived rows are priced from their event
                # tokens only: a supplied estimate is never a proxy for a
                # missing price row.
                supplied = None
                supplied_known = False
            if (source in ("vendor", "price_table")
                    and supplied_known and supplied is not None):
                # The CLI (vendor) or the adapter's own price pass
                # (price_table) already computed this cost -- use it as-is,
                # never re-priced from the token buckets.
                row["cost"] += _float(supplied)
            elif resolved is not None:
                # Reasoning tokens bill at the output rate unless the price
                # row carries an explicit reasoning rate.
                reasoning_rate = resolved.get("reasoning", resolved["out"])
                cost = (model_tokens[0] * resolved["miss"]
                        + model_tokens[1] * resolved["hit"]
                        + model_tokens[2] * resolved["out"]
                        + model_tokens[3] * reasoning_rate) / 1000000.0
                if model_tokens[4] == 0 and supplied_known and supplied is not None:
                    cost = _float(supplied)
                row["cost"] += cost
            elif supplied_known and supplied is not None:
                row["cost"] += _float(supplied)
            else:
                row["cost_known"] = False
                self._unpriced.add(label)
            if score is not None and reviewer:
                self.add_score(label, score, source="reviewer")
            elif (score is not None and not reviewer
                  and not str(role).lower().startswith("candidate")):
                self.add_score(label, score, source="self")
            if execution.get("winner") or execution.get("win"):
                self._wins[label] = self._wins.get(label, 0) + 1
            if execution.get("loser") or execution.get("loss"):
                self._losses[label] = self._losses.get(label, 0) + 1
        return self

    add = add_execution

    def _score_summary(self, label):
        items = [item for item in self._scores if item["label"] == label]
        if not items:
            return None, 0, 0
        vals = [item["score"] for item in items]
        source = min((item["source"] for item in items),
                     key=lambda name: _SOURCE_RANK.get(name, 9))
        wins = sum(1 for item in items if item["win"]) + self._wins.get(label, 0)
        losses = (sum(1 for item in items if item["loss"])
                  + self._losses.get(label, 0))
        return ({"count": len(vals), "mean": round(sum(vals) / len(vals), 2),
                 "last": round(vals[-1], 2), "source": source},
                wins, losses)

    def _by_vendor_rollup(self):
        """Aggregate every tracked row (and unattributed tokens) per vendor.

        Buckets use the canonical vendor names (``Xiaomi (MiMo)``,
        ``DeepSeek``, ``Kimi (Moonshot)``, ``unknown``, ...) and carry
        ``cost``/``cost_known`` where cost is claimed only when every
        contributing model is priced.
        """
        buckets = {}

        def bucket(vendor):
            entry = buckets.get(vendor)
            if entry is None:
                entry = {
                    "input_tokens": 0, "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "output_tokens": 0, "reasoning_tokens": 0,
                    "total_tokens": 0,
                    "cost": 0.0, "cost_known": True,
                    "models": [],
                }
                buckets[vendor] = entry
            return entry

        for label, row in self._rows.items():
            entry = bucket(_vendor_for(label))
            entry["input_tokens"] += row["tokens_in"]
            entry["cache_read_tokens"] += row["cache_read_tokens"]
            entry["cache_write_tokens"] += row["cache_write_tokens"]
            entry["output_tokens"] += row["tokens_out"]
            entry["reasoning_tokens"] += row["reasoning_tokens"]
            entry["total_tokens"] += row["total_tokens"]
            entry["models"].append(label)
            if row["cost_known"]:
                entry["cost"] += row["cost"]
            else:
                entry["cost_known"] = False
        other = self._other
        if (other["tokens_in"] or other["cache_read_tokens"]
                or other["cache_write_tokens"] or other["tokens_out"]
                or other["reasoning_tokens"]
                or other["total_tokens"]):
            entry = bucket("unknown")
            entry["input_tokens"] += other["tokens_in"]
            entry["cache_read_tokens"] += other["cache_read_tokens"]
            entry["cache_write_tokens"] += other["cache_write_tokens"]
            entry["output_tokens"] += other["tokens_out"]
            entry["reasoning_tokens"] += other["reasoning_tokens"]
            entry["total_tokens"] += other["total_tokens"]
            if other["cost_known"]:
                entry["cost"] += other["cost"]
            else:
                entry["cost_known"] = False
        rollup = {}
        for vendor in sorted(buckets):
            entry = buckets[vendor]
            entry["models"] = sorted(entry["models"])
            entry["cost"] = (round(entry["cost"], 6)
                             if entry["cost_known"] else None)
            rollup[vendor] = entry
        return rollup

    def finish(self):
        """Return the version-1 aggregate.  Calling it twice is identical."""
        raw = {}
        for label, row in self._rows.items():
            scores, wins, losses = self._score_summary(label)
            item = {key: row[key] for key in (
                "provider", "model", "vendor", "roles", "executions",
                "tokens_in", "cache_read_tokens", "cache_write_tokens",
                "tokens_out", "reasoning_tokens", "total_tokens")}
            item["estimated_cost_usd"] = (
                round(row["cost"], 6) if row["cost_known"] else None)
            item["cost_known"] = bool(row["cost_known"])
            item["cost_source"] = (row["cost_sources"][0]
                                   if row["cost_sources"] else "unknown")
            item["scores"] = scores
            item["wins"] = wins
            item["losses"] = losses
            raw[label] = item
        ordered = sorted(raw, key=lambda name: (-raw[name]["total_tokens"],
                                                name))
        keep = ordered[:self.max_models]
        folded = ordered[self.max_models:]
        models = {label: raw[label] for label in keep}
        other = {"models_folded": len(folded),
                 "total_tokens": self._other["total_tokens"],
                 "estimated_cost_usd": (round(self._other["cost"], 6)
                                        if self._other["cost_known"] else None),
                 "cost_known": bool(self._other["cost_known"])}
        known_total = 0.0
        all_known = bool(self._other["cost_known"])
        known_total += self._other["cost"] if self._other["cost_known"] else 0.0
        for label in folded:
            other["total_tokens"] += raw[label]["total_tokens"]
            if raw[label]["cost_known"]:
                other["estimated_cost_usd"] = (
                    (other["estimated_cost_usd"] or 0.0)
                    + raw[label]["estimated_cost_usd"])
            else:
                other["estimated_cost_usd"] = None
                other["cost_known"] = False
        if not other["models_folded"] and not self._other["total_tokens"]:
            other["estimated_cost_usd"] = (0.0 if other["cost_known"] else None)
        for label, item in raw.items():
            if item["cost_known"]:
                known_total += item["estimated_cost_usd"]
            else:
                all_known = False
        if not raw and not self._other["total_tokens"] and self._other["cost_known"]:
            all_known = True
        return {
            "version": 1,
            "max_models": self.max_models,
            "models": models,
            "by_vendor": self._by_vendor_rollup(),
            "__other__": other,
            "unpriced_models": sorted(self._unpriced),
            "unattributed_scores": self._unattributed,
            "totals": {
                "estimated_cost_usd": (round(known_total, 6)
                                       if all_known else None),
                "cost_known": bool(all_known),
            },
        }


def by_vendor_of(model_usage_dict):
    """Return the ``by_vendor`` rollup from a model_usage aggregate (or {})."""
    if isinstance(model_usage_dict, dict):
        rollup = model_usage_dict.get("by_vendor")
        if isinstance(rollup, dict):
            return rollup
    return {}


def build_model_usage(executions, cfg=None, role_map=None):
    """Build a :class:`ModelUsage` aggregate from execution dicts."""
    max_models = _int(_cfg(cfg, "model_usage_max_models", 8), 8)
    prices = _cfg(cfg, "model_prices", {})
    reviewer_prices = _cfg(cfg, "reviewer_prices", {})
    usage = ModelUsage(max_models=max_models,
                       prices=prices if isinstance(prices, dict) else {},
                       reviewer_prices=(reviewer_prices
                                        if isinstance(reviewer_prices, dict)
                                        else {}),
                       mimo_prices=_cfg(cfg, "mimo_prices", {}))
    if isinstance(executions, dict):
        executions = executions.get("executions") or []
    for execution in executions or []:
        if isinstance(execution, dict) and role_map and not _model_names(execution):
            mapped = role_map.get(execution.get("role"))
            if isinstance(mapped, (list, tuple)):
                execution = dict(execution, models=list(mapped))
            elif isinstance(mapped, str) and mapped.strip():
                execution = dict(execution, models=[mapped])
        usage.add_execution(execution)
    return usage.finish()
