"""Turkish wording for the pipeline's (English, CLI-facing) error messages, for the web page."""

from __future__ import annotations

import re


def _budget(spent: str, limit: str, model: str, check: str) -> str:
    tail = "; öneri hazır ama proje kontrolü geçmiyor" if check else ""
    return (f"İstek başına bütçe doldu: ${limit} sınırın ${spent} kadarı harcandı, {model} için "
            f"pay kalmadı{tail}. tokenwise.toml'da max_usd_per_request değerini artır ya da "
            "isteği küçült.")  # fmt: skip


def _no_edit(tried: str, problem: str) -> str:
    last = f" Son sorun: {problem}." if problem else ""
    return (f"Modelin önerisi dosyaya uygulanamadı ({tried} denendi).{last} "
            "Daha güçlü bir model seç ya da dosyayı elle ekleyip tekrar dene.")  # fmt: skip


# (pattern, wording): wording is a fixed text or a function of the pattern's groups
_RULES = [
    (
        re.compile(
            r"^the request budget is used up: \$([\d.]+) of \$([\d.]+) spent, "
            r"not enough left for (\S+)(; the check still fails)?$"
        ),
        _budget,
    ),  # fmt: skip
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
        lambda tried: (
            f"Öneri hazır ama proje kontrolü hâlâ geçmiyor ({tried} denendi). "
            "Kontrol çıktısına bakıp 'Düzelt' ile hatayı anlat."
        ),
    ),
    (re.compile(r"^no applicable edit from ([^;]+)(?:; last problem: (.*))?$", re.S), _no_edit),
]


def turkish_error(text: str | None) -> str | None:
    """The Turkish message for a known pipeline error; anything else is returned as is."""
    if not text:
        return text
    for pattern, wording in _RULES:
        found = pattern.match(text)
        if found:
            groups = [g or "" for g in found.groups()]
            return str(wording(*groups)) if callable(wording) else str(wording)
    return text
