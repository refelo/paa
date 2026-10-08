"""Compatibility entry; implementation lives in paa.cloud_importer."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paa.cloud_importer import merge_card_vectors, image_vectors, remove_absent_images, main  # noqa: E402,F401

if __name__ == "__main__":
    main()
