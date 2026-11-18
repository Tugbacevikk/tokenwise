# Tokenwise (hosted) — Kişisel veriler ve saklama

> **TASLAK.** Bu metin, kodun bugün ne yaptığını anlatan teknik bir dökümdür. KVKK aydınlatma
> metni ya da gizlilik politikası olarak yayımlanmadan önce bir hukuk uzmanı tarafından
> incelenmeli; veri sorumlusunun unvanı, adresi, başvuru yolu ve yurt dışı aktarım dayanağı
> gibi bölümler uzmanla birlikte doldurulmalıdır. `[...]` ile işaretli yerler bu bölümlerdir.

Bu belge yalnızca **hosted** sürümü (GitHub App + panel) kapsar. Kendi bilgisayarında çalışan
`tokenwise` komutu ve yerel sayfa hiçbir veriyi Tokenwise sunucusuna göndermez.

## Veri sorumlusu

`[Unvan, adres, iletişim, KEP / e-posta]`

## Hangi veriler, nereden, ne için

| Veri | Nereden gelir | Ne için | Nerede tutulur |
| --- | --- | --- | --- |
| GitHub kullanıcı kimliği (sayısal id) ve kullanıcı adı | GitHub ile giriş | Oturum açmak, kimin hangi organization'ın üyesi olduğunu bilmek, düzeltmeyi kimin istediğini/onayladığını göstermek | `users`, `members` tabloları |
| Oturum | Giriş sırasında üretilir | Girişin sürmesi | Tarayıcıda çerez; sunucuda yalnızca çerezin özeti (hash), 24 saat |
| Organization / hesap adı ve kimliği, kurulum kimliği | GitHub App kurulumu | Hangi deponun kime ait olduğunu bilmek | `organizations` |
| Depo adları, varsayılan dal | GitHub | Taramak ve panelde göstermek | `repositories` |
| Bulgular: dosya yolu, satır numarası, kural, kısa açıklama | Kodun taranması | Sağlık skoru ve öncelikli iş listesi | `findings`, `scans`, `snapshots` |
| PR risk raporları | PR'daki değişikliğin incelenmesi | PR yorumu ve panel | `pull_requests` |
| Onaylanan / önerilen düzeltmeler: değişen dosyaların **önceki ve sonraki içeriği** | Agent'ın önerisi | Onaydan önce gösterilmesi, taslak PR açılması | `fixes` |
| Model kullanımı: model adı, token sayısı, maliyet, isteyen kişi | Agent çağrıları | Maliyetin organization'a gösterilmesi | `model_usage` |
| Model API anahtarı | Organization sahibi girer | Agent'ın o organization'ın hesabıyla çalışması | `organizations.model_key`, **şifreli**; panelde bir daha gösterilmez, dışa aktarmaya girmez |
| İşlem kayıtları (log) | Sunucu | Hata ayıklama | Sunucu günlükleri; anahtarlar ve token'lar maskelenir |

**Kaynak kod:** Tarama, PR raporu ve düzeltme için depo, iş süresince sunucudaki geçici bir
klasöre kopyalanır ve iş bitince silinir. Veritabanına kodun kendisi yazılmaz; yalnızca
yukarıdaki bulgular ve onay bekleyen düzeltmelerin değiştirdiği dosyaların içeriği yazılır.

**Yapay zekâ sağlayıcısına aktarım:** "AI ile düzelt" ya da "Agent'a yaptır" kullanıldığında
ilgili dosyaların içeriği, organization'ın kendi girdiği anahtarla seçtiği sağlayıcıya
(Anthropic ya da izin verilen OpenAI uyumlu bir adres) gönderilir. Bu sağlayıcılar yurt dışında
olabilir. `[Yurt dışı aktarımın hukuki dayanağı ve sağlayıcıların politikalarına bağlantı]`.
Tarama ve PR risk raporu hiçbir modele veri göndermez.

**Çerezler:** Yalnızca oturum çerezi ve giriş sırasında 10 dakikalık bir güvenlik çerezi
kullanılır. Reklam, analitik ya da izleme çerezi yoktur.

## Ne kadar saklanır

| Kayıt | Süre | Nasıl silinir |
| --- | --- | --- |
| Organization ve altındaki her şey (depolar, taramalar, bulgular, PR raporları, düzeltmeler, model kullanımı, üyelikler) | GitHub App kurulu kaldığı sürece | App kaldırıldığı anda tamamı silinir |
| Bir deponun kayıtları | Depo App'ten çıkarılana kadar | Depo çıkarıldığında silinir |
| Oturumlar | 24 saat | Süresi dolunca saatlik temizlikte; "Çıkış yap" ile hemen |
| Hiçbir organization'a üye olmayan ve oturumu kalmayan kullanıcı | 30 gün | Saatlik temizlikte |
| Biten / başarısız kuyruk işleri | 30 gün | Saatlik temizlikte |
| Webhook teslimat kimlikleri (tekrarları önlemek için) | 30 gün | Saatlik temizlikte |
| Kaldırılan kurulumların kimliği (geç gelen olayları yok saymak için) | 90 gün | Saatlik temizlikte |
| Veritabanı yedekleri | 14 gün | `deploy/backup.sh` eski yedekleri siler |
| Sunucu günlükleri (log) | Servis başına en çok 5 × 10 MB; dolunca en eskisi silinir (yoğunluğa göre birkaç gün ile birkaç hafta) | `docker-compose.yml` içindeki `x-logging` ayarı |

Yukarıdaki süreler bu depodaki `docker-compose.yml` ile kurulan dağıtım içindir. Farklı bir kurulumda (başka bir log sistemi, farklı `BACKUP_KEEP_DAYS`) metin o kuruluma göre güncellenmelidir. Süreler kodda `tokenwise/server/db.py` içindeki `RETENTION_DAYS` ile tanımlıdır; worker
temizliği yaklaşık saatte bir çalıştırır.

## Haklar ve nasıl kullanılır

- **Verilerin bir kopyası:** Organization sahibi panelde *Model ayarları → Veriler → Verileri
  indir (JSON)* ile o organization için saklanan her şeyi indirir (API anahtarı hariç).
- **Kişinin kendi kaydının silinmesi:** Panelde *Hesabımı sil*. Kullanıcı adı, oturumlar ve
  üyelikler silinir. O kişinin istediği düzeltmeler ve model kullanımı organization'da kalır
  ama artık kişiyle ilişkilendirilmez.
- **Organization'ın bütün verisinin silinmesi:** GitHub App'i kaldırmak yeterlidir.
- **Yedeklerden silinme:** Silinen veri, en geç 14 gün içinde yedeklerden de çıkar.
- `[KVKK m.11 kapsamındaki diğer haklar ve başvuru yolu]`

## Güvenlik önlemleri (özet)

Model anahtarları şifreli saklanır. Oturum çerezleri `HttpOnly`, `Secure` ve `__Host-`
önekiyle gönderilir. Her API isteğinde organization üyeliği denetlenir. Webhook imzaları
doğrulanır. Müşteri kodu, ağı kapalı, salt okunur ve kaynakları sınırlı bir Docker
konteynerinde çalıştırılır (ayrıntı: `docs/hosting.md`).
