# Gerçek ölçüm: yerel modeller, Windows dizüstü

- Modeller (Ollama): seçici `qwen2.5-coder:3b`, düzenleyici `qwen3.5:9b` (merdiven)
- Donanım: Intel i5-11400H, 16 GB RAM, RTX 3050 Ti 4 GB
- Komut: `tokenwise benchmark benchmark/github/tasks.toml --methods tokenwise`
- Sonuç: **15/18** kontrol geçti; 2 görev atlandı (bilgisayarda `php` ve `javac` yok)
- Kullanıcı yolları `%USERPROFILE%` ile değiştirildi.

| Task | Method | Model | Input tok | Output tok | Cost ($) | Lines changed | Check |
| --- | --- | --- | ---: | ---: | ---: | ---: | :---: |
| netprobe-chunk-size | tokenwise | qwen3.5:9b | 2,205 | 378 | 0.0000 | 3 | ✅ |
| netprobe-logger-with | tokenwise | qwen3.5:9b | 6,277 | 611 | 0.0000 | 9 | ✅ |
| nlp-aux-vp | tokenwise | qwen3.5:9b | 2,671 | 831 | 0.0000 | 2 | ❌ |
| nlp-predict-main | tokenwise | qwen3.5:9b | 6,148 | 946 | 0.0000 | 6 | ✅ |
| rag-settings-env | tokenwise | qwen3.5:9b | 1,568 | 284 | 0.0000 | 6 | ❌ |
| rag-widget-escape | tokenwise | qwen3.5:9b | 2,813 | 307 | 0.0000 | 6 | ✅ |
| istakip-password-length | tokenwise | qwen3.5:9b | 5,235 | 768 | 0.0000 | 3 | ✅ |
| istakip-atomic-config | tokenwise | qwen3.5:9b | 4,879 | 637 | 0.0000 | 17 | ✅ |
| gateway-breaker-validate | tokenwise | qwen3.5:9b | 13,664 | 526 | 0.0000 | 4 | ✅ |
| gateway-office-is-open | tokenwise | qwen3.5:9b | 18,077 | 870 | 0.0000 | 12 | ❌ |
| gateway-negative-words | tokenwise | qwen3.5:9b | 14,596 | 921 | 0.0000 | 2 | ✅ |
| kelime-chunker-validate | tokenwise | qwen3.5:9b | 18,643 | 749 | 0.0000 | 2 | ✅ |
| kelime-wer | tokenwise | qwen3.5:9b | 18,590 | 807 | 0.0000 | 9 | ✅ |
| iot-plant-profile | tokenwise | qwen3.5:9b | 2,504 | 1,073 | 0.0000 | 1 | ✅ |
| staj-greedy-seed | tokenwise | qwen3.5:9b | 1,369 | 1,069 | 0.0000 | 8 | ✅ |
| su-server-traversal | tokenwise | qwen3.5:9b | 1,084 | 365 | 0.0000 | 6 | ✅ |
| kargo-delete-owner | tokenwise | qwen3.5:9b | 882 | 297 | 0.0000 | 4 | – |
| tuglang-else | tokenwise | qwen3.5:9b | 1,388 | 271 | 0.0000 | 2 | – |
| mobil-health | tokenwise | qwen3.5:9b | 1,510 | 355 | 0.0000 | 4 | ✅ |
| alien-bullet-hit | tokenwise | qwen3.5:9b | 931 | 1,180 | 0.0000 | 4 | ✅ |

## Failures

<details><summary>nlp-aux-vp · tokenwise: check failed</summary>

```diff
--- a/convert_to_bio.py
+++ b/convert_to_bio.py
@@ -30,7 +30,7 @@
 def get_chunk_tag(upos, deprel):
     if upos in ['NOUN', 'PROPN', 'PRON', 'DET', 'NUM']:
         return 'NP'
-    elif upos == 'VERB':
+    elif upos == 'VERB' or deprel == 'AUX':
         return 'VP'
     elif upos == 'ADV':
         return 'ADVP'
```

```
Traceback (most recent call last):
  File "%USERPROFILE%\tokenwise\benchmark\github\checks/nlp_aux_vp.py", line 7, in <module>
    assert m.get_chunk_tag("AUX", "aux") == "VP"
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError
```

</details>

<details><summary>rag-settings-env · tokenwise: check failed</summary>

```diff
--- a/config/settings.py
+++ b/config/settings.py
@@ -11,9 +11,9 @@
 CHROMA_DB_DIR = BASE_DIR / "chroma_db"
 
 # RAG Settings
-CHUNK_SIZE = 500
-CHUNK_OVERLAP = 100
-TOP_K_RESULTS = 4
+CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "500"))
+CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "100"))
+TOP_K_RESULTS = int(os.getenv("RAG_TOP_K_RESULTS", "4"))
 
 # API Keys
 GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
```

```
Traceback (most recent call last):
  File "%USERPROFILE%\tokenwise\benchmark\github\checks/rag_settings_env.py", line 9, in <module>
    assert (m.CHUNK_SIZE, m.CHUNK_OVERLAP, m.TOP_K_RESULTS) == (800, 50, 6)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError
```

</details>

<details><summary>gateway-office-is-open · tokenwise: check failed</summary>

```diff
--- a/src/sesli_asistan/gateway/core/schedule.py
+++ b/src/sesli_asistan/gateway/core/schedule.py
@@ -151,7 +151,11 @@
             close_at = self.dated[d] if d in self.dated else self.yearly[key]
             if close_at is None or minute >= close_at:
                 return HOLIDAY
-        if not self.weekly:
-            return OPEN
-        spans = self.weekly[now.weekday()]
-        return OPEN if any(s <= minute < e for s, e in spans) else AFTER_HOURS
+    if not self.weekly:
+        return OPEN
+    spans = self.weekly[now.weekday()]
+    return OPEN if any(s <= minute < e for s, e in spans) else AFTER_HOURS
+
+    def is_open(self, now: datetime) -> bool:
+        """Mesai açık mı? (tatil hari)."""
+        return self.status(now) == OPEN
```

```
Traceback (most recent call last):
  File "%USERPROFILE%\tokenwise\benchmark\github\checks/gateway_office_is_open.py", line 8, in <module>
    from sesli_asistan.gateway.core.schedule import OfficeHours
  File "%USERPROFILE%\AppData\Local\Temp\tokenwise-bench-XXXX\Tugbacevikk__sesli-asistan-gateway__bcdec4d6d97a\src\sesli_asistan\gateway\core\schedule.py", line 155
    return OPEN
    ^^^^^^^^^^^
SyntaxError: 'return' outside function
```

</details>
