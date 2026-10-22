import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from loop.candidates import TRAILER
from loop.freeze import freeze
from loop.inputs import acquire
from loop.live import confirm_push, publish, start, watch_single
from loop.policy import (Rejected, canonical, candidate_outcome, commit_author, digest,
                         pipeline_limit)
from loop.publication import authenticated_push, chain, import_candidate
from loop.source import acquire_source
from loop.stacks import check, derive, layer_request, push_args, refs
from loop.verify import git, verify, verify_candidate
from loop.worker_output import package
from tests.test_live import TEST_TOKEN, personal_pr, zipped
from tests.test_loop import FakeAPI, MemoryState, REVISION, run
from tests.test_pr_tasks import objects, result, task_request


class StackRead:
    def __init__(self, directory, request, trunk, lower, upper):
        self.directory = directory
        self.repo = request["repo"]
        self.owner = request["commit_author"]
        self.tips = {"main": trunk, "lower": lower, "upper": upper}
        self.prs = {}
        for number, ref, base in ((1, "lower", "main"), (2, "upper", "lower")):
            pr = personal_pr()
            pr.update(number=number, title=f"Layer {number}", body="Original body",
                      state="open", merged=False, merged_at=None, stack={"number": 3})
            repository = dict(pr["base"]["repo"], full_name=self.repo,
                              id=request["repo_id"], private=False)
            pr["head"] = {"ref": ref, "sha": self.tips[ref], "repo": repository}
            pr["base"] = {"ref": base, "sha": self.tips[base], "repo": repository}
            self.prs[number] = pr
        self.merged = []

    def detail(self):
        return {"number": 3, "open": True, "base": {"ref": "main"},
                "pull_requests": copy.deepcopy(self.merged + list(self.prs.values()))}

    def call(self, path, method="GET", data=None, **kwargs):
        assert method == "GET"
        root = f"repos/{self.repo}"
        if path == f"user/{self.owner['id']}":
            return self.owner
        if path == root + "/stacks/3":
            return self.detail()
        if path.startswith(root + "/git/ref/heads/"):
            return {"object": {"sha": self.tips[path.split("/heads/", 1)[1]]}}
        if path.startswith(root + "/compare/"):
            base, head = path.split("/compare/", 1)[1].split("...")
            return {"status": "ahead", "base_commit": {"sha": base},
                    "merge_base_commit": {"sha": git(["merge-base", base, head],
                                                       self.directory).decode().strip()}}
        if path.startswith(root + "/pulls/"):
            number = int(path.rsplit("/", 1)[1])
            pr = copy.deepcopy(self.prs[number])
            base = git(["merge-base", pr["base"]["sha"], pr["head"]["sha"]],
                       self.directory).decode().strip()
            if kwargs.get("raw"):
                return git(["diff", base, pr["head"]["sha"]], self.directory)
            pr["changed_files"] = len(self.pages(path + "/files"))
            return pr
        raise AssertionError(path)

    def pages(self, path, key=None):
        number = int(path.split("/pulls/", 1)[1].split("/")[0])
        pr = self.prs[number]
        base = git(["merge-base", pr["base"]["sha"], pr["head"]["sha"]],
                   self.directory).decode().strip()
        stats = git(["diff", "--numstat", base, pr["head"]["sha"]], self.directory).decode()
        return [{"filename": path, "additions": int(added), "deletions": int(deleted)}
                for added, deleted, path in (line.split("\t") for line in stats.splitlines())]

    def publish(self, candidate, *, prs=True):
        for entry in candidate["stack"]:
            self.tips[entry["head_ref"]] = entry["commit"]
            if prs:
                self.prs[entry["pr"]]["head"]["sha"] = entry["commit"]
        if prs:
            self.prs[2]["base"]["sha"] = self.tips["lower"]


