"""Frozen native stacks, linear rebase candidates and atomic branch publication."""

import hashlib
from pathlib import Path

from loop.policy import (SHA, attributed_owner, commit_author, eligible, exact,
                         require, safe_ref, unchanged)


def layer_request(request, layer):
    return dict({key: value for key, value in request.items()
                 if key not in {"stack", "inputs", "pr_author_id"}}, **layer,
                stack_number=request["stack"]["number"])


def members(detail):
    result = []
    for pr in detail["pull_requests"]:
        require(type(pr["number"]) is int and pr["number"] > 0
                and pr["state"] in {"open", "closed"}
                and (pr["state"] == "open" or pr["merged_at"] is not None)
                and safe_ref(pr["head"]["ref"]) and safe_ref(pr["base"]["ref"]),
                "Stack contains a closed unmerged PR or invalid branch")
        result.append({"pr": pr["number"], "merged": pr["merged_at"] is not None,
                       "head_ref": pr["head"]["ref"], "base_ref": pr["base"]["ref"]})
    require(result and len({item["pr"] for item in result}) == len(result),
            "Missing or duplicate native stack members")
    return result


def freeze(api, request, pr):
    number = pr["stack"]["number"]
    require(type(number) is int and number > 0, "Invalid native stack number")
    root = f"repos/{request['repo']}"
    detail = api.call(f"{root}/stacks/{number}")
    require(detail["number"] == number and detail["open"] is True
            and safe_ref(detail["base"]["ref"]), "Native stack is not open")
    snapshot = members(detail)
    base_ref = detail["base"]["ref"]
    base_sha = api.call(f"{root}/git/ref/heads/{base_ref}")["object"]["sha"]
    require(isinstance(base_sha, str) and SHA.fullmatch(base_sha), "Invalid stack trunk tip")
    layers = []
    previous = base_ref
    for member in snapshot:
        if member["merged"]:
            require(not layers, "Merged stack member above an open PR")
            continue
        live = api.call(f"{root}/pulls/{member['pr']}")
        require(live.get("stack", {}).get("number") == number
                and live["head"]["ref"] == member["head_ref"]
                and live["base"]["ref"] == member["base_ref"] == previous
                and live["head"]["repo"]["id"] == request["repo_id"]
                and not live.get("auto_merge") and not live.get("merge_queue_entry"),
                "Native stack branch chain changed or a member is queued to merge")
        owner = attributed_owner(api, live, request["repo"], request["authorized_actor_id"])
        layer = eligible(live, request["repo"], request["authorized_actor_id"],
                         "pr_conflict_resolver", owner)
        base = api.call(f"{root}/git/ref/heads/{previous}")["object"]["sha"]
        comparison = api.call(f"{root}/compare/{base}...{layer['frozen_sha']}")
        require(comparison["base_commit"]["sha"] == base
                and SHA.fullmatch(comparison["merge_base_commit"]["sha"]),
                "Incomplete native stack merge-base identity")
        layer.update(base_ref=previous, base_sha=base,
                     merge_base_sha=comparison["merge_base_commit"]["sha"],
                     commit_author=request["commit_author"])
        layers.append(layer)
        previous = layer["head_ref"]
    require(any(layer["pr"] == request["pr"] for layer in layers),
            "Selected PR is absent from the open native stack")
    request["stack"] = {"number": number, "base_ref": base_ref, "base_sha": base_sha,
                        "members": snapshot, "layers": layers}
    validate(request)
    check(api, request)
    return request


