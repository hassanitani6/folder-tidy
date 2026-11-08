import os
import sys

import pytest

from folder_tidy.cli import main


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")
@pytest.mark.parametrize("umask, expected", [(0o022, 0o644), (0o002, 0o664), (0o077, 0o600)])
def test_init_writes_the_rules_file_with_the_usual_permissions(tmp_path, umask, expected):
    dest = tmp_path / "rules.toml"
    old = os.umask(umask)
    try:
        assert main(["init", str(dest)]) == 0
    finally:
        os.umask(old)
    assert dest.stat().st_mode & 0o777 == expected
