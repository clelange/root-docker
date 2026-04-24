#!/usr/bin/env python3
"""Discover, build-plan, and document ROOT container images.

The script intentionally uses only the Python standard library so that it can
run both locally and on GitHub-hosted runners without a bootstrap step.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


ROOT_REPO_URL = "https://github.com/root-project/root.git"
ACTIVE_BRANCHES_URL = "https://github.com/root-project/root/branches/active"
DOWNLOAD_INDEX_URL = "https://root.cern/download/"
README_BEGIN = "<!-- BEGIN ROOT-GHCR-IMAGES -->"
README_END = "<!-- END ROOT-GHCR-IMAGES -->"

STABLE_TAG_RE = re.compile(r"^v(?P<major>\d+)-(?P<minor>\d+)-(?P<patch>\d+)$")
PATCH_BRANCH_RE = re.compile(r"^v(?P<major>\d+)-(?P<minor>\d+)-00-patches$")
ROOT_BINARY_RE = re.compile(
    r"root_v(?P<version>\d+\.\d+\.\d+)\.Linux-(?P<platform>ubuntu\d+(?:\.\d+)?)-"
    r"x86_64-[^\"'<> ]+?\.tar\.gz"
)

# Contexts that are suitable for the primary release image. Keep this list in
# newest-supported-LTS-first order. It intentionally omits short-lived Ubuntu
# releases such as 25.10 even if this repository has a matching Dockerfile.
UBUNTU_LTS_CONTEXTS = {
    "ubuntu24.04": "ubuntu2404",
    "ubuntu22.04": "ubuntu2204",
    "ubuntu20.04": "ubuntu20",
}
UBUNTU_LTS_PRIORITY = tuple(UBUNTU_LTS_CONTEXTS)
UBUNTU_PLATFORM_ALIASES = {
    "ubuntu24": "ubuntu24.04",
    "ubuntu24.04": "ubuntu24.04",
    "ubuntu22": "ubuntu22.04",
    "ubuntu22.04": "ubuntu22.04",
    "ubuntu20": "ubuntu20.04",
    "ubuntu20.04": "ubuntu20.04",
}


@dataclass(frozen=True)
class RootTag:
    name: str
    major: int
    minor: int
    patch: int

    @property
    def version(self) -> str:
        return f"{self.major}.{self.minor:02d}.{self.patch:02d}"

    @property
    def branch_family(self) -> str:
        return f"v{self.major}-{self.minor:02d}-00-patches"

    @property
    def sort_key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


def run(args: Sequence[str]) -> str:
    completed = subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE)
    return completed.stdout


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def read_supported_branches(path: Path) -> list[str]:
    branches: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not PATCH_BRANCH_RE.match(line):
            raise ValueError(f"Unsupported branch format in {path}: {line}")
        branches.append(line)
    return branches


def stable_root_tag(tag: str) -> RootTag | None:
    match = STABLE_TAG_RE.match(tag)
    if not match:
        return None
    return RootTag(
        name=tag,
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
    )


def parse_ref_names(ls_remote_output: str, ref_prefix: str) -> list[str]:
    refs: list[str] = []
    for line in ls_remote_output.splitlines():
        if not line.strip():
            continue
        try:
            _sha, ref = line.split(None, 1)
        except ValueError:
            continue
        if ref.startswith(ref_prefix):
            refs.append(ref.removeprefix(ref_prefix))
    return refs


def fetch_upstream_tags(root_repo_url: str = ROOT_REPO_URL) -> list[str]:
    output = run(["git", "ls-remote", "--tags", "--refs", root_repo_url, "refs/tags/v*"])
    return parse_ref_names(output, "refs/tags/")


def parse_active_branch_page(branches_html: str) -> list[str]:
    branches: list[str] = []
    for match in re.finditer(r'href="/root-project/root/tree/([^"]+)"', branches_html):
        branch = html.unescape(match.group(1))
        if branch not in branches:
            branches.append(branch)
    return branches


def fetch_upstream_branches(
    root_repo_url: str = ROOT_REPO_URL, active_branches_url: str = ACTIVE_BRANCHES_URL
) -> list[str]:
    try:
        active_branches = parse_active_branch_page(fetch_text(active_branches_url))
        if active_branches:
            return active_branches
    except Exception as error:  # pragma: no cover - network fallback
        print(
            f"warning: could not fetch active branches from {active_branches_url}: {error}",
            file=sys.stderr,
        )

    output = run(
        [
            "git",
            "ls-remote",
            "--heads",
            root_repo_url,
            "refs/heads/v*-00-patches",
        ]
    )
    return parse_ref_names(output, "refs/heads/")


def parse_root_binaries(download_index_html: str) -> dict[str, dict[str, str]]:
    binaries: dict[str, dict[str, str]] = {}
    for match in ROOT_BINARY_RE.finditer(html.unescape(download_index_html)):
        filename = match.group(0)
        version = match.group("version")
        platform = UBUNTU_PLATFORM_ALIASES.get(match.group("platform"))
        if platform not in UBUNTU_LTS_CONTEXTS:
            continue
        binaries.setdefault(version, {})[platform] = filename
    return binaries


def choose_primary_ubuntu_binary(
    version: str, binaries: dict[str, dict[str, str]]
) -> tuple[str, str, str] | None:
    version_binaries = binaries.get(version, {})
    for platform in UBUNTU_LTS_PRIORITY:
        root_bin = version_binaries.get(platform)
        if root_bin:
            return platform, UBUNTU_LTS_CONTEXTS[platform], root_bin
    return None


def image_exists(image_ref: str, inspector: Callable[[str], bool] | None = None) -> bool:
    if inspector:
        return inspector(image_ref)
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image_ref],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.returncode == 0


def release_image_entry(
    tag: RootTag,
    image: str,
    platform: str,
    context: str,
    root_bin: str,
    latest: bool,
) -> dict[str, object]:
    image_tag = f"{tag.version}-{platform}"
    tags = [f"{image}:{image_tag}"]
    if latest:
        tags.append(f"{image}:latest")
    return {
        "kind": "release",
        "root_tag": tag.name,
        "root_version": tag.version,
        "root_bin": root_bin,
        "platform": platform,
        "context": context,
        "dockerfile": f"{context}/Dockerfile",
        "image_tag": image_tag,
        "tags": tags,
        "primary_tag": tags[0],
        "build_args": [f"ROOT_BIN={root_bin}"],
        "readme_dockerfile_url": (
            f"https://github.com/root-project/root-docker/blob/master/{context}/Dockerfile"
        ),
    }


def nightly_image_entry(branch: str, image: str) -> dict[str, object]:
    return {
        "kind": "nightly",
        "branch": branch,
        "context": "ubuntu_from_source",
        "dockerfile": "ubuntu_from_source/Dockerfile",
        "image_tag": branch,
        "tags": [f"{image}:{branch}"],
        "primary_tag": f"{image}:{branch}",
        "build_args": [f"ROOT_VERSION={branch}"],
    }


def build_plan(
    *,
    supported_branches: Sequence[str],
    upstream_tags: Sequence[str],
    upstream_branches: Sequence[str],
    download_index_html: str,
    image: str,
    skip_existing: bool = False,
    inspector: Callable[[str], bool] | None = None,
) -> dict[str, object]:
    supported = set(supported_branches)
    stable_tags = sorted(
        (
            parsed
            for parsed in (stable_root_tag(tag) for tag in upstream_tags)
            if parsed and parsed.branch_family in supported
        ),
        key=lambda item: item.sort_key,
    )
    binaries = parse_root_binaries(download_index_html)

    release_candidates: list[tuple[RootTag, str, str, str]] = []
    tags_without_binary: list[str] = []

    for tag in stable_tags:
        binary = choose_primary_ubuntu_binary(tag.version, binaries)
        if not binary:
            tags_without_binary.append(tag.name)
            continue
        platform, context, root_bin = binary
        release_candidates.append((tag, platform, context, root_bin))

    all_release_images: list[dict[str, object]] = []
    latest_release_tag = release_candidates[-1][0].name if release_candidates else None
    for tag, platform, context, root_bin in release_candidates:
        all_release_images.append(
            release_image_entry(
                tag=tag,
                image=image,
                platform=platform,
                context=context,
                root_bin=root_bin,
                latest=tag.name == latest_release_tag,
            )
        )

    release_images = [
        entry
        for entry in all_release_images
        if not skip_existing or not image_exists(str(entry["primary_tag"]), inspector)
    ]

    nightly_images = [nightly_image_entry(branch, image) for branch in supported_branches]
    upstream_patch_branches = sorted(
        {branch for branch in upstream_branches if PATCH_BRANCH_RE.match(branch)},
        key=branch_sort_key,
    )
    missing_branches = [
        branch for branch in upstream_patch_branches if branch not in supported
    ]

    return {
        "image": image,
        "all_release_images": all_release_images,
        "release_images": release_images,
        "nightly_images": nightly_images,
        "missing_branches": missing_branches,
        "tags_without_binary": tags_without_binary,
        "supported_branches": list(supported_branches),
    }


def branch_sort_key(branch: str) -> tuple[int, int]:
    match = PATCH_BRANCH_RE.match(branch)
    if not match:
        return (0, 0)
    return (int(match.group("major")), int(match.group("minor")))


def matrix(entries: Sequence[dict[str, object]]) -> dict[str, object]:
    return {"include": list(entries)}


def write_github_output(path: Path, outputs: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for name, value in outputs.items():
            output.write(f"{name}<<__ROOT_IMAGES__\n{value}\n__ROOT_IMAGES__\n")


def render_readme_section(plan: dict[str, object]) -> str:
    image = str(plan["image"])
    releases = list(plan["all_release_images"])
    nightlies = list(plan["nightly_images"])
    missing = list(plan["missing_branches"])

    lines = [
        README_BEGIN,
        "",
        "Images built by the GitHub Actions automation are published to GHCR.",
        f"The current development target is `{image}`.",
        "When this workflow runs in `root-project/root-docker`, set the repository",
        "variable `GHCR_NAMESPACE=root-project` to publish the same image tags under",
        "`ghcr.io/root-project/root` without changing the workflow.",
        "",
        "Pull the latest supported stable release with:",
        "",
        "```",
        f"docker pull {image}:latest",
        "```",
        "",
        "### Active release images",
        "",
    ]

    if releases:
        lines.extend(
            [
                "| Image tag | ROOT tag | Dockerfile |",
                "| --- | --- | --- |",
            ]
        )
        for entry in sorted(
            releases,
            key=lambda item: tuple(int(part) for part in str(item["root_version"]).split(".")),
            reverse=True,
        ):
            image_tag = str(entry["image_tag"])
            root_tag = str(entry["root_tag"])
            dockerfile_url = str(entry["readme_dockerfile_url"])
            lines.append(
                f"| `{image}:{image_tag}` | `{root_tag}` | "
                f"[{entry['dockerfile']}]({dockerfile_url}) |"
            )
        lines.append("")
        latest = next((entry for entry in releases if f"{image}:latest" in entry["tags"]), None)
        if latest:
            lines.append(f"`{image}:latest` points to `{latest['image_tag']}`.")
            lines.append("")
    else:
        lines.extend(["No active release images were discovered.", ""])

    lines.extend(
        [
            "### Nightly branch images",
            "",
            "Tracked patch branches are rebuilt nightly from source and tagged with the",
            "upstream branch name.",
            "",
        ]
    )
    if nightlies:
        lines.extend(["| Image tag | Upstream branch |", "| --- | --- |"])
        for entry in nightlies:
            branch = str(entry["branch"])
            lines.append(f"| `{image}:{branch}` | `{branch}` |")
        lines.append("")
    else:
        lines.extend(["No nightly branches are currently tracked.", ""])

    if missing:
        lines.extend(
            [
                "### Untracked upstream patch branches",
                "",
                "These upstream patch branches were detected but are not in",
                "`supported-branches.txt`:",
                "",
            ]
        )
        lines.extend(f"- `{branch}`" for branch in missing)
        lines.append("")

    lines.append(README_END)
    return "\n".join(lines)


def update_readme(readme_path: Path, plan: dict[str, object]) -> None:
    readme = readme_path.read_text(encoding="utf-8")
    section = render_readme_section(plan)
    if README_BEGIN not in readme or README_END not in readme:
        raise ValueError(
            f"{readme_path} must contain {README_BEGIN} and {README_END} markers"
        )
    before, rest = readme.split(README_BEGIN, 1)
    _old, after = rest.split(README_END, 1)
    readme_path.write_text(before + section + after, encoding="utf-8")


def render_missing_branches_issue(plan: dict[str, object]) -> str:
    missing = list(plan["missing_branches"])
    lines = [
        "The ROOT image automation found upstream patch branches that are not listed",
        "in `supported-branches.txt`.",
        "",
        "Please decide whether each branch should be published as a nightly image.",
        "",
    ]
    if missing:
        lines.extend(f"- [ ] `{branch}`" for branch in missing)
    else:
        lines.append("No untracked branches are currently detected.")
    lines.extend(
        [
            "",
            "This issue was generated by `.github/workflows/images.yml`.",
        ]
    )
    return "\n".join(lines)


def load_plan(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def command_plan(args: argparse.Namespace) -> int:
    supported = read_supported_branches(Path(args.supported_branches))
    upstream_tags = (
        Path(args.tags_file).read_text(encoding="utf-8").splitlines()
        if args.tags_file
        else fetch_upstream_tags(args.root_repo_url)
    )
    upstream_branches = (
        Path(args.branches_file).read_text(encoding="utf-8").splitlines()
        if args.branches_file
        else fetch_upstream_branches(args.root_repo_url, args.active_branches_url)
    )
    download_index_html = (
        Path(args.download_index_file).read_text(encoding="utf-8")
        if args.download_index_file
        else fetch_text(args.download_index_url)
    )

    plan = build_plan(
        supported_branches=supported,
        upstream_tags=upstream_tags,
        upstream_branches=upstream_branches,
        download_index_html=download_index_html,
        image=args.image,
        skip_existing=args.skip_existing,
    )

    plan_json = json.dumps(plan, sort_keys=True, indent=2)
    if args.plan_json:
        Path(args.plan_json).write_text(plan_json + "\n", encoding="utf-8")
    else:
        print(plan_json)

    if args.github_output:
        write_github_output(
            Path(args.github_output),
            {
                "release_matrix": json.dumps(matrix(plan["release_images"])),
                "nightly_matrix": json.dumps(matrix(plan["nightly_images"])),
                "release_count": str(len(plan["release_images"])),
                "nightly_count": str(len(plan["nightly_images"])),
                "missing_branches_json": json.dumps(plan["missing_branches"]),
            },
        )

    if args.fail_on_missing_branches and plan["missing_branches"]:
        return 2
    return 0


def command_update_readme(args: argparse.Namespace) -> int:
    update_readme(Path(args.readme), load_plan(Path(args.plan_json)))
    return 0


def command_missing_issue(args: argparse.Namespace) -> int:
    print(render_missing_branches_issue(load_plan(Path(args.plan_json))))
    return 0


def command_local_build_args(args: argparse.Namespace) -> int:
    plan = load_plan(Path(args.plan_json))
    releases = list(plan["all_release_images"])
    if not releases:
        raise SystemExit("No release images are available in the plan")
    latest = max(
        releases,
        key=lambda item: tuple(int(part) for part in str(item["root_version"]).split(".")),
    )
    for build_arg in latest["build_args"]:
        print(build_arg)
    return 0


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    subcommands = argument_parser.add_subparsers(dest="command", required=True)

    plan_parser = subcommands.add_parser("plan", help="discover images and write a build plan")
    plan_parser.add_argument("--supported-branches", default="supported-branches.txt")
    plan_parser.add_argument("--image", default="ghcr.io/clelange/root")
    plan_parser.add_argument("--root-repo-url", default=ROOT_REPO_URL)
    plan_parser.add_argument("--active-branches-url", default=ACTIVE_BRANCHES_URL)
    plan_parser.add_argument("--download-index-url", default=DOWNLOAD_INDEX_URL)
    plan_parser.add_argument("--tags-file")
    plan_parser.add_argument("--branches-file")
    plan_parser.add_argument("--download-index-file")
    plan_parser.add_argument("--skip-existing", action="store_true")
    plan_parser.add_argument("--fail-on-missing-branches", action="store_true")
    plan_parser.add_argument("--plan-json")
    plan_parser.add_argument("--github-output")
    plan_parser.set_defaults(func=command_plan)

    readme_parser = subcommands.add_parser(
        "update-readme", help="replace the generated README image section"
    )
    readme_parser.add_argument("--plan-json", required=True)
    readme_parser.add_argument("--readme", default="README.md")
    readme_parser.set_defaults(func=command_update_readme)

    issue_parser = subcommands.add_parser(
        "missing-branches-issue", help="render an issue body for untracked branches"
    )
    issue_parser.add_argument("--plan-json", required=True)
    issue_parser.set_defaults(func=command_missing_issue)

    local_parser = subcommands.add_parser(
        "local-build-args", help="print build args for the newest release image in a plan"
    )
    local_parser.add_argument("--plan-json", required=True)
    local_parser.set_defaults(func=command_local_build_args)

    return argument_parser


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
