# Faz 1 ölçümü: 10 gerçek depo

Ölçüm tarihi 17 Kasım 2026. RepoDoctor ve ReleaseGuard 10 açık kaynak depoda çalıştırıldı.

- Bulgu sayısı 1.537'den 394'e indi. Gürültünün kaynakları bulunup düzeltildi.
- Kalan bulguların incelenen örneklerinde yanlış alarm oranı %10 civarında. Bu oran, güvenlik kurallarında daha yüksek.
- Risk skorunun ortalama AUC'si 0,68.
- Varsayılan risk ağırlıkları değiştirilmedi. Depolardan öğrenilen ağırlıklar, ölçüme katılmayan depoda ancak +0,004 kazanç getirdi.

## Depolar

| Depo | Commit | Geçmiş (commit) | Kaynak dosya | Bulgu (önce → sonra) | Sağlık (önce → sonra) |
| --- | --- | ---: | ---: | --- | --- |
| psf/requests | 3c7baaa0 | 6.496 | 37 | 30 → 24 | 80 → 85 |
| pallets/flask | 49943b5a | 5.566 | 83 | 52 → 29 | 83 → 89 |
| pallets/click | 08b803c | 3.416 | 91 | 115 → 53 | 76 → 84 |
| encode/httpx | 7cff5f7 | 1.527 | 60 | 65 → 15 | 75 → 90 |
| Textualize/rich | 3a109ccd | 4.470 | 212 | 480 → 101 | 67 → 84 |
| fastapi/fastapi | a1b3be01a | 8.125 | 1.225 | 427 → 75 | 93 → 97 |
| fastapi/typer | 695d845 | 1.822 | 645 | 236 → 63 | 91 → 96 |
| python-attrs/attrs | 2a1fb86 | 1.852 | 56 | 46 → 28 | 79 → 84 |
| expressjs/express | 43fca52d | 6.186 | 142 | 37 → 2 | 96 → 100 |
| axios/axios | c0d4c900 | 2.284 | 260 | 49 → 4 | 97 → 100 |

- Tarama `tokenwise scan --tools ruff,bandit` komutuyla yapıldı. On depo aynı anda tarandı ve her biri 1 ile 14 saniye sürdü.
- "Önce" sütunu bu ölçümden önceki sürümü (c33ee2d), "sonra" sütunu bu dalı gösterir.

## Gürültü kaynakları ve düzeltmeler

| Kaynak | Örnek | Düzeltme | Etkisi |
| --- | --- | --- | --- |
| Belge ve örnek kodu ürün kodu sayılıyordu | `docs_src/` altındaki 94 FastAPI eğitim dosyası "test yok" uyarısı aldı | `docs`, `docs_src`, `examples`, `scripts`, `tools`, `benchmarks`, `sandbox` klasörleri ve `*.config.js`, `gulpfile`, `.d.ts` gibi dosyalar destek kodu sayılıyor. Bunlara test, stil ve döngü kuralı uygulanmıyor; güvenlik kuralları uygulanmaya devam ediyor. | "Test yok" uyarıları 407'den 2'ye indi (aşağıdaki iki düzeltmeyle birlikte) |
| Paketin kendisi üzerinden yapılan testler görülmüyordu | `import httpx` yazan testler `httpx/_api.py` dosyasını kapsamıyor sayılıyordu | Bir dosya şu durumlarda test edilmiş sayılıyor: bir test onu doğrudan içe aktarıyorsa, `__init__.py` ya da `index.js` gibi bir giriş dosyası üzerinden ulaşıyorsa ya da arada en fazla bir ara modül varsa | |
| Davranış içermeyen modüller | `rich/_unicode_data/` altındaki üretilmiş tablolar | Fonksiyon ya da sınıf içermeyen dosyalara "test yok" ve "büyük dosya" uyarısı verilmiyor | "Büyük dosya" uyarıları 134'ten 54'e indi |
| Bandit'in düşük önemli kuralları | `assert` kullanımı (228), `import subprocess` (27), tahmine dayalı `shell=True` (10) | Yalnızca orta ve üstü önem ve güven seviyesindeki Bandit bulguları alınıyor (`-ll -ii`) | Bandit bulguları yaklaşık 430'dan 7'ye indi |
| Projenin kendisi seçmediği Ruff kuralları | Ruff ayarı olmayan rich deposunda 117 stil uyarısı çıktı | Ruff ayarı olmayan projelerde yalnızca gerçek hata gösteren kurallar çalışıyor (`E9, F63, F7, F82, F811, F841`, bazı `B` kuralları). Test dosyalarında Ruff çalıştırılmıyor. | Ruff bulguları 117'den 12'ye indi |
| Aynı sorunun iki kez sayılması | `eval` için hem yerleşik kural hem Bandit B307 bulgu üretiyordu | Bandit çalıştığında aynı satırdaki yerleşik bulgu düşürülüyor | |
| Sahte import döngüleri | Flask'ta `import json` komutu `flask/json/` paketine bağlanıyordu; rich'te 53 dosyalık bir döngü çıktı | Standart kütüphane adları yalnızca projenin en üst düzeyindeki bir modüle eşleniyor. Fonksiyon içindeki importlar, `if TYPE_CHECKING:` ve `if __name__ == "__main__":` blokları döngü hesabına katılmıyor. `from pkg import modül` artık paketin `__init__.py` dosyasına değil, doğrudan modüle bağlanıyor. | Döngü bulguları 13'ten 4'e indi |
| Çökme | Express'te `require('..')` satırı taramayı durduruyordu | Bu satır paketin `index.js` dosyasına bağlanıyor | |

