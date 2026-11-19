# Bağımsız doğrulama

README'deki ve [measurements.md](measurements.md)'deki sayıları şimdiye kadar yalnızca bu projeyi geliştirenler ölçtü. Bu belge, aynı sayıları **başkalarının** nasıl kontrol edeceğini anlatır. Üç ayrı soru var; her birinin cevabını farklı kişiler verir.

| Soru | Kim cevaplar | Araç | Süre |
| --- | --- | --- | --- |
| 1. Aynı kodda aynı sayılar çıkıyor mu? | Herkes, GitHub'ın makineleri | `benchmark/reproduce/run.py`, *reproduce measurements* iş akışı | ~5 dk, makine |
| 2. Bulgular gerçekten doğru mu? AUC'nin dayandığı "sorunlu commit" etiketi doğru mu? | En az 2 insan, birbirinden habersiz | `benchmark/labeling/` | Kişi başı 2–3 saat |
| 3. Agent, geliştirenlerin görmediği görevlerde ne kadar başarılı? | Görevi kendisi yazan bir dış kullanıcı | `tokenwise benchmark` | Kişi başı yarım gün |

## 1. Tekrar üretme

```bash
pip install -e ".[impact]" ruff==0.16.9 bandit==1.9.4
python benchmark/reproduce/run.py --check
```

- 12 açık kaynak depo, [repos.toml](../benchmark/reproduce/repos.toml)'daki sabit commit'lerden klonlanır.
- Her birinde kullanıcının çalıştıracağı komutlar çalışır: `tokenwise scan --tools ruff,bandit` ve `tokenwise calibrate`.
- `--check`, sonucu [expected.json](../benchmark/reproduce/expected.json) ile karşılaştırır. Bulgu sayısı %5'ten, herhangi bir AUC 0,02'den fazla oynarsa iş başarısız olur.
- Model çağrılmaz, API anahtarı gerekmez.

Aynı işi GitHub da çalıştırır: depoda **Actions → reproduce measurements → Run workflow**. İş her ayın 1'inde kendiliğinden ve bu klasörlere dokunan her PR'da da çalışır. Ham çıktılar ve gözden geçirme tabloları iş sayfasından indirilir. Fork eden herkes aynı işi kendi hesabında çalıştırabilir.

Bu adım ölçümün **tekrarlanabilir** olduğunu gösterir. Doğru olduğunu göstermez: kodu da ölçütü de biz yazdık. Doğruluğu 2. ve 3. adımlar sınar.

## 2. İnsanların etiketlemesi

Şu an yayımlanan yanlış alarm oranı (~%10), 47 bulguya yapay zekânın (Claude) koyduğu etiketlerden çıktı. AUC ise "sonraki 20 commit içinde aynı koda dokunan bir düzeltme geldi" varsayımına dayanıyor. İkisini de insanlar kontrol etmeli.

**Hazırlık:** Tablolar hazır, [`benchmark/labeling/sheets/`](../benchmark/labeling/sheets) klasöründe. Yeniden üretmek için:

```bash
python benchmark/reproduce/run.py
python benchmark/labeling/make_sheets.py .reproduce/results benchmark/labeling/sheets
```

İki dosya var; ikisi de Excel'de ya da Google Sheets'te açılır:

- **`findings.csv`:** Her kuraldan en fazla 12 bulgu, toplam 78. Her satırda bulgunun iddiası ve GitHub'da tam o satıra giden bir bağlantı var.
- **`regressions.csv`:** "Sorunlu" etiketi almış 30 commit. Her satırda o commit'e ve onu sorunlu yapan düzeltmeye giden bağlantılar var.

Örnekler sabit bir tohumla seçilir; herkes aynı satırları alır. `.key.json` dosyaları puanlama için satırların kuralını tutar. Gözden geçirenlere yalnızca `.csv` dosyalarını gönder.

