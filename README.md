# tokenwise

**Yapay zekâya kod yazdırırken en az token ve en az kodla sonuç almak için komut satırı aracı.**
*A frugal AI coding assistant: sends less context, writes less code.*

Modele bütün projeyi göndermek ve bütün dosyayı yeniden yazdırmak yerine tokenwise:

1. Projenin **haritasını** çıkarır: dosya yolları ve fonksiyon/sınıf imzaları, gövdeler olmadan.
2. **En ucuz model** haritaya bakıp işin hangi dosyalara ihtiyaç duyduğunu seçer.
3. **Sadece o dosyalar** düzenleyici modele gönderilir; büyük dosyalardan da sadece ilgili fonksiyonlar.
4. **Yönlendirici** isteğin zorluğunu kurallarla tahmin eder (model çağrısı yok) ve model merdiveninin uygun basamağından başlar; model beceremezse bir üst modele geçer.
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
```

Görev kodu bir klasörden (`project = "..."`) ya da GitHub'dan sabit bir commit'ten (`repo = "sahip/ad"`, `ref = "<commit>"`, isteğe bağlı `subdir`) gelebilir; GitHub projeleri ilk kullanımda `.repos/` altına indirilir. `benchmark/` klasöründe örnek bir proje (`projects/shop`) ve 5 görev var; her görevin kontrolü değişiklikten önce başarısız olur. Anlamlı sonuç için bunları kendi projelerinden ~20 gerçek görevle değiştir. Görev biçimi:

```toml
[[task]]
name = "discount"
project = "projects/shop"                         # tasks.toml'a göre göreli yol
request = "Add Cart.apply_discount(percent) ..."  # tokenwise ask'e yazacağın istek
check = "{python} -m pytest -q {checks}/test_discount.py"   # değişiklikten sonra başarılı olmalı
```

Model ayarları `benchmark/` klasöründeki `tokenwise.toml`'dan okunur (yoksa varsayılanlar); yerel modelle denemek için orada `tokenwise init --local` çalıştır. Sonuçlar `benchmark/results.md`'ye tablo olarak yazılır ve her çalıştırma `benchmark/.tokenwise/ledger.db`'ye yöntem adıyla kaydedilir.

### Sonuçlar

**Tahmini, model çağrılmadan.** Kendi GitHub projelerimden 20 gerçek görev üzerinde hesaplandı. Projeler 2 bin ile 370 bin token arasında; Python, JavaScript, PHP, Java ve Kotlin kodu içeriyor. Tablolar [`benchmark/github/estimate.md`](benchmark/github/estimate.md) dosyasında.

| Toplam (20 görev) | Baseline | tokenwise | Değişim |
| --- | ---: | ---: | ---: |
| Giriş tokeni | 1.763.113 | 91.557 | −95% |
| Çıkış tokeni | 70.837 | 1.903 | −97% |
| Maliyet ($) | 4,23 | 0,08 | −98% |

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

## Lisans

MIT
