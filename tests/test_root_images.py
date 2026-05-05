import json
import tempfile
import unittest
from pathlib import Path

from scripts import root_images


DOWNLOAD_HTML = """
<a href="root_v6.38.04.Linux-ubuntu22.04-x86_64-gcc11.4.tar.gz">x</a>
<a href="root_v6.38.04.Linux-ubuntu24.04-x86_64-gcc13.3.tar.gz">x</a>
<a href="root_v6.36.10.Linux-ubuntu22.04-x86_64-gcc11.4.tar.gz">x</a>
<a href="root_v6.36.10.Linux-ubuntu25.10-x86_64-gcc15.2.tar.gz">x</a>
<a href="root_v6.32.22.Linux-ubuntu20.04-x86_64-gcc9.4.tar.gz">x</a>
<a href="root_v6.28.12.Linux-ubuntu22-x86_64-gcc11.4.tar.gz">x</a>
<a href="root_v6.26.14.Linux-ubuntu20-x86_64-gcc9.4.tar.gz">x</a>
"""


class RootImagesTest(unittest.TestCase):
    def test_stable_tag_parser_excludes_rc_and_suffixes(self):
        parsed = root_images.stable_root_tag("v6-38-04")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.version, "6.38.04")
        self.assertIsNone(root_images.stable_root_tag("v6-38-04-rc1"))
        self.assertIsNone(root_images.stable_root_tag("v6-30-00a"))

    def test_primary_ubuntu_binary_prefers_newest_supported_lts(self):
        binaries = root_images.parse_root_binaries(DOWNLOAD_HTML)
        self.assertEqual(
            root_images.choose_primary_ubuntu_binary("6.38.04", binaries),
            (
                "ubuntu24.04",
                "ubuntu2404",
                "root_v6.38.04.Linux-ubuntu24.04-x86_64-gcc13.3.tar.gz",
            ),
        )
        self.assertEqual(
            root_images.choose_primary_ubuntu_binary("6.36.10", binaries),
            (
                "ubuntu22.04",
                "ubuntu2204",
                "root_v6.36.10.Linux-ubuntu22.04-x86_64-gcc11.4.tar.gz",
            ),
        )
        self.assertEqual(
            root_images.choose_primary_ubuntu_binary("6.28.12", binaries),
            (
                "ubuntu22.04",
                "ubuntu2204",
                "root_v6.28.12.Linux-ubuntu22-x86_64-gcc11.4.tar.gz",
            ),
        )
        self.assertEqual(
            root_images.choose_primary_ubuntu_binary("6.26.14", binaries),
            (
                "ubuntu20.04",
                "ubuntu20",
                "root_v6.26.14.Linux-ubuntu20-x86_64-gcc9.4.tar.gz",
            ),
        )

    def test_build_plan_filters_existing_release_images(self):
        plan = root_images.build_plan(
            upstream_tags=[
                "v6-38-00",
                "v6-38-04",
                "v6-38-04-rc1",
                "v6-37-01",
                "v6-36-10",
            ],
            download_index_html=DOWNLOAD_HTML,
            image="ghcr.io/example/root",
            skip_existing=True,
            inspector=lambda image: image.endswith(":6.38.04-ubuntu24.04"),
        )

        self.assertEqual(
            [entry["image_tag"] for entry in plan["all_release_images"]],
            ["6.36.10-ubuntu22.04", "6.38.04-ubuntu24.04"],
        )
        self.assertEqual(
            [entry["image_tag"] for entry in plan["release_images"]],
            ["6.36.10-ubuntu22.04"],
        )
        latest = plan["all_release_images"][-1]
        self.assertIn("ghcr.io/example/root:latest", latest["tags"])

    def test_latest_uses_newest_release_with_binary(self):
        plan = root_images.build_plan(
            upstream_tags=["v6-38-04", "v6-40-00"],
            download_index_html=DOWNLOAD_HTML,
            image="ghcr.io/example/root",
        )

        self.assertEqual(plan["tags_without_binary"], ["v6-40-00"])
        self.assertEqual(
            plan["all_release_images"][-1]["image_tag"],
            "6.38.04-ubuntu24.04",
        )
        self.assertIn(
            "ghcr.io/example/root:latest",
            plan["all_release_images"][-1]["tags"],
        )

    def test_readme_generation_and_update(self):
        plan = root_images.build_plan(
            upstream_tags=["v6-38-04"],
            download_index_html=DOWNLOAD_HTML,
            image="ghcr.io/root-project/root",
        )
        section = root_images.render_readme_section(plan)
        self.assertIn("ghcr.io/root-project/root:6.38.04-ubuntu24.04", section)
        self.assertNotIn("Nightly branch images", section)

        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            readme.write_text(
                "before\n"
                f"{root_images.README_BEGIN}\nold\n{root_images.README_END}\n"
                "after\n",
                encoding="utf-8",
            )
            plan_path = Path(directory) / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            root_images.update_readme(readme, plan)
            content = readme.read_text(encoding="utf-8")
            self.assertIn("before", content)
            self.assertIn("after", content)
            self.assertNotIn("old", content)


if __name__ == "__main__":
    unittest.main()
