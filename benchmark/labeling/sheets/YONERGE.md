# Gözden geçirme yönergesi

Bu çalışmada tokenwise adlı bir aracın iki iddiasını kontrol ediyorsun. Aracı geliştirenlerin ne düşündüğünü bilmeden, kendi kararını ver. Başka gözden geçirenlerle cevaplarını konuşma; iş bittikten sonra konuşulabilir.

Dosyaları Excel'de ya da Google Sheets'te aç. Her satırın **karar** sütununa şunlardan birini yaz:

| Yaz | Anlamı |
| --- | --- |
| `d` | doğru |
| `y` | yanlış |
| `?` | emin değilim (bilgi yetmiyor ya da karar veremedin) |

**not** sütunu isteğe bağlı. Kararını kısaca açıklamak istersen kullan.

## findings.csv (78 satır, ~1,5 saat)

Her satır, aracın açık kaynak bir projede bulduğu bir sorun. **bağlantı** seni GitHub'da tam o satıra götürür.

Soru: **Kod, `iddia` sütununda yazanı gerçekten taşıyor mu?**

- "Karmaşıklık 21", "uzun (85 satır)", "büyük dosya" gibi ölçümlerde: ölçüm kabaca doğruysa `d`. Saymana gerek yok; açıkça yanlışsa `y`.
- Güvenlik iddialarında ("eval kullanımı", "zayıf hash", "shell=True" gibi): o kullanım gerçekten bir risk taşıyorsa ya da gözden geçirilmeye değerse `d`. Açıkça zararsızsa `y`. Örneğin SHA1 parola için değil, yalnızca önbellek anahtarı için kullanılıyorsa `y`.
- Sorunun düzeltilmeye değer olup olmadığını **sorma**. Yalnızca iddianın doğru olup olmadığına bak.

## regressions.csv (30 satır, ~1 saat)

Araç, her satırdaki **commit**'in daha sonra bir hata düzeltmesi gerektirdiğini söylüyor. Gerekçesi şu: kısa süre sonra gelen **düzeltme** commit'i aynı koda dokunuyor.

Soru: **Düzeltme, bu commit'in getirdiği ya da bozduğu bir şeyi mi onarıyor?**

- İki bağlantıyı da aç ve değişikliklere bak.
- Düzeltme gerçekten bu commit'in bir hatasını ya da eksiğini gideriyorsa `d`.
- Yalnızca aynı dosyaya dokunuyor ama başka bir şeyi düzeltiyorsa `y`. Bu commit sadece bir bağımlılık güncellemesi ya da biçim değişikliğiyse ve düzeltmeyle ilgisi yoksa da `y`.

Bitince dosyayı adınla kaydet (ör. `ayse-findings.csv`) ve geri gönder. Teşekkürler!
