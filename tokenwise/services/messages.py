"""Turkish wording for the pipeline's (English, CLI-facing) error messages, for the web page."""

from __future__ import annotations

import re

_RULES = [
    (
        re.compile(r"^the selector found no relevant files$"),
        "Değişecek dosya bulunamadı. 'Dosya ekle' ile ilgili dosyayı seçip tekrar dene.",
    ),
    (
        re.compile(r"^the edit changed nothing$"),
        "Model dosyada bir şey değiştirmedi. İsteği daha somut yaz ya da dosyayı elle seç.",
    ),
    (
        re.compile(r"^the check still fails after trying (.+)$"),
        "Öneri hazır ama proje kontrolü hâlâ geçmiyor ({0} denendi). "
        "Kontrol çıktısına bakıp 'Düzelt' ile hatayı anlat.",
    ),
    (
        re.compile(r"^no applicable edit from ([^;]+)(?:; last problem: (.*))?$", re.S),
        "Modelin önerisi dosyaya uygulanamadı ({0} denendi).{1} "
        "Daha güçlü bir model seç ya da dosyayı elle ekleyip tekrar dene.",
    ),
]


def turkish_error(text: str | None) -> str | None:
    """The Turkish message for a known pipeline error; anything else is returned as is."""
    if not text:
        return text
    for pattern, template in _RULES:
        found = pattern.match(text)
        if found:
            groups = [g or "" for g in found.groups()]
            if len(groups) > 1:
                groups[1] = f" Son sorun: {groups[1]}." if groups[1] else ""
            return template.format(*groups)
    return text
