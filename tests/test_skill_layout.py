import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "splunk-query"
SKILL_MD = SKILL / "SKILL.md"


def frontmatter(text):
    head = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
    name = re.search(r"^name:\s*(\S+)\s*$", head, re.M).group(1)
    desc = re.search(r"^description:\s*>-\n((?:  .*\n?)+)", head + "\n", re.M).group(1)
    return name, " ".join(line.strip() for line in desc.splitlines())


def shipped_and_repo_files():
    for path in sorted(ROOT.rglob("*")):
        skip = {"__pycache__", ".git"} & set(path.relative_to(ROOT).parts)
        if path.is_file() and not skip and path.suffix in (".md", ".py", ".json", ""):
            yield path


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.text = SKILL_MD.read_text(encoding="utf-8")

    def test_frontmatter(self):
        name, description = frontmatter(self.text)
        self.assertEqual(name, "splunk-query")
        self.assertLess(len(description), 1024)

    def test_skill_md_is_short(self):
        self.assertLess(len(self.text.splitlines()), 300)

    def test_referenced_files_exist(self):
        refs = set(re.findall(r"`((?:reference|scripts)/[\w./-]+)`", self.text))
        self.assertTrue(refs)
        for ref in sorted(refs):
            self.assertTrue((SKILL / ref).exists(), ref)

    def test_no_install_paths(self):
        for path in SKILL.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                for bad in ("~/.claude/skills", "~/.agents/skills", "/Users/"):
                    self.assertNotIn(bad, text, "%s contains %s" % (path, bad))

    def test_ascii_only(self):
        for path in shipped_and_repo_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                self.assertTrue(line.isascii(), "%s:%d is not ASCII" % (path.relative_to(ROOT), number))


if __name__ == "__main__":
    unittest.main()
