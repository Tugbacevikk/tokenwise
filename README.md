# tokenwise

**Yapay zekâya kod yazdırırken en az token ve en az kodla sonuç almak için komut satırı aracı.**
*A frugal AI coding assistant: sends less context, writes less code.*

Modele bütün projeyi göndermek ve bütün dosyayı yeniden yazdırmak yerine tokenwise:

1. Projenin **haritasını** çıkarır: dosya yolları ve fonksiyon/sınıf imzaları, gövdeler olmadan.
2. **Ucuz bir model** (Haiku) haritaya bakıp işin hangi dosyalara ihtiyaç duyduğunu seçer.
3. **Sadece o dosyalar** düzenleyici modele (Sonnet) gönderilir.
4. Model dosyayı baştan yazmaz, sadece **değişen satırları** döndürür.
5. Değişiklik renkli fark (diff) olarak gösterilir, onay verirsen uygulanır, istersen geri alınır.
6. Her adımda harcanan token tablo olarak yazdırılır.

Sabit kısımlar (talimat + harita) API'nin *prompt caching* özelliğiyle işaretlenir; aynı oturumda tekrar okunduklarında çok daha ucuza gelir.

## Kurulum

Python 3.11 veya üzeri gerekir.

```bash
git clone https://github.com/Tugbacevikk/tokenwise
cd tokenwise
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
```

## Kullanım

Çalışmak istediğin projenin klasöründe:

```bash
tokenwise init                 # tokenwise.toml ve .env oluşturur, API anahtarını sorar
tokenwise map                  # proje haritası ve "tüm kaynak vs harita" token karşılaştırması
tokenwise ask "login fonksiyonuna şifre uzunluk kontrolü ekle"
tokenwise ask "..." --dry-run  # sadece farkı göster, dosyaya yazma
tokenwise ask "..." -y         # onay sormadan uygula
tokenwise undo                 # son uygulanan değişikliği geri al
```

Başka bir klasör için her komuta `--root <klasör>` verebilirsin.

### Ayarlar

`tokenwise init` şu dosyayı oluşturur; istediğin gibi değiştir:

```toml
[tokenwise]
selector_model = "claude-haiku-5-5"
editor_model = "claude-sonnet-5-5"
extensions = [".py", ".js", ".jsx", ".ts", ".tsx"]
max_files = 8
```

Model adları `TOKENWISE_SELECTOR_MODEL` ve `TOKENWISE_EDITOR_MODEL` ortam değişkenleriyle de değiştirilebilir. API anahtarı sadece `.env` içinde durur ve `init` onu `.gitignore`'a ekler.

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
├── cli.py        # komutlar: init, map, ask, undo
├── config.py     # ayarlar: varsayılanlar → tokenwise.toml → ortam değişkenleri
├── repo_map.py   # proje haritası (Python: ast, JS/TS: düzenli ifadeler)
├── selector.py   # ucuz modelle dosya seçimi
├── editor.py     # edit blokları, farkın hesaplanması, uygulama ve geri alma
├── llm.py        # Anthropic API sarmalayıcısı ve token sayımı
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
- [ ] Aşama 2: işin zorluğuna göre model seçimi (Haiku / Sonnet / Opus), istek önbelleği
- [ ] Aşama 3: maliyet kaydı (SQLite) ve `tokenwise report`
- [ ] Aşama 4: 20 görevlik karşılaştırma testi, sonuç tablosu
- [ ] Aşama 5: VS Code eklentisi
- [ ] Aşama 6: PyPI ve Marketplace yayını

## Lisans

MIT
