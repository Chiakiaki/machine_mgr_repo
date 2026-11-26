"""Local vs ssh backend selection and identical semantics."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs.backend import LocalBackend, make_backend
from local_machines.registry import Machine


def test_local_backend_shell_semantics():
    b = LocalBackend()
    assert b.run("echo hello").strip() == "hello"
    code, out, _ = b.try_run("echo out; exit 4")
    assert code == 4 and out.strip() == "out"


def test_local_backend_file_operations(tmp_path):
    b = LocalBackend()
    path = str(tmp_path / "command.sh")
    b.write_file(path, "#!/usr/bin/env bash\necho hi\n", mode="755")
    assert b.exists(path) and not b.is_dir(path)
    assert b.read_file(path).startswith("#!/usr/bin/env bash")
    assert oct(Path(path).stat().st_mode)[-3:] == "755"


def test_local_backend_resolve_path():
    b = LocalBackend()
    assert b.resolve_path("~/project") == str(Path.home() / "project")
    assert b.resolve_path("/abs/path") == "/abs/path"


def test_local_backend_git_head(tmp_path):
    import subprocess
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a",
                    "commit", "-q", "--allow-empty", "-m", "x"], cwd=repo, check=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert LocalBackend().git_head(str(repo)) == head


def test_make_backend_selection():
    assert isinstance(make_backend(local=True), LocalBackend)


def test_machine_is_local_flag():
    assert Machine(mac="02:00:aa:bb:cc:01", backend="local").is_local
    assert Machine(mac="cc:5e:f8:07:36:13", backend="ssh").is_local is False
    # default stays ssh for backwards compatibility with existing manifests
    assert Machine(mac="cc:5e:f8:07:36:13").is_local is False


def test_registry_roundtrips_backend(tmp_path):
    from local_machines.registry import Registry
    path = tmp_path / "m.yaml"
    reg = Registry(path, path.parent / "inventory.yaml")
    reg.upsert(Machine(mac="02:00:aa:bb:cc:01", name="me", backend="local"))
    reg.save()
    assert Registry(path, path.parent / "inventory.yaml").find("me").backend == "local"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
