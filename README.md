# tokenwise

**Yapay zekâya kod yazdırırken en az token ve en az kodla sonuç almak için komut satırı aracı.**
*A frugal AI coding assistant: sends less context, writes less code.*

Modele bütün projeyi göndermek ve bütün dosyayı yeniden yazdırmak yerine tokenwise:

1. Projenin **haritasını** çıkarır: dosya yolları ve fonksiyon/sınıf imzaları, gövdeler olmadan.
2. **En ucuz model** haritaya bakıp işin hangi dosyalara ihtiyaç duyduğunu seçer.
3. **Sadece o dosyalar** düzenleyici modele gönderilir.
4. **Yönlendirici** isteğin zorluğunu kurallarla tahmin eder (model çağrısı yok) ve model merdiveninin uygun basamağından başlar; model beceremezse bir üst modele geçer.
5. Model dosyayı baştan yazmaz, sadece **değişen satırları** döndürür.
6. Değişiklik renkli fark (diff) olarak gösterilir, onay verirsen uygulanır, istersen geri alınır.
7. Birebir aynı istek tekrar gelirse cevap **önbellekten** döner, model çağrılmaz.
8. Her adımda harcanan token tablo olarak yazdırılır.

Claude API'de sabit kısımlar (talimat + harita) *prompt caching* ile işaretlenir; aynı oturumda tekrar okunduklarında çok daha ucuza gelir.

## Kurulum

Python 3.11 veya üzeri gerekir.

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
tokenwise map                  # proje haritası ve "tüm kaynak vs harita" token karşılaştırması
tokenwise ask "login fonksiyonuna şifre uzunluk kontrolü ekle"
tokenwise ask "..." --dry-run  # sadece farkı göster, dosyaya yazma
tokenwise ask "..." -y         # onay sormadan uygula
tokenwise ask "..." -m claude-opus-5-5   # tek bir modeli zorla, merdiven yok
tokenwise ask "..." --no-cache # önbelleğe bakma
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
ollama pull qwen2.5-coder:7b   # 4 GB VRAM'li ekran kartında çalışır; daha hızlı istersen qwen2.5-coder:3b
cd <projen>
tokenwise init --local
tokenwise ask "..." --dry-run
```

`init --local`, `provider = "openai"` ve `base_url = "http://localhost:11434/v1"` yazar; LM Studio, vLLM ya da OpenAI'nin kendisi gibi OpenAI uyumlu her sunucu aynı yoldan çalışır (anahtar gerekiyorsa `OPENAI_API_KEY` ortam değişkeni).

### Ayarlar

`tokenwise init` şu dosyayı oluşturur; istediğin gibi değiştir:

```toml
[tokenwise]
provider = "anthropic"          # ya da "openai" (Ollama, LM Studio, vLLM...)
# base_url = "http://localhost:11434/v1"
models = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]   # ucuzdan güçlüye
extensions = [".py", ".js", ".jsx", ".ts", ".tsx"]
max_files = 8
```

`models` bir merdivendir: dosya seçici hep ilkini kullanır; düzenleyici, yönlendiricinin seçtiği basamaktan başlar (kolay → 1., orta → 2., zor → 3.) ve her basamakta 2 deneme yapıp bir üste çıkar. Tek model yazarsan merdiven yoktur, sadece tekrar dener. `TOKENWISE_PROVIDER`, `TOKENWISE_BASE_URL` ve `TOKENWISE_MODELS` (virgülle ayrılmış) ortam değişkenleri ayar dosyasını ezer. API anahtarı sadece `.env` içinde durur ve `init` onu `.gitignore`'a ekler.

### Önbellek

Cevaplar `.tokenwise/cache.db` (SQLite) içinde tutulur. Anahtar; model, talimat ve dosya içerikleri dahil tüm istek metnidir. Gönderilen bir dosya değişirse anahtar da değişir, bu yüzden eski bir cevap asla yanlış dosyaya uygulanmaz.

### Maliyet kaydı

Her istek ve her model çağrısı `.tokenwise/ledger.db` içine yazılır: token sayıları, önbellekten gelip gelmediği, dolar maliyeti, değişen satır sayısı ve uygulanıp uygulanmadığı. Claude fiyatları [resmî fiyat sayfasından](https://platform.claude.com/docs/en/about-claude/pricing) alınıp `pricing.py`'ye yazılmıştır; önbellekten dönen cevap ve fiyatı bilinmeyen modeller (yerel modeller) 0 sayılır. Başka bir model için `tokenwise.toml`'a fiyat ekleyebilirsin:

```toml
[tokenwise.prices]
"gpt-4o-mini" = [0.15, 0.15, 0.075, 0.60]   # input, cache write, cache read, output ($ / 1M token)
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
├── cli.py        # komutlar: init, map, ask, undo, cache
├── config.py     # ayarlar: varsayılanlar → tokenwise.toml → ortam değişkenleri
├── repo_map.py   # proje haritası (Python: ast, JS/TS: düzenli ifadeler)
├── selector.py   # ucuz modelle dosya seçimi
├── router.py     # zorluk tahmini ve model merdiveni
├── editor.py     # edit blokları, model merdiveninde tırmanma, uygulama ve geri alma
├── pipeline.py   # bir isteğin baştan sona akışı (CLI, JSON ve benchmark ortak kullanır)
├── ledger.py     # maliyet kaydı (.tokenwise/ledger.db) ve özetler
├── pricing.py    # model fiyatları ve token → dolar hesabı
├── llm.py        # Anthropic ve OpenAI uyumlu arka uçlar, SQLite önbellek
└── prompts.py    # modellere giden tüm talimatlar
```

## Geliştirme

```bash
ruff check . && ruff format --check .
pytest --cov=tokenwise
```

Testler sahte bir modelle çalışır, API'ye para harcamaz. Her push'ta GitHub Actions aynı kontrolleri çalıştırır.

## Yol haritası

- [x] Aşama 0–1: iskelet, proje haritası, dosya seçici, edit blokları, uygulama/geri alma
- [x] Aşama 2: zorluğa göre model seçimi ve yükseltme, istek önbelleği, yerel model desteği (Ollama)
- [x] Aşama 3: maliyet kaydı (SQLite), `tokenwise report`, `--json` ve `apply`
- [ ] Aşama 4: 20 görevlik karşılaştırma testi, sonuç tablosu
- [ ] Aşama 5: VS Code eklentisi
- [ ] Aşama 6: PyPI ve Marketplace yayını

## Lisans

MIT