**Gözden geçirenler:** Python ya da JavaScript okuyabilen, projeyle ilgisi olmayan en az iki kişi. Örneğin bölümden iki arkadaş ya da staj yerinden iki geliştirici. Birbirlerinin cevaplarını görmemeliler. Her satırın `karar` sütununa şunlardan birini yazarlar:

- `d`: doğru. Bulgu gerçek ya da düzeltme gerçekten bu commit'in bozduğu bir şeyi onarıyor.
- `y`: yanlış. Yanlış alarm ya da düzeltmenin bu commit'le ilgisi yok.
- `?`: emin değilim.

Gözden geçirenlere verilecek kısa yönerge:

> **Bulgular:** "Bu kod parçası iddia edilen sorunu gerçekten taşıyor mu?" sorusunu cevapla. Sorunun düzeltilmeye değer olup olmadığı ayrı bir soru; burada sorulmuyor. Örneğin karmaşıklığı 21 denen bir fonksiyon gerçekten o kadar dallanıyorsa cevap `d`.
>
> **Commit'ler:** İki bağlantıyı da aç. "Düzeltme, bu commit'in getirdiği ya da bozduğu bir şeyi mi onarıyor?" Yalnızca aynı dosyaya dokunuyorsa, ama başka bir şeyi düzeltiyorsa cevap `y`.

**Sonuç:**

```bash
python benchmark/labeling/score.py benchmark/labeling/sheets ayse/findings.csv mehmet/findings.csv
python benchmark/labeling/score.py benchmark/labeling/sheets ayse/regressions.csv mehmet/regressions.csv
```

Her gözden geçiren için şunlar çıkar:
- yanlış alarm oranı ya da yanlış etiket oranı, %95 güven aralığıyla;
- bulgularda kural başına döküm;
- iki gözden geçirenin şans dışı uyumu (Cohen's kappa).

Kappa 0,4'ün altındaysa iki kişi "doğru"yu farklı anlıyor demektir. O durumda önce ayrıştıkları satırları birlikte konuşun, sonra sayıya güvenin.

Bu iki sayı (yanlış alarm oranı ve etiket doğruluğu), aralıklarıyla birlikte README'ye yazılır. Kişiler kabul ederse adlarıyla, etmezse "iki dış gözden geçiren" diye.

## 3. Görmediğimiz görevler

Agent'ın 15/18 sonucu, araç o görevlerde görülen hatalara göre düzeltildikten sonra alındı. Gerçek başarıyı ancak geliştirenlerin hiç görmediği görevler gösterir.

- **Görevler:** Dış bir kullanıcı kendi projesinden 10–20 görev yazar. Her görevin bir kontrol betiği olur: değişiklikten önce başarısız, doğru değişiklikten sonra başarılı. Biçim README'deki "Karşılaştırma testi" bölümünde.
- **Kilitleme:** Görevler çalıştırılmadan önce bir commit'e kilitlenir. Commit'in kimliği (SHA) README'ye yazılır. Böylece görevler sonuca bakıp değiştirilemez.
- **Çalıştırma:** O kişi kendi API anahtarıyla `tokenwise benchmark <görevler.toml>` komutunu bir kez çalıştırır ve `results.md` dosyasını olduğu gibi paylaşır.
- **Raporlama:** Başarısız görevler de raporda kalır. Bu görevlere göre araçta yapılan bir düzeltme, sonraki ölçümün "görülmemiş görev" sayılmasını bozar. O görevler bundan sonra "görülmüş" diye ayrı raporlanır.

## Durum

| Adım | Durum |
| --- | --- |
| 1. Tekrar üretme | Hazır. 19 Kasım 2026'da 12 depoda belgedeki sayılar çıktı: bulgu sayıları birebir aynı, AUC değerleri ±0,01 içinde, ortalama 0,706 |
| 2. İnsan etiketleri | Tablolar hazır, gözden geçiren bekleniyor |
| 3. Görülmemiş görevler | Gönüllü kullanıcı bekleniyor |
