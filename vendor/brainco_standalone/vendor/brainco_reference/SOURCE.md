# Reference provenance

Copied 2026-10-27 from user-provided `/Users/Leslie/Downloads/oi-armi-main-2/oi-mi/acquisition/`.
`base.py` unchanged; `brainco_acquirer.py` changes only its base import to package-relative.
The source `oi-mi/pyproject.toml` declares MIT and authors `oi-mi contributors`; no separate LICENSE file was present in the supplied project root.
Only the acquisition backend is reused; no online classifier, game routing, pretrained model or user EEG records are copied.
The adapter uses incremental drained samples rather than sliding-window decoding output.