def validate(request):
    stack = request["stack"]
    exact(stack, {"number", "base_ref", "base_sha", "members", "layers"})
    require(request["loop_kind"] == "pr_conflict_resolver"
            and request.get("input_mode") == "direct"
            and request["head_repo"] == request["repo"]
            and type(stack["number"]) is int and stack["number"] > 0
            and safe_ref(stack["base_ref"]) and SHA.fullmatch(stack["base_sha"])
            and isinstance(stack["members"], list) and isinstance(stack["layers"], list),
            "Invalid frozen native stack")
    for member in stack["members"]:
        exact(member, {"pr", "merged", "head_ref", "base_ref"})
        require(type(member["pr"]) is int and member["pr"] > 0
                and type(member["merged"]) is bool
                and safe_ref(member["head_ref"]) and safe_ref(member["base_ref"]),
                "Invalid frozen stack member")
    require(len({item["pr"] for item in stack["members"]}) == len(stack["members"])
            and [item["pr"] for item in stack["members"] if not item["merged"]]
            == [layer["pr"] for layer in stack["layers"]] and stack["layers"],
            "Frozen native stack order differs")
    previous, sha = stack["base_ref"], stack["base_sha"]
    for layer in stack["layers"]:
        require(layer["repo"] == layer["head_repo"] == request["repo"]
                and layer["repo_id"] == layer["head_repo_id"] == request["repo_id"]
                and layer["source_private"] is layer["target_private"] is False
                and layer["authorized_actor_id"] == request["authorized_actor_id"]
                and layer["commit_author"] == request["commit_author"]
                and safe_ref(layer["head_ref"]) and layer["head_ref"] != stack["base_ref"]
                and layer["base_ref"] == previous and layer["base_sha"] == sha
                and all(SHA.fullmatch(layer[key]) for key in
                        ("frozen_sha", "base_sha", "merge_base_sha")),
                "Invalid frozen native stack layer")
        previous, sha = layer["head_ref"], layer["frozen_sha"]
    require(len({layer["head_ref"] for layer in stack["layers"]}) == len(stack["layers"]),
            "Duplicate native stack branches")
    selected = next((layer for layer in stack["layers"] if layer["pr"] == request["pr"]), None)
    require(selected is not None and all(selected[key] == request[key] for key in
            ("head_ref", "frozen_sha", "base_ref", "base_sha", "merge_base_sha")),
            "Selected PR differs from frozen native stack layer")


def check(api, request, candidate=None):
    """Return exact live ref and PR tips after checking membership and ownership."""
    stack = request["stack"]
    root = f"repos/{request['repo']}"
    detail = api.call(f"{root}/stacks/{stack['number']}")
    require(detail["number"] == stack["number"] and detail["open"] is True
            and detail["base"]["ref"] == stack["base_ref"]
            and members(detail) == stack["members"],
            "Native stack membership changed after freeze")
    require(api.call(f"{root}/git/ref/heads/{stack['base_ref']}")["object"]["sha"]
            == stack["base_sha"], "Native stack trunk changed after freeze")
    tips = {entry["pr"]: entry["commit"] for entry in candidate["stack"]} if candidate else {}
    observed = []
    for layer in stack["layers"]:
        live = api.call(f"{root}/pulls/{layer['pr']}")
        allowed = {layer["frozen_sha"], tips.get(layer["pr"], layer["frozen_sha"])}
        require(live["head"]["sha"] in allowed
                and live.get("stack", {}).get("number") == stack["number"]
                and not live.get("auto_merge") and not live.get("merge_queue_entry"),
                "Native stack PR changed or became queued after freeze")
        frozen = layer_request(request, layer)
        unchanged(dict(frozen, frozen_sha=live["head"]["sha"]), live)
        if "pr_author_id" in layer:
            require(attributed_owner(api, live, request["repo"], request["authorized_actor_id"],
                                     request["commit_author"]) == request["commit_author"],
                    "Native stack PR ownership changed after freeze")
        ref = api.call(f"{root}/git/ref/heads/{layer['head_ref']}")["object"]["sha"]
        require(ref in allowed, "Native stack branch changed after freeze")
        observed.append((ref, live["head"]["sha"]))
    return observed


def refs(request):
    return [("stack-trunk", request["stack"]["base_sha"]),
            *[("stack-original-" + str(layer["pr"]), layer["frozen_sha"])
              for layer in request["stack"]["layers"]]]


