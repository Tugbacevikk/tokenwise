# GitHub projelerinde tahmini token ve maliyet

Model çağrılmadan, tokenwise'ın kendi koduyla ve her görevin referans çözümüyle hesaplandı (bkz. `estimate.py`). Model ilk denemede doğru dosyayı seçip en küçük doğru değişikliği yazarsa iki yöntemin ne gönderip ne alacağını gösterir. Token = karakter / 4 (iki yöntem için aynı). Fiyatlar: Claude Haiku/Sonnet/Opus 5.5 liste fiyatları, önbelleksiz.

| Görev | Proje (token) | Baseline giriş | tokenwise giriş | Baseline çıkış | tokenwise çıkış | Baseline $ | tokenwise $ | tokenwise modeli | Kontrol |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | :---: |
| netprobe-chunk-size | 10,731 | 10,759 | 1,948 | 1,075 | 104 | 0.0323 | 0.0038 | sonnet (medium) | ✅ |
| netprobe-logger-with | 10,731 | 10,771 | 2,421 | 1,529 | 84 | 0.0368 | 0.0003 | haiku (easy) | ✅ |
| nlp-aux-vp | 7,849 | 7,774 | 1,504 | 722 | 34 | 0.0228 | 0.0024 | sonnet (medium) | ✅ |
| nlp-predict-main | 7,849 | 7,783 | 1,367 | 572 | 96 | 0.0213 | 0.0002 | haiku (easy) | ✅ |
| rag-settings-env | 17,421 | 17,398 | 1,430 | 185 | 73 | 0.0366 | 0.0020 | sonnet (medium) | ✅ |
| rag-widget-escape | 17,421 | 17,365 | 2,552 | 1,382 | 79 | 0.0486 | 0.0043 | sonnet (medium) | ✅ |
| istakip-password-length | 54,012 | 53,799 | 3,147 | 22,930 | 135 | 0.3369 | 0.0046 | sonnet (medium) | ✅ |
| istakip-atomic-config | 54,012 | 53,810 | 2,814 | 22,920 | 103 | 0.3368 | 0.0036 | sonnet (medium) | ✅ |
| gateway-breaker-validate | 371,327 | 365,910 ⚠ | 11,694 | 1,576 | 107 | 0.7476 | 0.0092 | sonnet (medium) | ✅ |
| gateway-office-is-open | 371,327 | 365,904 ⚠ | 11,929 | 1,816 | 75 | 0.7500 | 0.0094 | sonnet (medium) | ✅ |
| gateway-negative-words | 371,327 | 365,904 ⚠ | 11,253 | 6,349 | 75 | 0.7953 | 0.0080 | sonnet (medium) | ✅ |
| kelime-chunker-validate | 227,401 | 225,362 ⚠ | 15,691 | 848 | 106 | 0.4592 | 0.0071 | sonnet (medium) | ✅ |
| kelime-wer | 227,401 | 225,377 ⚠ | 15,683 | 852 | 105 | 0.4593 | 0.0071 | sonnet (medium) | ✅ |
| iot-plant-profile | 8,979 | 7,975 | 1,740 | 2,711 | 72 | 0.0431 | 0.0033 | sonnet (medium) | ✅ |
| staj-greedy-seed | 8,709 | 8,448 | 1,134 | 431 | 157 | 0.0212 | 0.0030 | sonnet (medium) | ✅ |
| su-server-traversal | 4,513 | 4,569 | 988 | 456 | 91 | 0.0137 | 0.0023 | sonnet (medium) | ✅ |
| kargo-delete-owner | 6,328 | 6,275 | 742 | 211 | 83 | 0.0147 | 0.0019 | sonnet (medium) | ✅ |
| tuglang-else | 2,929 | 2,927 | 1,336 | 731 | 59 | 0.0132 | 0.0026 | sonnet (medium) | ✅ |
| mobil-health | 2,041 | 2,159 | 1,354 | 783 | 99 | 0.0121 | 0.0031 | sonnet (medium) | ✅ |
| alien-bullet-hit | 2,845 | 2,844 | 830 | 2,758 | 166 | 0.0333 | 0.0027 | sonnet (medium) | ✅ |

| Toplam (20 görev) | Baseline | tokenwise | Değişim |
| --- | ---: | ---: | ---: |
| Giriş tokeni | 1,763,113 | 91,557 | -95% |
| Çıkış tokeni | 70,837 | 1,903 | -97% |
| Maliyet ($) | 4.23 | 0.08 | -98% |

⚠ = baseline girişi 200 bin tokeni aşıyor; bu boyutta tüm projeyi göndermek pratikte mümkün olmayabilir (model bağlam sınırı ve fiyat kademesi).

✅ = kontrol orijinal kodda başarısız, iki yöntemin değişikliğinden sonra başarılı.
