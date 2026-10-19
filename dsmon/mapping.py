"""Which published directories belong to which dataset, for every dataset and not only the configured ones.

[content.paths] in config.toml always wins. Every other directory under `alternative/` is matched from the
inventory: its vendor folder against each dataset's job repository (`Lean.DataSource.<Repo>`), vendor and name, then
its dataset folder against the dataset names when the vendor folder holds several datasets. A directory that stays
ambiguous is left unmapped: it is still judged, and the report lists it under its vendor instead of guessing.
"""
import re

_STOP = {"data", "price", "the", "and", "of", "on", "us", "for", "crypto", "universe", "company", "corporate", "model"}


def _flat(text):
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _tokens(text):
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w[:-1] if w.endswith("s") and len(w) > 4 else w for w in words if len(w) >= 4 and w not in _STOP}


def _keys(dataset, inventory):
    repos = {_flat(job.repo.split(".")[-1]) for job in (inventory.jobs.get(j) for j in dataset.job_ids)
             if job is not None and job.repo}
    vendor = _tokens(dataset.vendor) | {_flat(dataset.vendor)}
    return repos, vendor | _tokens(dataset.name)


def _vendor_candidates(segment, inventory):
    found = []
    for dataset in inventory.datasets.values():
        if not dataset.job_ids:
            continue
        repos, words = _keys(dataset, inventory)
        if segment in words or segment in repos or (len(segment) >= 4 and any(segment in r for r in repos)):
            found.append(dataset)
    return found


def _narrow(folder, candidates):
    folder = _flat(folder)
    scores = {}
    for dataset in candidates:
        name = _flat(dataset.name)
        score = sum(1 for t in _tokens(dataset.name) if t in folder)
        if len(folder) >= 6 and (folder in name or name in folder):
            score += 2
        scores[dataset.id] = score
    best = max(scores.values(), default=0)
    return [d for d in candidates if best and scores[d.id] == best]


def auto_paths(paths, inventory, configured=None):
    """{dataset_id: [prefix, ...]} for `paths` (published directories), configured prefixes first."""
    mapping = {int(k): list(v) for k, v in (configured or {}).items()}
    covered = [p for prefixes in mapping.values() for p in prefixes]
    cache = {}
    for path in sorted(paths):
        parts = path.split("/")
        if parts[0] != "alternative" or len(parts) < 2 or any(path.startswith(p) for p in covered):
            continue
        prefix = "/".join(parts[:3])
        if prefix in cache:
            continue
        candidates = _vendor_candidates(_flat(parts[1]), inventory)
        if len(parts) >= 3 and len(candidates) > 1:
            candidates = _narrow(parts[2], candidates)
        cache[prefix] = [d.id for d in candidates] if len(candidates) == 1 else []
    for prefix, ids in cache.items():
        for dataset_id in ids:
            mapping.setdefault(dataset_id, []).append(prefix)
    return mapping
