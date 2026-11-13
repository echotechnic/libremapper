"""Create a disposable Git source in the isolated synthetic sidecar."""

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

artifacts = Path("/oxidized/topology")
files = sorted(artifacts.glob("*--lldp-neighbors"))
if not files or any(not path.name.endswith(".example.test--lldp-neighbors") for path in files):
    raise RuntimeError("Oxidized test seeding requires the isolated synthetic artifacts")
repository = Path("/oxidized.git")
if repository.exists():
    raise RuntimeError("Refusing to overwrite an existing Git repository")
with TemporaryDirectory() as directory:
    root = Path(directory)
    shutil.copytree(artifacts, root / "topology")
    hostname = files[0].name.removesuffix("--lldp-neighbors")
    (root / hostname).write_text(
        'oc-if:interfaces interface "IF_ACCESS" config name "IF_ACCESS" type point-to-point\n'
        'oc-if:interfaces interface "IF_ACCESS" ipv4 addresses address "192.0.2.0" '
        'config ip "192.0.2.0" prefix-length 31\n'
    )
    for arguments in [
        ["init", "-b", "main"],
        ["config", "user.name", "Synthetic test"],
        ["config", "user.email", "test@example.test"],
        ["add", "."],
        ["commit", "-m", "Synthetic parser source"],
        ["clone", "--bare", str(root), str(repository)],
    ]:
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
print("Synthetic Oxidized Git source prepared.")
