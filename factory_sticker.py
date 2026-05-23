"""Shared sticker-rendering helper for the factory provisioners.

`flash_base_station.py` and `provision_gate.py` both end with a
divider / title / key-value / divider block that the operator prints
onto a physical sticker. Keeping the format here means the two scripts
stay visually aligned and can grow new fields (e.g. a QR code) without
drifting.

Stdlib-only by contract: imported by factory scripts which must run on
an unmodified operator laptop. `scripts/check_factory_deps.py`
allowlists this module name in `LOCAL_MODULES`.
"""

STICKER_WIDTH = 50
STICKER_TITLE = "PRINT THIS ON THE PRODUCT STICKER:"


def print_sticker(*kv_pairs: tuple[str, str]) -> None:
    """Render a product-sticker block to stdout.

    Args:
        kv_pairs: ``(label, value)`` tuples in the visual order they
            should appear. Labels are right-padded so every value
            starts in the same column.
    """
    if not kv_pairs:
        raise ValueError("print_sticker requires at least one (label, value) pair")
    label_col = max(len(label) for label, _ in kv_pairs) + 2  # ":" + space
    print("-" * STICKER_WIDTH)
    print(STICKER_TITLE)
    for label, value in kv_pairs:
        print(f"  {(label + ':').ljust(label_col)}{value}")
    print("-" * STICKER_WIDTH)
