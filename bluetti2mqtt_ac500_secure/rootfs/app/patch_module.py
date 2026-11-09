"""Patch an exact user-supplied Linux Bluetti module only in a runtime copy."""
from pathlib import Path
import hashlib
import shutil
import struct

ORIGINAL_SHA = "23918adaf25fb32c910024cbcf5ab0daa4997e1299d2b215966c11d65689abf5"
TABLE_OFFSET = 0x80700
ROW_COUNT = 61
REPLACEMENTS = {
    (6184, 6184, 2): (3019, 3019, 1),
    (6181, 6181, 2): (170, 170, 3),
}

def prepare(src: Path, target: Path):
    for filename in ("_bluetti_crypt.so", "bluetti_crypt.py", "bluetti_device_licence.csv"):
        if not (src / filename).is_file():
            raise FileNotFoundError(f"Missing {src / filename}")
    content = (src / "_bluetti_crypt.so").read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    if sha != ORIGINAL_SHA:
        raise ValueError(f"Unsupported vendor binary SHA256 {sha}; refusing patch")
    modified = bytearray(content)
    entries = [struct.unpack_from("<HHH", modified, TABLE_OFFSET + i * 6)
               for i in range(ROW_COUNT)]
    for old, new in REPLACEMENTS.items():
        if entries.count(old) != 1:
            raise ValueError(f"Expected exactly one {old}; got {entries.count(old)}")
        if any(public <= new[1] < public + count
               for _internal, public, count in entries):
            raise ValueError(f"Register {new[1]} already mapped")
        i = entries.index(old)
        struct.pack_into("<HHH", modified, TABLE_OFFSET + i * 6, *new)
        entries[i] = new
    target.mkdir(parents=True, exist_ok=True)
    (target / "_bluetti_crypt.so").write_bytes(modified)
    shutil.copyfile(src / "bluetti_crypt.py", target / "bluetti_crypt.py")
    shutil.copyfile(src / "bluetti_device_licence.csv", target / "bluetti_device_licence.csv")
    return hashlib.sha256(modified).hexdigest()