class StackTests(unittest.TestCase):
    def context(self, directory, *, linear=False, selected=2):
        git(["init", "--bare", "--quiet"], directory)
        old = objects(directory, {"A.txt": "old\n", "B.txt": "old\n"})
        trunk = objects(directory, {"A.txt": "incoming\n", "B.txt": "old\n",
                                    "Upstream.txt": "incoming\n"}, [old])
        lower_tree = {"A.txt": "lower and incoming\n", "B.txt": "old\n",
                      "Upstream.txt": "incoming\n"}
        upper_tree = dict(lower_tree, **{"B.txt": "upper\n"})
        if linear:
            lower = objects(directory, lower_tree, [trunk], "Lower")
            upper = objects(directory, upper_tree, [lower], "Upper")
        else:
            original_lower = objects(directory, {"A.txt": "lower\n", "B.txt": "old\n"}, [old])
            original_upper = objects(directory, {"A.txt": "lower\n", "B.txt": "upper\n"},
                                     [original_lower])
            lower = objects(directory, lower_tree, [original_lower, trunk], "Merge lower")
            upper = objects(directory, upper_tree, [original_upper, lower], "Merge upper")
        req = task_request("pr_conflict_resolver")
        read = StackRead(directory, req, trunk, lower, upper)
        req = freeze(read, selected, REVISION, 100, read.repo, loop_kind="pr_conflict_resolver")
        req.update(mode="publish", publication=task_request("pr_conflict_resolver")["publication"])
        req = acquire(read, req, directory)
        for name, sha in refs(req):
            git(["update-ref", "refs/heads/" + name, sha], directory)
        git(["update-ref", "refs/heads/snapshot", req["frozen_sha"]], directory)
        Path(directory, "FETCH_HEAD").write_text(req["frozen_sha"] + "\n", encoding="ascii")
        return req, read

    def fetch(self, source, request):
        def fetch(directory):
            for _, sha in refs(request):
                git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-auto-maintenance",
                     source, sha], directory)
            git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-auto-maintenance",
                 source, request["frozen_sha"]], directory)
        return fetch

    def rewritten(self, directory, req, *, bad_author=False):
        base = req["stack"]["base_sha"]
        tips = []
        for layer in req["stack"]["layers"]:
            tree = git(["rev-parse", layer["frozen_sha"] + "^{tree}"], directory).decode().strip()
            name, email = commit_author(req)
            author = "Wrong <wrong@invalid>" if bad_author else "T <t@invalid>"
            text = (f"tree {tree}\nparent {base}\nauthor {author} 1 +0000\n"
                    f"committer {name} <{email}> 1 +0000\n\nRebase layer {layer['pr']}\n\n{TRAILER}\n")
            tip = git(["hash-object", "-t", "commit", "-w", "--stdin"],
                      directory, text.encode()).decode().strip()
            git(["update-ref", "refs/heads/stack-" + str(layer["pr"]), tip], directory)
            tips.append(tip)
            base = tip
        return tips

    def files(self, source, request, output):
        value = result(request, "rebase")
        Path(output, "result.json").write_bytes(canonical(value))
        Path(output, "diagnostics.txt").write_text("Preserved both layer diffs and incoming changes.",
                                                 encoding="ascii")
        with tempfile.TemporaryDirectory() as worker:
            git(["init", "--quiet"], worker)
            acquire_source(worker, request, self.fetch(source, request))
            git(["checkout", "--quiet", "--detach", request["frozen_sha"]], worker)
            tips = self.rewritten(worker, request)
            package(request, worker, Path(output))
        return {path.name: path.read_bytes() for path in Path(output).iterdir()}, tips

    def test_native_stack_freezes_every_open_layer_and_diffs(self):
        with tempfile.TemporaryDirectory() as directory:
            req, read = self.context(directory)
            self.assertEqual([1, 2], [layer["pr"] for layer in req["stack"]["layers"]])
            self.assertEqual(req["base_sha"], req["merge_base_sha"])
            self.assertEqual({"1", "2"}, set(req["inputs"]["stack_diffs"]))
            self.assertEqual(digest(req["inputs"]["stack_diffs"]),
                             req["inputs"]["identity"]["stack_diffs_sha256"])
            pipeline_limit(req)
            _, state = start(MemoryState(), FakeAPI(), req, "", 0, True,
                             "fine_grained_pat", [], True, 100)
            self.assertEqual("ready", state["stage"])
            self.assertEqual(0, state["iteration"])
            read.tips["lower"] = "a" * 40
            with self.assertRaisesRegex(Rejected, "branch changed"):
                check(read, req)

    def test_partial_merge_membership_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            req, read = self.context(directory)
            merged = copy.deepcopy(read.prs[1])
            merged.update(number=99, state="closed", merged=True, merged_at="2026-10-21T00:00:00Z")
            read.merged = [merged]
            frozen = freeze(read, 2, REVISION, 100, read.repo, loop_kind="pr_conflict_resolver")
            self.assertEqual([99, 1, 2], [member["pr"] for member in frozen["stack"]["members"]])
            self.assertEqual([1, 2], [layer["pr"] for layer in frozen["stack"]["layers"]])
            read.merged[0]["merged_at"] = None
            with self.assertRaisesRegex(Rejected, "closed unmerged"):
                check(read, frozen)

    def test_bot_and_human_stack_layers_keep_their_own_author_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            _, read = self.context(directory)
            read.prs[1]["user"].update(id=999, type="Bot", login="dependabot[bot]")
            for selected in (1, 2):
                with self.subTest(selected=selected):
                    req = freeze(read, selected, REVISION, 100, read.repo,
                                 loop_kind="pr_conflict_resolver")
                    req = acquire(read, req, directory)
                    check(read, req)
                    lower, upper = req["stack"]["layers"]
                    self.assertEqual(999, layer_request(req, lower)["pr_author_id"])
                    self.assertNotIn("pr_author_id", layer_request(req, upper))
                    read.prs[1]["user"]["id"] = 1000
                    with self.assertRaisesRegex(Rejected, "Target changed after freeze"):
                        check(read, req)
                    read.prs[1]["user"]["id"] = 999

    def test_source_deepens_past_merge_bases_to_keep_side_history_complete(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as acquired:
            req, _ = self.context(directory)
            git(["init", "--bare", "--quiet"], acquired)
            depths = []
            def fetch(destination, request, repo=None, sha=None, depth=1):
                depths.append(depth)
                git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-auto-maintenance",
                     "--depth=" + str(depth // 256), Path(directory).as_uri(),
                     sha or request["frozen_sha"]], destination)
            with patch("loop.source.public_fetch", side_effect=fetch):
                acquire_source(acquired, req)
            self.assertIn(1024, depths)
            self.assertEqual(req["frozen_sha"],
                             git(["rev-parse", "snapshot"], acquired).decode().strip())
            shallow = Path(acquired, "shallow")
            boundaries = set(shallow.read_bytes().splitlines()) if shallow.exists() else set()
            for layer in req["stack"]["layers"]:
                history = git(["rev-list", layer["frozen_sha"], "^" + layer["merge_base_sha"]],
                              acquired).splitlines()
                self.assertFalse(boundaries.intersection(history))

    def test_overlapping_stack_tasks_wait_for_current_phases_not_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            req, read = self.context(directory)
            lower = freeze(read, 1, REVISION, 100, read.repo, loop_kind="pr_conflict_resolver")
            store = MemoryState()
            name, state = start(store, FakeAPI(), lower, "", 0, True,
                                "fine_grained_pat", [], True, 100)
            with self.assertRaisesRegex(Rejected, "active phase"):
                start(store, FakeAPI(), req, "", 0, True, "fine_grained_pat", [], True, 100)
            store.entries["request-" + lower["request_id"] + ".json"] = copy.deepcopy(state)
            store.entries[name].update(stage="complete", reason="single_pass_complete")
            _, started = start(store, FakeAPI(), req, "", 0, True,
                               "fine_grained_pat", [], True, 100)
            self.assertEqual("ready", started["stage"])

    def test_membership_trunk_ownership_and_layer_identity_changes_stop_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            req, read = self.context(directory)
            for mutation in ("trunk", "order", "author", "base", "head"):
                changed = copy.deepcopy(read)
                if mutation == "trunk":
                    changed.tips["main"] = "a" * 40
                elif mutation == "order":
                    changed.prs = dict(reversed(list(changed.prs.items())))
                elif mutation == "author":
                    changed.prs[1]["user"]["id"] += 1
                elif mutation == "base":
                    changed.prs[1]["base"]["ref"] = "other"
                else:
                    changed.prs[1]["head"]["sha"] = req["frozen_sha"]
                with self.subTest(mutation=mutation), self.assertRaises(Rejected):
                    check(changed, req)

    def test_old_merge_only_no_change_and_merge_candidates_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as verified:
            req, _ = self.context(directory)
            files = {"result.json": canonical(result(req)), "candidate.bundle": b""}
            with self.assertRaisesRegex(Rejected, "must be linear"):
                verify_candidate(files, req, self.fetch(directory, req), repository_dir=verified)
            files["result.json"] = canonical(result(req, "merge"))
            with self.assertRaisesRegex(Rejected, "Unknown worker outcome"):
                verify_candidate(files, req, self.fetch(directory, req))

    def test_linear_no_change_and_blocked_results_keep_all_tips(self):
        for linear, outcome in ((True, "no_change"), (False, "blocked")):
            with self.subTest(linear=linear), tempfile.TemporaryDirectory() as directory:
                req, _ = self.context(directory, linear=linear)
                files = {"result.json": canonical(result(req, outcome)), "candidate.bundle": b""}
                candidate = verify_candidate(files, req, self.fetch(directory, req))
                self.assertFalse(candidate["changed"])
                self.assertEqual([layer["frozen_sha"] for layer in req["stack"]["layers"]],
                                 [entry["commit"] for entry in candidate["stack"]])
                candidate_outcome(result(req, outcome), req, candidate)

    def test_packaging_verification_and_import_rebase_the_whole_stack_from_any_member(self):
        for selected in (1, 2):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as directory, \
                    tempfile.TemporaryDirectory() as output, tempfile.TemporaryDirectory() as verified, \
                    tempfile.TemporaryDirectory() as imported:
                req, _ = self.context(directory, selected=selected)
                files, tips = self.files(directory, req, output)
                candidate = verify_candidate(files, req, self.fetch(directory, req), repository_dir=verified)
                candidate_outcome(result(req, "rebase"), req, candidate)
                chain(candidate, req)
                self.assertEqual(tips[selected - 1], candidate["commit"])
                self.assertEqual(tips, [entry["commit"] for entry in candidate["stack"]])
                self.assertEqual([req["stack"]["base_sha"], tips[0]],
                                 [entry["commits"][0]["parent"] for entry in candidate["stack"]])
                self.assertEqual([], git(["rev-list", "--min-parents=2", tips[-1],
                                          "^" + req["stack"]["base_sha"]], verified).splitlines())
                for layer, tip in zip(req["stack"]["layers"], tips):
                    self.assertEqual(git(["rev-parse", layer["frozen_sha"] + "^{tree}"], verified),
                                     git(["rev-parse", tip + "^{tree}"], verified))
                import_candidate(imported, Path(output, "candidate.bundle"), req, candidate,
                                 self.fetch(directory, req))
                self.assertEqual(candidate["commit"],
                                 git(["rev-parse", "candidate"], imported).decode().strip())

    def test_missing_refs_diff_drift_and_foreign_authors_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as output:
            req, _ = self.context(directory)
            files, _ = self.files(directory, req, output)
            changed = copy.deepcopy(req)
            changed["inputs"]["stack_diffs"]["1"]["text"] = ""
            with self.assertRaisesRegex(Rejected, "complete frozen head"):
                verify_candidate(files, changed, self.fetch(directory, req))
            bad = self.rewritten(directory, req, bad_author=True)
            with self.assertRaisesRegex(Rejected, "author or committer"):
                derive(directory, req, bad, "")
            git(["update-ref", "-d", "refs/heads/stack-2"], directory)
            from loop.stacks import package as stack_package
            with self.assertRaisesRegex(Rejected, "Git validation failed"):
                stack_package(req, directory, Path(output, "bad.bundle"))

    def test_unchanged_selected_lower_layer_still_publishes_a_rebased_upper_layer(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as output:
            req, read = self.context(directory, linear=True, selected=1)
            upper = read.prs[2]["head"]["sha"]
            tree = git(["rev-parse", upper + "^{tree}"], directory).decode().strip()
            merged = git(["hash-object", "-t", "commit", "-w", "--stdin"], directory, (
                f"tree {tree}\nparent {upper}\nparent {read.tips['lower']}\n"
                "author T <t@invalid> 0 +0000\ncommitter T <t@invalid> 0 +0000\n\nMerge upper\n"
            ).encode()).decode().strip()
            read.prs[2]["head"]["sha"] = read.tips["upper"] = merged
            req = freeze(read, 1, REVISION, 100, read.repo, loop_kind="pr_conflict_resolver")
            req = acquire(read, req, directory)
            git(["init", "--quiet"], output)
            acquire_source(output, req, self.fetch(directory, req))
            git(["checkout", "--quiet", "--detach", req["frozen_sha"]], output)
            tips = self.rewritten(output, req)
            tips[0] = req["frozen_sha"]
            git(["update-ref", "refs/heads/stack-1", tips[0]], output)
            upper_tree = git(["rev-parse", merged + "^{tree}"], output).decode().strip()
            name, email = commit_author(req)
            tips[1] = git(["hash-object", "-t", "commit", "-w", "--stdin"], output, (
                f"tree {upper_tree}\nparent {tips[0]}\nauthor T <t@invalid> 1 +0000\n"
                f"committer {name} <{email}> 1 +0000\n\nRebase upper\n\n{TRAILER}\n"
            ).encode()).decode().strip()
            git(["update-ref", "refs/heads/stack-2", tips[1]], output)
            from loop.stacks import package as package_stack
            bundle = Path(output, "candidate.bundle")
            package_stack(req, output, bundle)
            candidate = verify_candidate({"result.json": canonical(result(req, "rebase")),
                                          "candidate.bundle": bundle.read_bytes()},
                                         req, self.fetch(directory, req))
            self.assertTrue(candidate["changed"])
            self.assertEqual([], candidate["commits"])
            self.assertEqual(req["frozen_sha"], candidate["commit"])
            self.assertEqual([False, True], [entry["changed"] for entry in candidate["stack"]])

    def test_bundle_cannot_omit_a_rebased_lower_layer(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as output:
            req, _ = self.context(directory)
            self.rewritten(directory, req)
            bundle = Path(output, "missing-lower.bundle")
            git(["bundle", "create", str(bundle), "refs/heads/stack-2",
                 *["^" + sha for _, sha in refs(req)]], directory)
            with self.assertRaisesRegex(Rejected, "missing or extra branch refs"):
                verify_candidate({"result.json": canonical(result(req, "rebase")),
                                  "candidate.bundle": bundle.read_bytes()}, req, self.fetch(directory, req))

    def test_layers_already_upstream_can_rebase_to_existing_tips(self):
        for empty_upper in (False, True):
            with self.subTest(empty_upper=empty_upper), tempfile.TemporaryDirectory() as directory, \
                    tempfile.TemporaryDirectory() as output:
                req, read = self.context(directory, linear=True, selected=1)
                tree = git(["rev-parse", read.tips["upper" if empty_upper else "lower"] + "^{tree}"],
                           directory).decode().strip()
                trunk = git(["hash-object", "-t", "commit", "-w", "--stdin"], directory, (
                    f"tree {tree}\nparent {read.tips['main']}\nauthor T <t@invalid> 2 +0000\n"
                    "committer T <t@invalid> 2 +0000\n\nIncorporate stack changes upstream\n"
                ).encode()).decode().strip()
                read.tips["main"] = read.prs[1]["base"]["sha"] = trunk
                req = freeze(read, 1, REVISION, 100, read.repo, loop_kind="pr_conflict_resolver")
                req = acquire(read, req, directory)
                upper = trunk
                if not empty_upper:
                    tree = git(["rev-parse", read.tips["upper"] + "^{tree}"], directory).decode().strip()
                    name, email = commit_author(req)
                    upper = git(["hash-object", "-t", "commit", "-w", "--stdin"], directory, (
                        f"tree {tree}\nparent {trunk}\nauthor T <t@invalid> 1 +0000\n"
                        f"committer {name} <{email}> 1 +0000\n\nRebase upper\n\n{TRAILER}\n"
                    ).encode()).decode().strip()
                for number, tip in ((1, trunk), (2, upper)):
                    git(["update-ref", "refs/heads/stack-" + str(number), tip], directory)
                from loop.stacks import package as package_stack
                bundle = Path(output, "candidate.bundle")
                package_stack(req, directory, bundle)
                candidate = verify_candidate({"result.json": canonical(result(req, "rebase")),
                                              "candidate.bundle": bundle.read_bytes()},
                                             req, self.fetch(directory, req))
                self.assertTrue(candidate["changed"])
                self.assertEqual(trunk, candidate["commit"])
                self.assertEqual([], candidate["commits"])
                self.assertEqual([trunk, upper], [entry["commit"] for entry in candidate["stack"]])

    def test_full_verified_publication_uses_one_atomic_push(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as output, \
                tempfile.TemporaryDirectory() as verified:
            req, read = self.context(directory)
            store = MemoryState()
            name, state = start(store, FakeAPI(), req, "", 0, True,
                                "fine_grained_pat", [], True, 100)
            req = state["request"]
            files, _ = self.files(directory, req, output)
            worker = run()
            payload = zipped(files)
            report = verify(payload, req, worker, {"id": 33, "digest": "sha256:" + "0" * 64},
                            self.fetch(directory, req), repository_dir=verified)
            report["verification_run"] = {"id": 555, "attempt": 1}
            state.update(stage="publish_pending", iteration=1, report=report,
                         run={"id": worker["id"], "attempt": 1},
                         verification_run=report["verification_run"])
            store.entries[name] = copy.deepcopy(state)
            from loop.publication import acceptance
            accepted = acceptance(state)
            candidate = report["candidate"]
            for entry in candidate["stack"]:
                git(["-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-auto-maintenance",
                     verified, entry["commit"]], directory)
            publisher = Mock(token=TEST_TOKEN)
            with patch("loop.live.authenticated_push", side_effect=lambda *args, **kwargs:
                       read.publish(kwargs["candidate"])) as push, \
                    patch("loop.live.time.time", return_value=101):
                published = publish(store, name, state, Mock(), read, publisher, 101,
                                    (accepted, candidate, verified))
            self.assertEqual("published", published["stage"])
            self.assertEqual(candidate, push.call_args.kwargs["candidate"])
            self.assertEqual(1, push.call_count)

    def test_atomic_leases_reject_the_whole_update_when_another_layer_moves(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as remote:
            req, _ = self.context(directory)
            tips = self.rewritten(directory, req)
            candidate = derive(directory, req, tips, "")
            git(["init", "--bare", "--quiet"], remote)
            initial = [layer["frozen_sha"] + ":refs/heads/" + layer["head_ref"]
                       for layer in req["stack"]["layers"]]
            git(["-c", "protocol.file.allow=always", "push", "--quiet", remote, *initial], directory)
            concurrent = objects(directory, {"Other.txt": "concurrent\n"},
                                 [req["stack"]["layers"][1]["frozen_sha"]])
            git(["-c", "protocol.file.allow=always", "push", "--quiet", remote,
                 concurrent + ":refs/heads/upper"], directory)
            options, refspecs = push_args(req, candidate)
            with self.assertRaises(Rejected):
                git(["-c", "protocol.file.allow=always", "push", *options, remote, *refspecs], directory)
            self.assertEqual(req["stack"]["layers"][0]["frozen_sha"],
                             git(["rev-parse", "lower"], remote).decode().strip())
            self.assertEqual(concurrent, git(["rev-parse", "upper"], remote).decode().strip())
            with patch("loop.publication.subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 0, b"", b"")) as execute:
                authenticated_push(directory, req, candidate["commit"], TEST_TOKEN, candidate=candidate)
            argv = execute.call_args.args[0]
            self.assertIn("--atomic", argv)
            self.assertTrue(all(option in argv for option in options))
            self.assertEqual(refspecs, argv[-2:])
            self.assertNotIn("--force", argv)

    def test_publication_waits_for_every_layer_and_never_retries_a_push(self):
        with tempfile.TemporaryDirectory() as directory:
            req, read = self.context(directory)
            tips = self.rewritten(directory, req)
            candidate = derive(directory, req, tips, "")
            store = MemoryState()
            name, state = start(store, FakeAPI(), req, "", 0, True,
                                "fine_grained_pat", [], True, 100)
            state.update(stage="publication_intent", iteration=1,
                         report={"dispositions": result(req, "rebase"), "candidate": candidate},
                         publication_intent={"status": "acknowledged", "candidate": candidate,
                                             "recorded_at": 100, "acceptance": {}})
            store.entries[name] = copy.deepcopy(state)
            read.publish(candidate, prs=False)
            pending = confirm_push(store, name, state, read, 101)
            self.assertEqual("publication_intent", pending["stage"])
            read.publish(candidate)
            published = confirm_push(store, name, pending, read, 102)
            self.assertEqual("published", published["stage"])
            self.assertEqual("confirmed", published["publication_intent"]["status"])
            complete = watch_single(store, name, published, read, 103)
            self.assertEqual("complete", complete["stage"])
            self.assertEqual("rebase", complete["task_completion"]["outcome"])
            self.assertEqual(1, len(complete["publications"]))


if __name__ == "__main__":
    unittest.main()