## Kalan 394 bulgu: elle inceleme

Bu etiketleri bir kişi değil, ben (Claude) koydum. Her bulguda kodu açıp okudum. Beta kullanıcıları ignore/resolve kararı verdikçe asıl oranı `tokenwise findings --stats` gösterecek.

| Kural | Adet | İncelenen | Doğru | Yanlış alarm | Not |
| --- | ---: | ---: | ---: | ---: | --- |
| `high-complexity` | 179 | 6 | 6 | 0 | Ölçüm doğru (örnek: attrs `_attrs_to_init_script` 46). Düzeltmeye değer olup olmadığı bağlama göre değişir. |
| `long-function` | 128 | 4 | 4 | 0 | Ölçüm doğru |
| `large-file` | 54 | 4 | 4 | 0 | Ölçüm doğru |
| `ruff:*` | 12 | 12 | 10 | 2 | Yanlış alarmların ikisi de `get_ipython`, IPython'un çalışma anında tanımladığı bir ad. `B023` (döngü değişkeni yakalama) olası gerçek bir hata. |
| `bandit:*` ve `shell-command` | 8 | 8 | 5 | 3 | Doğru tespitler tasarım gereği: Flask `config.from_pyfile` içinde `exec`, attrs kod üretiminde `eval`. Yanlış alarmlar: imza ve protokol için kullanılan SHA1 (Flask, httpx) ve rich'teki iç `pickle` kullanımı. |
| `todo-density` | 7 | 7 | 7 | 0 | |
| `import-cycle` | 4 | 4 | 4 | 0 | Hepsi gerçek ve yükleme anında oluşuyor (örnek: FastAPI'de `_compat/v2.py` paket `__init__`'inden ad alıyor). Python sıralama sayesinde tolere ediyor. |
| `missing-tests` | 2 | 2 | 2 | 0 | rich `diagnose.py`, axios `deprecatedMethod.js` |
| **Toplam** | **394** | **47** | **42** | **5** | İncelenen örnekte yanlış alarm oranı %10,6 |

- Sayı ölçen kurallarda (karmaşıklık, uzunluk, boyut) yanlış alarm görülmedi.
- Yanlış alarmlar desen eşleyen güvenlik kurallarında toplanıyor. Bu kuralların bulguları panelde "gözden geçir" niteliğinde; merge'ü engellemiyor.

## ReleaseGuard kalibrasyonu

Her depoda son 200 uygun commit, PR gibi puanlandı. Sonraki 20 commit içinde aynı koda dokunan bir hata düzeltmesi geldiyse o commit "sorunlu" sayıldı.

| Depo | Sorunlu / 200 | Etki | Geçmiş hata | Test eksikliği | Kritiklik | Karmaşıklık | Büyüklük | **Skor** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| requests | 52 | 0,67 | 0,62 | 0,48 | 0,75 | 0,78 | 0,57 | **0,77** |
| flask | 41 | 0,57 | 0,69 | 0,46 | 0,75 | 0,67 | 0,59 | **0,75** |
| fastapi | 33 | 0,53 | 0,80 | 0,53 | 0,28 | 0,77 | 0,81 | **0,74** |
| click | 82 | 0,56 | 0,58 | 0,49 | 0,68 | 0,66 | 0,62 | **0,72** |
| httpx | 41 | 0,56 | 0,66 | 0,51 | 0,66 | 0,71 | 0,62 | **0,71** |
| axios | 100 | 0,68 | 0,69 | 0,53 | 0,47 | 0,50 | 0,56 | **0,69** |
| express | 43 | 0,65 | 0,66 | 0,46 | 0,60 | 0,50 | 0,61 | **0,67** |
| attrs | 45 | 0,54 | 0,54 | 0,49 | 0,62 | 0,65 | 0,53 | **0,63** |
| typer | 18 | 0,53 | 0,55 | 0,50 | 0,49 | 0,65 | 0,51 | **0,59** |
| rich | 77 | 0,55 | 0,55 | 0,49 | 0,50 | 0,59 | 0,56 | **0,53** |
| **Ortalama** | | 0,58 | 0,63 | 0,49 | 0,58 | 0,65 | 0,60 | **0,68** |

AUC 0,5 yazı-tura demektir, 1 ise mükemmel ayırmadır.

