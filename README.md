# tokenwise

**Yapay zekâya kod yazdırırken en az token ve en az kodla sonuç almak için komut satırı aracı.**
*A frugal AI coding assistant: sends less context, writes less code.*

Modele bütün projeyi göndermek ve bütün dosyayı yeniden yazdırmak yerine tokenwise:

1. Projenin **haritasını** çıkarır: dosya yolları ve fonksiyon/sınıf imzaları, gövdeler olmadan.
2. **En ucuz model** haritaya bakıp işin hangi dosyalara ihtiyaç duyduğunu seçer. İstekte adı geçen fonksiyon ve sınıfların dosyaları da modele sorulmadan, metin aramasıyla eklenir.
3. **Sadece o dosyalar** düzenleyici modele gönderilir; büyük dosyalardan da sadece ilgili fonksiyonlar.
4. **Yönlendirici** işin zorluğunu, değişecek koda bakarak tahmin eder (model çağrısı yok) ve model merdiveninin uygun basamağından başlar; model beceremezse bir üst modele geçer. Ayrıntılar "Model seçimi" bölümünde.
5. Model dosyayı baştan yazmaz, sadece **değişen satırları** döndürür.
6. Değişiklik renkli fark (diff) olarak gösterilir, onay verirsen uygulanır, istersen geri alınır.
7. Birebir aynı istek tekrar gelirse cevap **önbellekten** döner, model çağrılmaz.
8. Her adımda harcanan token tablo olarak yazdırılır.

Claude API'de sabit kısımlar (talimat + harita) *prompt caching* ile işaretlenir; aynı oturumda tekrar okunduklarında çok daha ucuza gelir.

## Kurulum

Python 3.11 veya üzeri gerekir. Haritada Python, JavaScript/TypeScript, Java, Kotlin, C#, Go, Rust ve PHP desteklenir. PyPI'de paket adı `tokenwise-coder`, komut adı `tokenwise`:

```powershell
pipx install tokenwise-coder     # ya da: pip install tokenwise-coder
```

Kaynaktan (geliştirme için):

```powershell
git clone https://github.com/Tugbacevikk/tokenwise
cd tokenwise
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
```

## Kullanım

Çalışmak istediğin projenin klasöründe:

```powershell
tokenwise init                 # tokenwise.toml ve .env oluşturur, API anahtarını sorar
tokenwise init --local         # API yerine yerel model (Ollama) için ayar dosyası
tokenwise doctor               # ayar, model sunucusu ve modeller hazır mı
tokenwise map                  # proje haritası ve "tüm kaynak vs harita" token karşılaştırması
tokenwise ask "login fonksiyonuna şifre uzunluk kontrolü ekle"
tokenwise ask "..." --dry-run  # sadece farkı göster, dosyaya yazma
tokenwise ask "..." -y         # onay sormadan uygula
tokenwise ask "..." -m claude-opus-5-5   # tek bir modeli zorla, merdiven yok
tokenwise ask "..." --no-cache # önbelleğe bakma
tokenwise ask "..." --check "pytest -q"   # önce testler geçsin (ayardaki check'i ezer)
tokenwise ask "..." --no-check # ayarlı check'i bu sefer atla
tokenwise ask "..." --over-budget         # haftalık bütçe dolsa da çalıştır
tokenwise undo                 # son uygulanan değişikliği geri al
tokenwise cache                # önbellekte kaç cevap var; --clear ile temizle
tokenwise report               # son 7 gün ve tüm zaman: istek, token, maliyet, model dağılımı
tokenwise report --csv runs.csv  # her isteği CSV'ye aktar
tokenwise ask "..." --json     # sonucu JSON olarak ver (VS Code eklentisi bunu kullanır)
tokenwise apply                # --json / --dry-run ile bekleyen son öneriyi uygula
```

Başka bir klasör için her komuta `--root <klasör>` verebilirsin.

### Yerel modelle (API anahtarsız) kullanım

