"""pcbreview — deterministic review of KiCad PCB layouts and schematics."""


def console_utf8() -> None:
    """Make stdout/stderr able to carry the report's own characters.

    Every entry point calls this first. On Windows the console defaults to
    cp1252, which cannot encode `Ω`, `≤`, `≈`, `→`, `ε` or the CJK strings in
    `schematic.py` — so a run would complete, spend its whole cost, and then
    die on the final `print` with `UnicodeEncodeError`. That is not a cosmetic
    failure: it threw away a 574-second audit of the four-layer board, because
    `record_review` runs *after* the report is printed and never got there.

    `errors="replace"` rather than `strict`, because a console that genuinely
    cannot represent a character should show `?` and still deliver the other
    ten thousand lines. Guarded with `getattr`, since a caller may have
    replaced these streams with something that has no `reconfigure` — pytest's
    capture and `io.StringIO` both do, and neither has the problem anyway.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                # Already-detached or non-reconfigurable stream. Nothing to do,
                # and failing here would be worse than the encoding it fixes.
                pass