- **Test eksikliği sinyalinin tahmin gücü yok (0,49).** Eskiden bu sinyalin içinde "bu PR hiç test değiştirmiyor" bileşeni vardı ve ters çalışıyordu (0,36 ile 0,45 arası). Test değiştiren commit'ler davranış değiştiren commit'lerdi; bu yüzden sonradan düzeltme ihtiyacı onlarda daha sıktı. Bu bileşen artık puana katılmıyor, yalnızca kanıt olarak gösteriliyor.
- **JS/TS depolarında karmaşıklık sinyali 0,50.** Karmaşıklık şimdilik yalnızca Python için ölçülüyor.
- **FastAPI'de kritiklik sinyali ters (0,28).** Çok dosyanın içe aktardığı çekirdek dosyalar burada daha dikkatli değiştiriliyor.
- **Varsayılan ağırlıklar değiştirilmedi.** Her depo için ağırlıklar diğer 9 depodan öğrenilip o depoda denendi (leave-one-out). Ortalama AUC 0,682'den yalnızca 0,686'ya çıktı ve 10 depodan 6'sında iyileşme oldu. Bu fark, varsayılanları değiştirmeye yetmiyor. Bir proje kendi geçmişine göre `tokenwise calibrate --save` ile ayar yapabilir.

## JS/TS ekleri: karmaşıklık ve paket adıyla import

İlk ölçümde JS/TS depolarında karmaşıklık sinyali yazı-tura düzeyindeydi (0,50), çünkü karmaşıklık yalnızca Python için hesaplanıyordu. Bu bölümdeki ölçümler, iki eklemeden sonra iki TypeScript deposu daha katılarak yapıldı:

- **JS/TS fonksiyonlarında karmaşıklık ve uzunluk:** Tree-sitter ile hesaplanıyor. Python'daki kuralların aynısı uygulanıyor; dallar, `&&`, `||` ve `??` sayılıyor. İsimli iç fonksiyonlar ayrı birim sayılıyor; isimsiz geri çağırmalar (callback) içinde bulundukları fonksiyona ekleniyor.
- **Paket adıyla yapılan importlar:** `import { z } from "zod/v4"` gibi importlar, `package.json` dosyasındaki `name` ve `exports` alanlarından çözülerek projenin kendi kaynak dosyasına bağlanıyor. Zod'da `v3/types.ts` dosyası artık "test yok" uyarısı almıyor.
- **Testten sayılan dosyalar:** `fixtures`, `__mocks__` klasörleri ve `test-*.ts` adlı dosyalar artık test sayılıyor.

| Depo | Commit | Kaynak dosya | Bulgu | Sorunlu / 200 | Karmaşıklık AUC (önce → sonra) | Skor AUC (önce → sonra) |
| --- | --- | ---: | ---: | ---: | --- | --- |
| colinhacks/zod | 90bf57b8 | 514 | 233 | 103 | 0,50 → 0,77 | 0,80 → 0,81 |
| sindresorhus/got | 6b5aafc | 98 | 61 | 166 | 0,50 → 0,83 | 0,87 → 0,89 |
| axios/axios | c0d4c900 | 260 | 44 | 100 | 0,50 → 0,64 | 0,69 → 0,66 |
| expressjs/express | 43fca52d | 142 | 3 | 43 | 0,50 → 0,54 | 0,67 → 0,67 |

- **12 deponun ortalaması:** risk skoru AUC 0,71, karmaşıklık 0,69, test eksikliği 0,47.
- **Ağırlık denemesi:** Leave-one-out yöntemiyle öğrenilen ağırlıklar 0,708'i 0,715'e çıkardı ve 12 depodan 8'inde iyileşme oldu. Bu küçük fark nedeniyle varsayılanlar yine değiştirilmedi.
- **axios'ta skorun düşmesi:** Skor 0,69'dan 0,66'ya indi. Paket adıyla çözülen importlar test eksikliği sinyalini 0,53'ten 0,43'e indirdi. Bu sinyal 12 deponun hiçbirinde işe yaramıyor (bkz. yukarıdaki not).
- **got'ta etiket doygunluğu:** got'ta 200 commit'in 166'sı "sorunlu" çıktı. Bu depoda düzeltme commit'leri çok sık. Bu kadar yüksek bir sorunlu oranında AUC'ye temkinli bakılmalı.
- **Elle doğrulama:** axios'taki `assertParamsAcyclic` fonksiyonunun 16 olan karmaşıklık değeri satır satır sayılarak doğrulandı. Zod'da kalan 7 "test yok" bulgusunun 5'i, testlere ancak üç ya da daha fazla ara modül üzerinden ulaşan dosyalar.

## Tekrar üretmek

Bütün tabloyu tek komutla tekrar üretmek ve insanların kontrol etmesi için: [verification.md](verification.md).

```bash
git clone https://github.com/pallets/flask && cd flask
tokenwise scan --tools ruff,bandit --json
tokenwise calibrate --json
```
