"""安装器不能删掉「碰巧同名」的目录。

``--force`` 曾经无条件 ``rmtree`` 目标目录：把 ``--dir`` 指到自己的代码目录，
而那里正好有个叫 ``cutwright`` 的项目，一条命令就没了。现在覆盖前先确认
目标里有 ``SKILL.md`` 且 ``name`` 对得上，对不上就拒绝，除非显式 ``--force-any``。
"""
from __future__ import annotations

import importlib.util
import os
import unittest

from . import tmpdir

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "skills", "cutwright", "scripts", "install.py")


def _load_install():
    spec = importlib.util.spec_from_file_location("cutwright_install", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)          # type: ignore[union-attr]
    return module


install = _load_install()


class IdentifySkillTest(unittest.TestCase):
    def test_real_skill_dir_is_recognised(self):
        self.assertTrue(install._is_our_skill(install.SKILL_DIR))

    def test_random_dir_is_not(self):
        with tmpdir("notskill") as root:
            os.makedirs(os.path.join(root, "cutwright"))
            with open(os.path.join(root, "cutwright", "main.py"), "w",
                      encoding="utf-8") as handle:
                handle.write("print('hello')\n")
            self.assertFalse(install._is_our_skill(os.path.join(root, "cutwright")))

    def test_wrong_name_is_not(self):
        with tmpdir("wrongname") as root:
            target = os.path.join(root, "cutwright")
            os.makedirs(target)
            with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write("---\nname: somethingelse\n---\n")
            self.assertFalse(install._is_our_skill(target))


class ForceGuardTest(unittest.TestCase):
    def test_refuses_to_delete_foreign_dir(self):
        with tmpdir("guard") as root:
            target = os.path.join(root, "cutwright")
            os.makedirs(target)
            keep = os.path.join(target, "我的重要文件.txt")
            with open(keep, "w", encoding="utf-8") as handle:
                handle.write("别删我")

            message = install._install_one(root, link=False, dry=False, force=True)
            self.assertIn("拒绝覆盖", message)
            self.assertTrue(os.path.isfile(keep), "外来文件被删了——这就是那条 bug")

    def test_force_any_replaces_foreign_dir(self):
        with tmpdir("guardany") as root:
            target = os.path.join(root, "cutwright")
            os.makedirs(target)
            keep = os.path.join(target, "我的重要文件.txt")
            with open(keep, "w", encoding="utf-8") as handle:
                handle.write("别删我")

            message = install._install_one(root, link=False, dry=False,
                                           force=True, force_any=True)
            self.assertIn("已复制", message)
            self.assertFalse(os.path.exists(keep), "给了 --force-any 还不删，说明没生效")
            self.assertTrue(os.path.isfile(os.path.join(target, "SKILL.md")))

    def test_fresh_install(self):
        with tmpdir("fresh") as root:
            message = install._install_one(root, link=False, dry=False, force=False)
            self.assertIn("已复制", message)
            self.assertTrue(os.path.isfile(os.path.join(root, "cutwright", "SKILL.md")))

    def test_second_install_skips_without_force(self):
        with tmpdir("skip") as root:
            install._install_one(root, link=False, dry=False, force=False)
            message = install._install_one(root, link=False, dry=False, force=False)
            self.assertIn("跳过", message)

    def test_reinstall_over_own_skill_is_allowed(self):
        """装过一份自己的技能，再加 --force 应该能覆盖（不然更新流程就断了）。"""
        with tmpdir("resync") as root:
            install._install_one(root, link=False, dry=False, force=False)
            message = install._install_one(root, link=False, dry=False, force=True)
            self.assertIn("已复制", message)


if __name__ == "__main__":
    unittest.main()
