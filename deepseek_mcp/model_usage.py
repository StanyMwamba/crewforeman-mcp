"""Per-model usage aggregation for the 3.12.0 run record (DESIGN 6.1).

The module is deliberately pure: callers pass execution dictionaries and get a
JSON-ready aggregate back.  No session logs are read here and no state is
written.  Cost is only claimed when every contributing model is priced; an
unpriced model never receives a zero, a proxy price or a token-share split.
"""

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
    """Read one token bucket, accepting the SDK/session aliases."""
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
    out = _num(source, ("tokens_out", "output_tokens", "output",
                        "completion_tokens"),
               _num(fallback, ("tokens_out", "output_tokens", "output",
                               "completion_tokens")))
    total = _num(source, ("total_tokens", "total"),
                 _num(fallback, ("total_tokens", "total"),
                      tokens_in + cache + out))
    if total <= 0:
        total = tokens_in + cache + out
    return int(round(tokens_in)), int(round(cache)), int(round(out)), int(round(total))


def _norm_prices(entry):
    if not isinstance(entry, dict):
        return None
    miss = entry.get("input", entry.get("miss"))
    hit = entry.get("cache_read", entry.get("hit"))
    out = entry.get("output", entry.get("out"))
    if miss is None or hit is None or out is None:
        return None
    try:
        return {"miss": float(miss), "hit": float(hit), "out": float(out)}
    except (TypeError, ValueError):
        return None


def _lookup_prices(table, *names):
    if not isinstance(table, dict):
        return None
    for name in names:
        for key, entry in table.items():
            if str(name) == str(key):
                found = _norm_prices(entry)
                if found is not None:
                    return found
    for name in names:
        if not name:
            continue
        for key, entry in table.items():
            if str(name).startswith(str(key)):
                found = _norm_prices(entry)
                if found is not None:
                    return found
    return None


def _prices_for(model, bare, table):
    found = _lookup_prices(table, model, bare)
    if found is not None:
        return found
    builtin = getattr(api, "HARNESS_PRICES", {}) if api is not None else {}
    return _lookup_prices(builtin, bare, model)


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
    if value in ("session_logs", "sdk_usage", "unknown"):
        return value
    return "unknown"


class ModelUsage:
    """Accumulate executions, then materialise a bounded aggregate on finish."""

    def __init__(self, max_models=8, prices=None, reviewer_prices=None):
        self.max_models = max(0, _int(max_models, 8))
        self.prices = prices if isinstance(prices, dict) else {}
        self.reviewer_prices = (reviewer_prices
                                if isinstance(reviewer_prices, dict) else {})
        self._rows = {}
        self._scores = []
        self._unattributed = 0
        self._other = {"tokens_in": 0, "cache_read_tokens": 0,
                       "tokens_out": 0, "total_tokens": 0,
                       "cost": 0.0, "cost_known": True}
        self._unpriced = set()
        self._wins = {}
        self._losses = {}

    def _row(self, label):
        row = self._rows.get(label)
        if row is None:
            provider, _sep, model = label.partition("/")
            row = {
                "provider": provider, "model": model or label,
                "vendor": provider, "roles": [], "executions": 0,
                "tokens_in": 0, "cache_read_tokens": 0, "tokens_out": 0,
                "total_tokens": 0, "cost": 0.0, "cost_known": True,
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
        self._other["tokens_out"] += tokens[2]
        self._other["total_tokens"] += tokens[3]
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
                model_tokens = (0, 0, 0, 0)
            row = self._row(label)
            row["executions"] += 1
            row["tokens_in"] += model_tokens[0]
            row["cache_read_tokens"] += model_tokens[1]
            row["tokens_out"] += model_tokens[2]
            row["total_tokens"] += model_tokens[3]
            if role not in row["roles"]:
                row["roles"].append(role)
            source = _cost_source(execution)
            if source not in row["cost_sources"]:
                row["cost_sources"].append(source)
            bare = label.split("/", 1)[1] if "/" in label else label
            resolved = _prices_for(label, bare, table)
            supplied = (entry.get("estimated_cost_usd")
                        if entry.get("estimated_cost_usd") is not None
                        else execution.get("estimated_cost_usd"))
            supplied_known = bool(entry.get("cost_known", execution.get("cost_known")))
            if resolved is not None:
                cost = (model_tokens[0] * resolved["miss"]
                        + model_tokens[1] * resolved["hit"]
                        + model_tokens[2] * resolved["out"]) / 1000000.0
                if model_tokens[3] == 0 and supplied_known and supplied is not None:
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

    def finish(self):
        """Return the version-1 aggregate.  Calling it twice is identical."""
        raw = {}
        for label, row in self._rows.items():
            scores, wins, losses = self._score_summary(label)
            item = {key: row[key] for key in (
                "provider", "model", "vendor", "roles", "executions",
                "tokens_in", "cache_read_tokens", "tokens_out",
                "total_tokens")}
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
            "__other__": other,
            "unpriced_models": sorted(self._unpriced),
            "unattributed_scores": self._unattributed,
            "totals": {
                "estimated_cost_usd": (round(known_total, 6)
                                       if all_known else None),
                "cost_known": bool(all_known),
            },
        }


def build_model_usage(executions, cfg=None, role_map=None):
    """Build a :class:`ModelUsage` aggregate from execution dicts."""
    max_models = _int(_cfg(cfg, "model_usage_max_models", 8), 8)
    prices = _cfg(cfg, "model_prices", {})
    reviewer_prices = _cfg(cfg, "reviewer_prices", {})
    usage = ModelUsage(max_models=max_models,
                       prices=prices if isinstance(prices, dict) else {},
                       reviewer_prices=(reviewer_prices
                                        if isinstance(reviewer_prices, dict)
                                        else {}))
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