def acquire_source(directory, request, fetch=None):
    from loop.source import public_fetch
    from loop.verify import git
    validate(request)
    if fetch is not None:
        fetch(directory)
    else:
        depth = 256
        while True:
            for _, sha in refs(request):
                public_fetch(directory, request, request["repo"], sha, depth=depth)
            git_dir = git(["rev-parse", "--git-dir"], directory).decode().strip()
            shallow_path = Path(directory) / git_dir / "shallow"
            shallow = set(shallow_path.read_bytes().splitlines()) if shallow_path.exists() else set()
            reachable = all(
                layer["merge_base_sha"].encode() in git(["rev-list", sha], directory).splitlines()
                for layer in request["stack"]["layers"]
                for sha in (layer["frozen_sha"], layer["base_sha"]))
            if reachable and not any(
                    shallow.intersection(git(["rev-list", layer["frozen_sha"],
                                              "^" + layer["merge_base_sha"]], directory).splitlines())
                    for layer in request["stack"]["layers"]):
                break
            require(shallow, "Native stack history is incomplete")
            depth *= 2
        public_fetch(directory, request, depth=depth)
    for name, sha in [*refs(request), ("snapshot", request["frozen_sha"]),
                      ("incoming", request["base_sha"]), ("review-base", request["merge_base_sha"])]:
        git(["update-ref", "refs/heads/" + name, sha], directory)
    require(git(["rev-parse", "FETCH_HEAD"], directory).decode().strip() == request["frozen_sha"],
            "Native stack source returned a different selected head")
    return source_identity(directory, request)


def source_identity(directory, request):
    from loop.verify import git, tree_entries
    for name, sha in refs(request):
        require(git(["rev-parse", "refs/heads/" + name], directory).decode().strip() == sha,
                "Native stack source ref mismatch")
        tree_entries(directory, sha)
    for layer in request["stack"]["layers"]:
        require(git(["merge-base", layer["base_sha"], layer["frozen_sha"]],
                    directory).decode().strip() == layer["merge_base_sha"],
                "Incomplete frozen native stack history")
    git(["fsck", "--strict", "--no-reflogs"], directory)
    return (git(["rev-parse", request["frozen_sha"] + "^{tree}"], directory).decode().strip(),
            int(git(["rev-list", "--count", *[sha for _, sha in refs(request)]], directory)))


def candidate_refs(request):
    return ["refs/heads/stack-" + str(layer["pr"]) for layer in request["stack"]["layers"]]


def derive(directory, request, tips, bundle_hash, *, blocked=False):
    from loop.candidates import TRAILER, prose
    from loop.conflicts import resolved_tree
    from loop.verify import git, safe_path, tree_entries
    entries = []
    base = request["stack"]["base_sha"]
    owner_name, owner_email = commit_author(request)
    for layer, tip in zip(request["stack"]["layers"], tips, strict=True):
        before = layer["frozen_sha"]
        original = git(["rev-list", before, "^" + layer["merge_base_sha"]],
                       directory).decode().splitlines()
        authors = {git(["show", "-s", "--format=%an%x00%ae", sha], directory).decode().strip()
                   for sha in original} | {owner_name + "\0" + owner_email}
        commits = []
        parent = base
        if not blocked:
            git(["merge-base", "--is-ancestor", base, tip], directory)
            history = git(["rev-list", "--reverse", tip, "^" + base], directory).decode().splitlines()
            require(len(history) <= 100, "Native stack layer exceeds candidate commit limit")
            for sha in history:
                parents = git(["show", "-s", "--format=%P", sha], directory).decode().strip().split()
                require(parents == [parent], "Native stack candidate must be linear from its rebased base")
                body = git(["show", "-s", "--format=%B", sha], directory).decode().strip()
                prose(body.splitlines()[0], summary=True)
                if sha not in original:
                    identity = git(["show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce", sha],
                                   directory).decode().strip().split("\0")
                    require("\0".join(identity[:2]) in authors
                            and identity[2:] == [owner_name, owner_email],
                            "Rebased commit author or committer differs from frozen authors and launch owner")
                    trailers = git(["interpret-trailers", "--parse"], directory, body.encode()).decode()
                    require(TRAILER in trailers.splitlines(), "Rebased commit lacks Copilot co-author trailer")
                tree_entries(directory, sha)
                paths = git(["diff", "--name-only", "--no-renames", "-z", parent, sha],
                            directory).decode().split("\0")[:-1]
                for path in paths:
                    safe_path(path, request)
                diff = git(["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary",
                            parent, sha], directory)
                commits.append({"commit": sha, "parent": parent,
                                "tree": git(["rev-parse", sha + "^{tree}"], directory).decode().strip(),
                                "subject": body.splitlines()[0], "changed_paths": paths,
                                "patch_sha256": hashlib.sha256(diff).hexdigest()})
                parent = sha
            require(parent == tip, "Unaccounted native stack candidate history")
        tree = git(["rev-parse", tip + "^{tree}"], directory).decode().strip()
        paths = git(["diff", "--name-only", "--no-renames", "-z", before, tip],
                    directory).decode().split("\0")[:-1]
        for path in paths:
            safe_path(path, request)
        if tip != before:
            resolved_tree(directory, dict(layer_request(request, layer), base_sha=base), tree)
        diff = git(["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary",
                    before, tip], directory)
        entries.append({"pr": layer["pr"], "head_ref": layer["head_ref"], "base": base,
                        "commit": tip, "parent": before, "tree": tree, "changed_paths": paths,
                        "changed": tip != before, "commits": commits if tip != before else [],
                        "patch_sha256": hashlib.sha256(diff).hexdigest()})
        base = tip
    selected = next(entry for entry in entries if entry["pr"] == request["pr"])
    candidate = {key: value for key, value in selected.items() if key not in {"pr", "head_ref", "base"}}
    candidate.update(changed=any(entry["changed"] for entry in entries), stack=entries,
                     cumulative_patch_sha256=selected["patch_sha256"], finding_commits={},
                     bundle_sha256=bundle_hash)
    return candidate


