"""Compatibility entry; implementation lives in paa.cloud_transfer."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paa.cloud_transfer import ssh_args, json_bytes, snapshot, upload, main  # noqa: E402,F401

if __name__ == "__main__":
    main()