[Ollama](https://ollama.com) kurulu olsun, bir kodlama modeli indir ve projende `--local` ile başla:

```powershell
ollama pull qwen2.5-coder:7b   # 4 GB VRAM'e tam sığmaz, yavaş olur; daha hızlısı: qwen2.5-coder:3b
cd <projen>
tokenwise init --local         # provider = "ollama", models = ["qwen2.5-coder:7b"]
tokenwise doctor               # sunucu çalışıyor mu, ayardaki model indirilmiş mi?
tokenwise ask "..." --dry-run
```

3b indirdiysen `tokenwise.toml`'daki `models` satırını `["qwen2.5-coder:3b"]` yap; `tokenwise doctor` eksik modeli söyler.

tokenwise Ollama'nın kendi API'sini kullanır ve her istek için bağlam penceresini (`num_ctx`) istemin boyuna göre ayarlar. Ollama'nın OpenAI uyumlu adresi varsayılan pencereyi (birkaç bin token) kullanır ve uzun istemleri **sessizce keser**; büyük bir projenin haritası o zaman modele yarım gider. Pencerenin üst sınırı `max_context` ayarıdır (varsayılan 32768). Daha büyüğü daha çok ekran kartı belleği ister.

### OpenRouter ile kullanım (tek anahtar, birçok şirketin modeli)

[OpenRouter](https://openrouter.ai) DeepSeek, Qwen, Mistral, Llama, Claude ve başka birçok şirketin modelini tek bir API anahtarıyla sunar. Anahtarı [openrouter.ai/keys](https://openrouter.ai/keys) sayfasından al:

```powershell
cd <projen>
tokenwise init --openrouter    # anahtarı sorar, .env'ye OPENROUTER_API_KEY olarak yazar
tokenwise doctor               # anahtar geçerli mi, ayardaki modeller OpenRouter'da var mı?
tokenwise ask "..." --dry-run
```

- **Başlangıç merdiveni (ucuzdan pahalıya):**
  - `~deepseek/deepseek-flash-latest` (seçici ve kolay işler)
  - `~deepseek/deepseek-pro-latest`
  - `anthropic/claude-sonnet-5.5`

  Adı `~` ile başlayan modeller, DeepSeek'in her zaman en yeni sürümünü gösterir. Başka bir model kullanmak için `models` satırına [openrouter.ai/models](https://openrouter.ai/models) sayfasındaki adını yaz.
- **Fiyatlar:** OpenRouter'ın model listesinden günde bir kez okunur. Böylece maliyet kaydı, haftalık bütçe ve `max_usd_per_request` her model için doğru çalışır. `prices` ayarına yazdığın fiyat önceliklidir. Fiyatlar okunamazsa `tokenwise doctor` bunu söyler.
- **Barındırılan sürüm:** Organization sahibi panelde *Model ayarları → Sağlayıcı → OpenRouter* seçip kendi anahtarını girer.

LM Studio, vLLM ya da OpenAI'nin kendisi gibi OpenAI uyumlu sunucular için `provider = "openai"` ve `base_url = "http://localhost:1234/v1"` gibi bir adres yaz. Anahtar gerekiyorsa `OPENAI_API_KEY` ortam değişkenini kullan.

### Ayarlar

`tokenwise init` şu dosyayı oluşturur; istediğin gibi değiştir:

```toml
[tokenwise]
provider = "anthropic"          # ya da "ollama" (yerel), "openai" (LM Studio, vLLM...)
# base_url = "http://localhost:11434"
models = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]   # ucuzdan güçlüye
extensions = [".py", ".js", ".jsx", ".ts", ".tsx"]
max_files = 8
```

`models` bir merdivendir: dosya seçici hep ilkini kullanır; düzenleyici, yönlendiricinin seçtiği basamaktan başlar (kolay → 1., orta → 2., zor → 3.) ve her basamakta 2 deneme yapıp bir üste çıkar. Tek model yazarsan merdiven yoktur, sadece tekrar dener. `TOKENWISE_PROVIDER`, `TOKENWISE_BASE_URL` ve `TOKENWISE_MODELS` (virgülle ayrılmış) ortam değişkenleri ayar dosyasını ezer. API anahtarı sadece `.env` içinde durur ve `init` onu `.gitignore`'a ekler.

### Büyük dosyalar

`focus_lines` (varsayılan 300) satırdan uzun dosyalar haritada `[large]` diye işaretlenir. Seçici bu dosyalar için `dosya::fonksiyon` ya da `dosya::Sınıf.metot` biçiminde hedef verebilir; düzenleyiciye o zaman dosyanın tamamı yerine **dosyanın başı (import'lar), istenen fonksiyonlar ve dosyadaki diğer tanımların listesi** gider. Atlanan yerler `⋯ N lines omitted` satırıyla gösterilir. Gösterilen kısımlar birebir kopyalandığı için modelin değişikliği asıl dosyaya yine tam uyar. Model daha fazlasını görmek isterse `NEED_FILES` ile dosyanın tamamını ister ve tamamı gönderilir; sembol bulunamazsa ya da kazanç azsa da dosya bütün gider.

Bu reponun kendi dosyalarında tek fonksiyon için ölçüm:

| Dosya | Tüm dosya | Odaklı |
| --- | ---: | ---: |
| `tokenwise/cli.py` (450 satır), `undo` | ≈4.300 token | ≈1.100 token |
| `tokenwise/editor.py` (377 satır), `plan_changes` | ≈3.450 token | ≈1.000 token |
| `vscode-extension/src/extension.ts` (290 satır), `ReportProvider` | ≈2.500 token | ≈600 token |

### Otomatik test kontrolü

`tokenwise.toml`'a bir kontrol komutu yazarsan her değişiklik sana gösterilmeden önce projenin **geçici bir kopyasında** denenir:

```toml
[tokenwise]
check = "{python} -m pytest -q -x"     # {python} = tokenwise'ı çalıştıran Python
```

Kontrol geçmezse komutun çıktısı (son 3000 karakter) modele geri gönderilir ve düzeltmesi istenir; o model iki denemede beceremezse merdivende bir üst modele geçilir. Hiçbiri geçemezse son değişiklik ve hata çıktısı gösterilir, dosyaya yazılmaz; yine de istersen `tokenwise apply` ile uygulayabilirsin. Asıl proje kontrol sırasında hiç değişmez.

### Otomatik korumalar

Her değişiklik, sana gösterilmeden önce test gerekmeden yakalanabilen hatalara karşı denetlenir. Sorun bulunursa model düzeltmesi için tekrar çalıştırılır:

- Python dosyası geçersiz hâle geldi mi (sözdizimi ya da girinti hatası)?
- Silinen bir fonksiyon, sınıf ya da değişken aynı dosyada hâlâ kullanılıyor mu?
- Yeniden adlandırılan ya da silinen bir ad başka bir modülde hâlâ kullanılıyor mu?

Bu üç kontrol, küçük bir yerel modelle (`qwen2.5-coder:3b`) yapılan ilk gerçek denemede görülen hatalardan çıkarıldı.

### Haftalık bütçe

```toml
[tokenwise]
budget_usd_week = 2.0
```

Son 7 günün harcaması bu tutara ulaşınca `tokenwise ask` modeli hiç çağırmadan durur (`--over-budget` ile aşılabilir). Her istekten sonra harcanan/limit gösterilir, %80'i geçince sarıya döner; `tokenwise report` ve VS Code paneli de bütçe durumunu gösterir.

### İstek başına bütçe

```toml
[tokenwise]
max_usd_per_request = 0.10
```

Haftalık bütçe yeni istekleri durdurur, ama tek bir isteğin daha güçlü modellere geçerken ne kadar harcayacağını sınırlamaz. Bu ayar sınırlar:
- Her model çağrısından önce girdinin maliyeti tahmin edilir. Tahmin bilerek kötümser tutulur: yaklaşık 4 karakter = 1 token, üstüne %20 pay ve önbelleksiz fiyat.
- Modelin cevabının uzunluğu (`max_tokens`), kalan payın karşılayabileceği kadarla sınırlanır.
- Kısa bir cevap için bile pay kalmamışsa çağrı hiç yapılmaz. O ana kadar en iyi öneri varsa o gösterilir.
- Fiyatı bilinmeyen modeller (yerel modeller) bu sınıra girmez.

Barındırılan sürümde her AI düzeltmesinin varsayılan sınırı $0,50'dir (`TOKENWISE_MAX_USD_PER_FIX`). Depodaki `tokenwise.toml` bu sınırı yükseltemez.

### Model seçimi (yönlendirici)

İstek hangi modelden başlayacak? Yönlendirici bunu hiç model çağırmadan, şu sinyallerden çıkarır:

| Sinyal | Etkisi |
| --- | --- |
| İsteğin ifadesi | "yazım hatası", "yeniden adlandır" bir basamak aşağı; "refactor", "performans", "eşzamanlılık" bir basamak yukarı |
| Değişecek kodun boyutu | Modelin göreceği kod 600 satırı ya da 4 dosyayı aşarsa yukarı |
| Hedef kodun karmaşıklığı | Değişecek fonksiyonun karmaşıklığı 15 ve üstündeyse yukarı (Python, JS/TS) |
| Koda bağlı dosyalar | Değişecek dosyayı 5 ya da daha fazla dosya içe aktarıyorsa yukarı: hata yayılır |
| Bu projenin geçmişi | Son 30 günde aynı basamaktan başlayan isteklerin en az yarısı daha güçlü modele geçmek zorunda kaldıysa (en az 5 istek), baştan bir basamak yukarıdan başlar ve boşa giden denemeler atlanır |

Hiçbir sinyal yoksa orta basamaktan başlanır. Komut satırı da web sayfası da kararın nedenini yazar, örneğin "Why: complexity 17 (+1)" ya da "Neden bu model: hedef kodun karmaşıklığı 17 (+1)".

`router = "simple"` yalnızca isteğin ifadesine ve kodun boyutuna bakar; eski davranış budur. İkisini kendi görevlerinde karşılaştırmak için:

```powershell
# benchmark/tokenwise.toml içinde router = "simple"
tokenwise benchmark --methods tokenwise --repeat 3 --json-out simple.json
# router = "smart" yap
tokenwise benchmark --methods tokenwise --repeat 3 --compare simple.json
```

### Önbellek

Cevaplar `.tokenwise/cache.db` (SQLite) içinde tutulur. Anahtar; model, talimat ve dosya içerikleri dahil tüm istek metnidir. Gönderilen bir dosya değişirse anahtar da değişir, bu yüzden eski bir cevap asla yanlış dosyaya uygulanmaz.

### Maliyet kaydı

Her istek ve her model çağrısı `.tokenwise/ledger.db` içine yazılır: token sayıları, önbellekten gelip gelmediği, dolar maliyeti, değişen satır sayısı ve uygulanıp uygulanmadığı. Claude fiyatları [resmî fiyat sayfasından](https://platform.claude.com/docs/en/about-claude/pricing) alınıp `pricing.py`'ye yazılmıştır; önbellekten dönen cevap ve fiyatı bilinmeyen modeller (yerel modeller) 0 sayılır. Başka bir model için `tokenwise.toml`'a fiyat ekleyebilirsin:

```toml
[tokenwise.prices]
"gpt-4o-mini" = [0.15, 0.15, 0.075, 0.60]   # input, cache write, cache read, output ($ / 1M token)
```

## VS Code eklentisi

`vscode-extension/` klasöründe. Komut paletinden **Tokenwise: Ask for a change** ile isteği yazarsın, öneri VS Code'un kendi fark görünümünde açılır, **Apply** ya da **Discard** seçersin. Gezgin kenar çubuğundaki **Tokenwise** paneli son 7 günün ve tüm zamanın maliyetini, önbellek isabet oranını ve model bazında harcamayı gösterir; durum çubuğunda haftalık maliyet görünür. Eklenti arka planda `tokenwise ask --json`, `apply`, `undo` ve `report --json` komutlarını çalıştırır.

Kurulum (paket hâline getirip VS Code'a yükleme):

```powershell
cd vscode-extension
npm install
npm run package                                   # tokenwise-1.0.0.vsix oluşur
code --install-extension tokenwise-1.0.0.vsix
```

Ayarlardan `tokenwise.command` alanına tokenwise programının yolunu yaz (örneğin `C:\Users\ADIL CEVIK\tokenwise\.venv\Scripts\tokenwise.exe`); boş bırakırsan açık projenin `.venv` klasörüne, sonra PATH'e bakar.

## Karşılaştırma testi (benchmark)

`tokenwise benchmark` her görevi iki yöntemle çalıştırır ve sonuçları karşılaştırır:

| Yöntem | Ne gönderir | Ne ister |
| --- | --- | --- |
| `baseline` | projedeki **tüm** kaynak dosyaları, orta basamaktaki modele | değişen dosyaların **tamamını** |
| `tokenwise` | harita → seçilen dosyalar → yönlendirilen model | sadece değişen satırları |

Her çalıştırma projenin geçici bir kopyasında yapılır, değişiklik oraya uygulanır ve görevin `check` komutu (genelde testler) değişikliğin gerçekten çalışıp çalışmadığına karar verir. Asıl proje hiç değişmez, önbellek kapalıdır.

```powershell
tokenwise benchmark                          # benchmark/tasks.toml → benchmark/results.md
tokenwise benchmark --only discount          # tek görev
tokenwise benchmark --methods tokenwise      # sadece bir yöntem
tokenwise benchmark -m qwen2.5-coder:7b      # iki yöntem için de aynı model
tokenwise benchmark --repeat 3               # her görevi 3 kez: sonuçların ne kadar oynadığını gör
tokenwise benchmark --json-out once.json     # bütün sonuçları sakla
tokenwise benchmark --compare once.json      # bir değişiklikten sonra: ne iyileşti, ne kötüleşti
```

Her çalıştırma şunları ölçer:

| Ölçüm | Anlamı |
| --- | --- |
| Çözülen | Görevin `check` komutu geçti mi? Kaç görev çözüldü (%) |
| Regresyon | `suite` (projenin kendi testleri) değişiklikten önce geçiyordu, sonra kırıldı mı? Görev çözülmüş görünse bile başka bir şey bozulmuş olabilir. |
| Maliyet | Toplam ve **çözülen görev başına** maliyet ($) |
| Süre | Görev başına ortanca süre (sn) |
| Model çağrısı, yükseltme | Kaç çağrı yapıldı; yönlendirici daha güçlü bir modele geçmek zorunda kaldı mı (`↑`) |

Modeller her seferinde aynı cevabı vermez. Bu yüzden `--repeat` ile her görev birkaç kez çalıştırılınca çözülme oranının çalıştırmalar arasında ne kadar oynadığı da yazılır, örneğin "%60–%80". İki sürüm arasındaki fark bu aralıktan küçükse, `--compare` tablosu onu gürültü sayar.

Görev kodu bir klasörden (`project = "..."`) ya da GitHub'dan sabit bir commit'ten (`repo = "sahip/ad"`, `ref = "<commit>"`, isteğe bağlı `subdir`) gelebilir; GitHub projeleri ilk kullanımda `.repos/` altına indirilir. `benchmark/` klasöründe örnek bir proje (`projects/shop`) ve 5 görev var; her görevin kontrolü değişiklikten önce başarısız olur. Anlamlı sonuç için bunları kendi projelerinden ~20 gerçek görevle değiştir. Görev biçimi:

```toml
[[task]]
name = "discount"
project = "projects/shop"                         # tasks.toml'a göre göreli yol
request = "Add Cart.apply_discount(percent) ..."  # tokenwise ask'e yazacağın istek
check = "{python} -m pytest -q {checks}/test_discount.py"   # değişiklikten sonra başarılı olmalı
suite = "{python} -m pytest -q"   # isteğe bağlı: projenin kendi testleri, regresyon için
```

`suite` her görev için ayrı ya da dosyanın en başında, bütün görevler için bir kez yazılabilir.

Model ayarları `benchmark/` klasöründeki `tokenwise.toml`'dan okunur (yoksa varsayılanlar); yerel modelle denemek için orada `tokenwise init --local` çalıştır. Sonuçlar `benchmark/results.md`'ye tablo olarak yazılır ve her çalıştırma `benchmark/.tokenwise/ledger.db`'ye yöntem adıyla kaydedilir.

### Sonuçlar

#### Gerçek ölçüm: yerel modellerle, ücretsiz

Kendi GitHub projelerimden 20 görev, bir Windows dizüstünde Ollama ile çalıştırıldı (RTX 3050 Ti 4 GB). Seçici `qwen2.5-coder:3b`, düzenleyici `qwen3.5:9b`. Her görevin kontrol betiği değişikliğin gerçekten çalışıp çalışmadığına karar veriyor.

| Tur | O turda ne değişti | Geçen kontrol |
| --- | --- | ---: |
| 1 | Sadece `qwen2.5-coder:3b` | 3/20 |
| 2 | `qwen3.5:9b` merdiveni; kodu bozan değişiklikleri yakalayan koruma; büyük projelerde harita kısaltma | 9/20 |
| 3 | Ölçüm hataları giderildi (Windows konsol kodlaması, kurulu olmayan araçlar); istekte adı geçen fonksiyonun dosyasını bulma; uydurma yolları düzeltme | **15/18** |

Her turda başarısız görevlerin değişikliklerine ve hata çıktılarına bakıldı. tokenwise'tan kaynaklanan her neden düzeltildi:

- **Kodu bozan değişiklikler:** Bozuk girinti, silinip hâlâ kullanılan bir fonksiyon, başka modüllerin kullandığı bir değişkenin adını değiştirme.
- **Bağlama sığmama:** Büyük projelerde harita ve 2.270 satırlık bir dosya modelin bağlamına sığmıyordu.
- **Yanlış dosya:** İstekte adı geçmesine rağmen yanlış dosya seçildi.
- **Uydurma yollar:** `core/isci_takip.py`, `path/to/predict.py`.

3. turda kalan üç hata:
- **Biri yakalanacak:** Fonksiyon dışına kayan bir `return`; artık koruma bunu yakalıyor.
- **İkisi modelin isteği yanlış anlaması:** `upos` yerine `deprel`; `RAG_TOP_K` yerine `RAG_TOP_K_RESULTS`.

İki görev atlandı, çünkü o bilgisayarda `php` ve `javac` kurulu değildi. Ayrıntılar [`benchmark/github/results-local-qwen.md`](benchmark/github/results-local-qwen.md) dosyasında.

#### Tahmini maliyet karşılaştırması

**Tahmini, model çağrılmadan.** Kendi GitHub projelerimden 20 gerçek görev üzerinde hesaplandı. Projeler 2 bin ile 370 bin token arasında; Python, JavaScript, PHP, Java ve Kotlin kodu içeriyor. Tablolar [`benchmark/github/estimate.md`](benchmark/github/estimate.md) dosyasında.

| Toplam (20 görev) | Baseline | tokenwise | Değişim |
| --- | ---: | ---: | ---: |
| Giriş tokeni | 1.763.113 | 95.374 | −95% |
| Çıkış tokeni | 70.837 | 1.903 | −97% |
| Maliyet ($) | 4,23 | 0,09 | −98% |

Nasıl hesaplandı:
- İki yöntem de tokenwise'ın gerçek kodundan geçti: harita, seçici istemi, yönlendirici, odaklı görünüm, edit ayrıştırma ve baseline istemi.
- Model yerine her görevin elle yazılmış referans cevabı kullanıldı ([`reference.toml`](benchmark/github/reference.toml)): doğru dosya seçimi ve en küçük doğru değişiklik.
- Her görevin kontrol betiği orijinal kodda **başarısız**, iki yöntemin değişikliğinden sonra **başarılı** oldu.
- Token sayısı karakter / 4 olarak tahmin edildi, iki yöntem için aynı şekilde.

Neyi göstermiyor:
- Gerçek bir modelin ne sıklıkla ilk seferde doğru yaptığını ya da kaç tekrar gerektiğini.
- Modelin gerçekte ne kadar uzun yazdığını.
- Prompt caching etkisini. Aynı projeye art arda istekte her iki yöntem de önbellekten yararlanır.

Büyük projelerde (`sesli-asistan-gateway`, `kelime-kelime`) baseline tek istekte 225–366 bin token gönderiyor; bu, bağlam sınırı ve fiyat kademesi yüzünden pratikte zor. tokenwise'ın girişinin çoğu ise ucuz modelin okuduğu proje haritası; harita `map_max_tokens` bütçesine sığacak şekilde kısaltılır (önce testler, sonra ayrıntılar).

#### Bu ölçümlerin sınırları

Buradaki ve [docs/measurements.md](docs/measurements.md)'deki sayılar projenin kendi ölçümleridir. Henüz kimse bunları bağımsız olarak tekrar çalıştırmadı. Okurken şunları bil:

- **Yerel model denemesi (15/18):**
  - Tek bir bilgisayarda, tek seferlik bir çalıştırma; güven aralığı yok.
  - Görevleri ve kontrol betiklerini tokenwise'ı geliştirenler yazdı; görevler de kendi projelerinden alındı.
  - Üç turun arasında tokenwise bu görevlerdeki hatalara göre düzeltildi. Yani son tur, aynı görevlere göre iyileştirilmiş bir sürümü ölçüyor; yeni görevlerde başarı daha düşük olabilir.
- **Maliyet tahmini (−98%):** Model çağrılmadan, elle yazılmış en iyi cevapla hesaplandı (yukarıdaki "Neyi göstermiyor" listesi). Gerçek kullanımdaki tasarruf daha az olacaktır.
- **Risk skoru (AUC 0,68–0,77):**
  - "Sorunlu commit" etiketi bir varsayıma dayanıyor: sonraki 20 commit içinde aynı koda dokunan bir hata düzeltmesi. Bu etiket gerçek hataları tam yansıtmaz.
  - Ölçüm 10 açık kaynak depoda yapıldı. Başka dillerde ve şirket içi kodda sonuç farklı olabilir.
- **Yanlış alarm oranı (~%10):** 394 bulgunun 47'sinden oluşan bir örneklem üzerinden. Etiketleri bir insan değil, yapay zekâ (Claude) koydu. Gerçek oranı, beta kullanıcılarının "Yok say / Çözüldü" kararları `tokenwise findings --stats` ile gösterecek.

Bu sayıları başkalarının nasıl kontrol edeceği [docs/verification.md](docs/verification.md)'de anlatılıyor:
- **Tekrar üretme:** tek komutla ya da GitHub Actions'ta, sabit commit'lerden. 19 Kasım 2026'da GitHub'ın makinesinde aynı sayılar çıktı.
- **İnsan etiketleri:** iki dış gözden geçirenin dolduracağı tablolar.
- **Görülmemiş görevler:** dış kullanıcıların yazacağı görevlerle ölçüm.

Gerçek modelle ölçmek için (projeler sabit commit'lerinden otomatik indirilir):

```powershell
tokenwise benchmark benchmark/github/tasks.toml -o benchmark/github/results.md
```

## Neden "edit blokları"?

Düzenleyici model değişiklikleri şu biçimde döndürür:

```
app/auth.py
<<<<<<< SEARCH
def login(user, password):
=======
def login(user, password):
    if len(password) < 8:
        raise ValueError("password too short")
>>>>>>> REPLACE
```

Modellerin yazdığı klasik *unified diff*'te satır numaraları sık sık kayar ve fark uygulanamaz. Aranan metni birebir verip yerine geleni yazmak hem aynı ölçüde kısadır hem de çok daha güvenilir uygulanır. Ekranda yine standart diff olarak gösterilir. Aranan metin bulunamazsa ya da birden fazla yerde geçerse araç modelden düzeltmesini ister (en fazla 3 deneme). Model başka bir dosyayı görmesi gerektiğini söylerse (`NEED_FILES:`) o dosya eklenip tekrar sorulur.

## Proje yapısı

```
tokenwise/
├── cli.py        # komutlar: init, map, ask, apply, undo, report, benchmark, cache
├── config.py     # ayarlar: varsayılanlar → tokenwise.toml → ortam değişkenleri
├── repo_map.py   # proje haritası (Python: ast, JS/TS: düzenli ifadeler)
├── selector.py   # ucuz modelle dosya seçimi
├── router.py     # zorluk tahmini ve model merdiveni
├── editor.py     # edit blokları, model merdiveninde tırmanma, uygulama ve geri alma
├── pipeline.py   # bir isteğin baştan sona akışı (CLI, JSON ve benchmark ortak kullanır)
├── ledger.py     # maliyet kaydı (.tokenwise/ledger.db) ve özetler
├── pricing.py    # model fiyatları ve token → dolar hesabı
├── focus.py      # büyük dosyalardan sadece ilgili fonksiyonları gösterme
├── checker.py    # kontrol komutunu geçici kopyada çalıştırma
├── textio.py     # satır sonu (CRLF/LF) ve BOM koruyan okuma/yazma
├── benchmark.py  # baseline ile tokenwise karşılaştırması, geçici kopyada kontrol
├── llm.py        # Anthropic ve OpenAI uyumlu arka uçlar, SQLite önbellek
├── graph.py      # ortak çekirdek: import grafiği, etkilenen dosyalar, döngüler, test eşleşmesi
├── history.py    # ortak çekirdek: git geçmişi, değişim sıklığı, hata düzeltmeleri, birlikte değişim
├── analyzers.py  # RepoDoctor: deterministik analizler, kalıcı parmak izli bulgular
├── health.py     # RepoDoctor: bulgu veritabanı (.tokenwise/health.db), skor, haftalık rapor
├── release.py    # ReleaseGuard: PR risk skoru, kanıtlar, kontrol listesi
├── web.py        # tokenwise serve: yerel web arayüzünün sunucusu
└── prompts.py    # modellere giden tüm talimatlar
vscode-extension/ # VS Code eklentisi (TypeScript), CLI'ı JSON modunda çağırır
benchmark/        # örnek proje, görevler ve kontrol testleri
```

## Geliştirme

```bash
ruff check . && ruff format --check .
pytest --cov=tokenwise
```

Testler sahte bir modelle çalışır, API'ye para harcamaz. Her push'ta GitHub Actions aynı kontrolleri çalıştırır ve VS Code eklentisini derleyip paketler.

## Web arayüzü

Tarayıcıda sohbet gibi kullanmak için:

```powershell
pip install "tokenwise-coder[web]"
tokenwise serve --root C:\projem                 # birden çok proje için --root tekrarla
```

Adres `http://127.0.0.1:8765` olur ve yalnızca bu bilgisayardan erişilir. Sayfada:

- **Değişiklik iste:** isteğini yaz, model ve dosyaları seç (boş bırakırsan seçici bulur), önce tahmin al, sonra gönder. Öneri satır içi diff olarak gelir; **Uygula**, **Vazgeç** ya da git deposuysa **Commit'le**.
- **Planla:** bir hedef yaz; iş parçacıklarına bölünür, her birinin hazır prompt'u vardır. Kopyalayabilir, tek tek ya da **Planı sırayla çalıştır** ile çalıştırabilirsin. Planlar `.tokenwise/plans/` altında kalır ve Markdown olarak indirilebilir.

Başka bir adresten erişmek istersen (`--host 0.0.0.0`) `--token` vermek zorunludur; sayfa bu anahtarı her istekte gönderir.

## Platform: Agent, RepoDoctor, ReleaseGuard

Tokenwise üç modülü tek bir ortak çekirdek üzerinde çalıştırır. Çekirdek, projenin import
grafiğini ve git geçmişini çıkarır; üç modül de bu veriyi kullanır.

| Modül | Sorduğu soru | Komut |
| --- | --- | --- |
| Agent | Bu değişikliği en az tokenla nasıl yaparım? | `tokenwise ask`, `tokenwise fix` |
| RepoDoctor | Bu deponun genel durumu nasıl? | `tokenwise scan`, `tokenwise findings` |
| ReleaseGuard | Bu değişiklik bu depo için ne kadar riskli? | `tokenwise risk` |

```bash
tokenwise scan                      # analiz et, bulguları kaydet, sağlık skorunu çıkar
tokenwise findings                  # öncelik sırasıyla açık bulgular
tokenwise findings --report         # haftalık Markdown rapor
tokenwise fix 12                    # bulgu #12'yi Agent'a düzelttir, yeniden tarayıp doğrula
tokenwise risk --base main          # bu dalın riski, kanıtları ve kontrol listesi
tokenwise risk -f markdown          # PR yorumu olarak yapıştırılacak rapor
tokenwise calibrate                 # risk sinyallerini bu deponun geçmişiyle ölç (AUC)
tokenwise calibrate --save          # önerilen ağırlıkları bu projede kullan
tokenwise findings --stats          # kural başına yanlış alarm oranı
tokenwise scan --tools ruff,bandit,coverage   # dış araçların bulgularını da ekle
```

- **Kalibrasyon:** Geçmişteki her commit bir PR gibi puanlanır. Kod o commit'teki haliyle,
  geçmiş ise o commit'ten önceki haliyle kullanılır. Sonraki 20 commit içinde bir hata
  düzeltmesi aynı koda dokunduysa o commit "sorunlu" sayılır. Her sinyalin bu sorunlu
  commit'leri ne kadar iyi ayırdığı AUC ile ölçülür. `psf/requests` deposundaki 200 commit
  üzerinde risk skorunun AUC'si 0,77 çıktı; 10 depoda ortalama 0,68
  ([docs/measurements.md](docs/measurements.md)).
- **Fonksiyon düzeyinde etki:** Risk raporu hangi fonksiyonların değiştiğini, onları hangi
  fonksiyonların ve hangi testlerin çağırdığını gösterir. Python dosyaları `ast` ile okunur.
  JS/TS dosyaları için `pip install "tokenwise-coder[impact]"` ile Tree-sitter kurulmalıdır.
- **Konteynerde kontrol:** `tokenwise.toml` dosyasına `sandbox = "docker"` yazılırsa test
  komutu ağı kapalı, sistemi salt okunur ve CPU/bellek sınırlı bir konteynerde çalışır.
  Docker yoksa kontrol "başarısız" değil "atlandı" sayılır.

Döngü: **anla → tespit et → önceliklendir → düzelt → doğrula → izle.** Web arayüzünde
(`tokenwise serve`) aynı akış "Tara", "Bulgular", "Agent ile düzelt" ve "Riski hesapla"
düğmeleriyle çalışır; uygulanan düzeltmeden sonra bulgu yeniden taranıp doğrulanır.

Tasarım kuralları:

- **Analizi araçlar yapar, model değil.** Karmaşıklık, test eşleşmesi, güvenlik örüntüleri,
  import döngüleri ve geçmiş sinyalleri deterministiktir; tarama ve risk raporu hiç token
  harcamaz. Model yalnızca düzeltme ve açıklama için çağrılır.
- **Her bulgunun kalıcı kimliği vardır.** Parmak izi satır numarasına bağlı değildir; bulgu açık,
  çözülmüş, yeniden açılmış ya da (yalnızca bir kişi isterse) yok sayılmış olarak izlenir.
- **Öncelik = önem × sıcak nokta.** Sık değişen ve sık düzeltilen dosyalardaki bulgular öne çıkar.
- **Risk skoru karar vermez.** Altı sinyalin (etki, geçmiş hata, test eksikliği, kritiklik,
  karmaşıklık, büyüklük) her biri kanıtı ve güven seviyesiyle gösterilir. Ağırlıklar deneysel
  başlangıç değerleridir; `--fail-above` verilmedikçe CI'ı durdurmaz.

GitHub'da her PR'a risk raporu yorumu ve haftalık sağlık raporu için
[`examples/github/tokenwise.yml`](examples/github/tokenwise.yml) dosyasını
`.github/workflows/` altına kopyalayın. API anahtarı gerekmez.

## Barındırılan sürüm (GitHub App)

`tokenwise server` komutu aynı üç modülü bir GitHub App olarak çalıştırır:

- Her push'ta depo taranır.
- Her PR'a tek bir risk yorumu yazılır ve sonraki push'larda güncellenir.
- Panelde "AI ile düzelt" denince Agent bir değişiklik önerir. Organization sahibi onaylarsa
  bir taslak PR açılır. Ana dala hiçbir zaman yazılmaz.

Veri ve iş kuyruğu PostgreSQL'de tutulur; kontrol komutları yalnızca kilitli konteynerlerde
çalışır. Kurulum için [`docs/hosting.md`](docs/hosting.md) dosyasına bakın.

```bash
pip install "tokenwise-coder[server]"
docker compose up -d --build        # PostgreSQL + web + worker
```

## Yayınlama

Yayın, etiket (tag) gönderince GitHub Actions tarafından otomatik yapılır. Bir kerelik hazırlık:

1. **PyPI:** pypi.org'da hesap aç → *Your projects → Publishing → Add a new pending publisher*: proje adı `tokenwise-coder`, sahip `Tugbacevikk`, repo `tokenwise`, iş akışı `release.yml`, ortam `pypi`. Böylece GitHub'a hiçbir şifre koymadan yayın yapılır (*trusted publishing*).
2. **GitHub:** repo *Settings → Environments* altında `pypi` adında bir ortam oluştur.
3. **VS Code Marketplace:** marketplace.visualstudio.com/manage adresinde `tugbacevik` adlı yayıncı (publisher) oluştur; Azure DevOps'tan *Marketplace → Manage* yetkili bir kişisel erişim anahtarı (PAT) al ve repo *Settings → Secrets → Actions* altına `VSCE_PAT` adıyla ekle. Yayıncı adı farklı olursa `vscode-extension/package.json` içindeki `publisher` alanını değiştir.

Her yayında:

```powershell
# 1. pyproject.toml ve tokenwise/__init__.py'deki sürümü, CHANGELOG.md'yi güncelle, commit et
git tag v1.0.0 && git push origin v1.0.0                  # → PyPI + GitHub Release
git tag vscode-v1.0.0 && git push origin vscode-v1.0.0    # → VS Code Marketplace
```

İş akışı, etiketin paketteki sürümle aynı olduğunu kontrol eder; farklıysa yayınlamaz.

## Katkı

Hata ve öneriler için [Issues](https://github.com/Tugbacevikk/tokenwise/issues) bölümündeki şablonları kullan. Kod katkısında: önce `ruff` ve `pytest` temiz geçsin, değişiklik `CHANGELOG.md`'ye yazılsın.

## Yol haritası

- [x] Aşama 0–1: iskelet, proje haritası, dosya seçici, edit blokları, uygulama/geri alma
- [x] Aşama 2: zorluğa göre model seçimi ve yükseltme, istek önbelleği, yerel model desteği (Ollama)
- [x] Aşama 3: maliyet kaydı (SQLite), `tokenwise report`, `--json` ve `apply`
- [x] Aşama 4: karşılaştırma testi altyapısı ve örnek görevler (`tokenwise benchmark`)
- [ ] Kendi projelerinden 20 görevle ölçüm ve sonuç tablosu
- [x] Aşama 5: VS Code eklentisi (fark görünümü, uygula/geri al, maliyet paneli)
- [x] Aşama 6: yayın altyapısı (PyPI ve Marketplace iş akışları, CHANGELOG, issue şablonları)
- [ ] İlk yayın: v1.0.0 etiketi
- [x] Platform: ortak çekirdek, RepoDoctor, ReleaseGuard
- [x] Faz 1 araçları: risk kalibrasyonu, kural başına yanlış alarm oranı
- [x] Faz 2: Docker sandbox, Ruff/Bandit/coverage, fonksiyon düzeyinde etki
- [x] Faz 3: barındırılan sürüm (GitHub App, PostgreSQL, kuyruk, panel)
- [x] Faz 1 ölçümü: 10 gerçek depo ([docs/measurements.md](docs/measurements.md))
- [x] Faz 4: beta hazırlığı (gürültü azaltma, services katmanı, `tokenwise server doctor`)
- [ ] Canlı GitHub App denemesi ve ilk beta kullanıcıları

## Lisans

MIT