def chain(candidate, request):
    validate(request)
    require(isinstance(candidate.get("stack"), list)
            and len(candidate["stack"]) == len(request["stack"]["layers"])
            and not candidate["finding_commits"], "Invalid native stack candidate evidence")
    base = request["stack"]["base_sha"]
    for layer, entry in zip(request["stack"]["layers"], candidate["stack"], strict=True):
        require(entry["pr"] == layer["pr"] and entry["head_ref"] == layer["head_ref"]
                and entry["base"] == base and entry["parent"] == layer["frozen_sha"]
                and all(SHA.fullmatch(entry[key]) for key in ("commit", "parent", "tree"))
                and entry["changed"] == (entry["commit"] != entry["parent"])
                and isinstance(entry["commits"], list) and len(entry["commits"]) <= 100,
                "Native stack candidate differs from frozen branches")
        parent = base
        for commit in entry["commits"]:
            require(commit["parent"] == parent and all(SHA.fullmatch(commit[key])
                    for key in ("commit", "parent", "tree")),
                    "Invalid rebased native stack commit chain")
            parent = commit["commit"]
        require(not entry["changed"] or parent == entry["commit"],
                "Native stack candidate tip differs from linear history")
        base = entry["commit"]
    selected = next(entry for entry in candidate["stack"] if entry["pr"] == request["pr"])
    require(candidate["changed"] == any(entry["changed"] for entry in candidate["stack"])
            and all(candidate[key] == selected[key] for key in
                    ("commit", "parent", "tree", "commits", "changed_paths", "patch_sha256")),
            "Selected native stack candidate differs")


def package(request, repository, bundle):
    from loop.verify import git
    branches = candidate_refs(request)
    tips = [git(["rev-parse", ref], repository).decode().strip() for ref in branches]
    candidate = derive(repository, request, tips, "")
    require(candidate["changed"], "Rebase outcome requires a changed native stack")
    sources = list(dict.fromkeys(sha for _, sha in refs(request)))
    revisions = "\n".join([*branches, *["^" + sha for sha in sources]]) + "\n"
    pack = git(["pack-objects", "--stdout", "--revs"], repository, revisions.encode("ascii"))
    # Explicit refs retain unchanged or dropped-to-base tips that bundle create omits.
    header = ("# v2 git bundle\n"
              + "".join("-" + sha + " frozen source\n" for sha in sources)
              + "".join(sha + " " + ref + "\n" for sha, ref in zip(tips, branches, strict=True)))
    bundle.write_bytes(header.encode("ascii") + b"\n" + pack)
    git(["bundle", "verify", str(bundle)], repository)


def verify(files, request, fetch_source=None, repository_dir=None):
    import tempfile
    from contextlib import nullcontext
    from loop.source import acquire_source
    from loop.verify import git, parse_json
    result = parse_json(files["result.json"])
    with (tempfile.TemporaryDirectory(prefix="stack-verify-")
          if repository_dir is None else nullcontext(repository_dir)) as directory:
        git(["init", "--bare", "--quiet"], directory)
        acquire_source(directory, request, fetch_source)
        tips = [layer["frozen_sha"] for layer in request["stack"]["layers"]]
        bundle = files["candidate.bundle"]
        require(bool(bundle) == (result["outcome"] == "rebase"),
                "Native stack outcome contradicts candidate bundle")
        if bundle:
            path = Path(directory, "worker.bundle")
            path.write_bytes(bundle)
            heads = git(["bundle", "list-heads", str(path)], directory).decode().splitlines()
            require(len(heads) == len(tips), "Native stack bundle has missing or extra branch refs")
            exported = dict(line.split(" ", 1)[::-1] for line in heads)
            require(set(exported) == set(candidate_refs(request))
                    and all(SHA.fullmatch(sha) for sha in exported.values()),
                    "Native stack bundle branches differ")
            git(["bundle", "verify", str(path)], directory)
            git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-auto-maintenance",
                 str(path), *[ref + ":" + ref for ref in candidate_refs(request)]], directory)
            tips = [exported[ref] for ref in candidate_refs(request)]
        candidate = derive(directory, request, tips, hashlib.sha256(bundle).hexdigest(),
                           blocked=result["outcome"] == "blocked")
        require(result["outcome"] == "blocked"
                or (result["outcome"] == "rebase") == candidate["changed"],
                "Native stack outcome contradicts verified branch history")
        chain(candidate, request)
        git(["update-ref", "refs/heads/candidate", candidate["commit"]], directory)
        git(["fsck", "--strict", "--no-reflogs"], directory)
        return candidate


def push_args(request, candidate):
    chain(candidate, request)
    return ["--atomic",
            *["--force-with-lease=refs/heads/" + layer["head_ref"] + ":" + layer["frozen_sha"]
              for layer in request["stack"]["layers"]]], [
                  entry["commit"] + ":refs/heads/" + entry["head_ref"] for entry in candidate["stack"]]


def confirm_push(store, name, state, read, now):
    from loop.live import PUSH_PROPAGATION_SECONDS, cas, guard
    request = state["request"]
    intent = state["publication_intent"]
    candidate = intent["candidate"]
    observed = check(read, request, candidate)
    complete = all(ref == pr == entry["commit"]
                   for (ref, pr), entry in zip(observed, candidate["stack"], strict=True))
    if not complete:
        before = all(ref == pr == layer["frozen_sha"]
                     for (ref, pr), layer in zip(observed, request["stack"]["layers"], strict=True))
        if before and intent["status"] != "acknowledged":
            return cas(store, name, state, stage="blocked",
                       reason="blocked_uncertain_publication_requires_operator", next_check_at=now)
        if now >= intent.get("acknowledged_at", intent["recorded_at"]) + PUSH_PROPAGATION_SECONDS:
            return cas(store, name, state, stage="blocked",
                       reason="publication_API_propagation_timeout", next_check_at=now)
        return cas(store, name, state, reason="publication_API_propagation", next_check_at=now + 300)
    guard(store, name, state, read, now, sha=candidate["commit"])
    publication = {"request_id": request["request_id"], "generation": state["generation"],
                   "sha": candidate["commit"], "candidate": candidate, "acceptance": intent["acceptance"],
                   "confirmed_at": now, "effect": "push" if candidate["changed"] else "no_change"}
    return cas(store, name, state, stage="published", expected_sha=candidate["commit"],
               publications=state["publications"] + [publication],
               publication_intent=dict(intent, status="confirmed"), next_check_at=now)
